import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import aclosing, asynccontextmanager
from typing import Protocol, cast

import httpx
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    SystemMessage,
    message_chunk_to_message,
    messages_to_dict,
)
from langchain_openai import ChatOpenAI
from opentelemetry.trace import Status, StatusCode, Tracer
from pydantic import Field, JsonValue, SecretStr, TypeAdapter

from deta.events import Listener, ModelDone, TextDelta, ToolCallDelta, emit
from deta.observability.artifacts import Artifacts
from deta.types import AgentMessage, Data, ToolSchema

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class ModelConfig(Data):
    """由入口传入 OpenAI 兼容接口地址、模型、凭据、时限和输出额度。"""

    model: str = Field(min_length=1)
    api_key: SecretStr
    base_url: str = Field(default=DEFAULT_BASE_URL, min_length=1)
    timeout_seconds: float = Field(default=60, gt=0)
    max_completion_tokens: int = Field(default=2048, ge=1)


class ModelProtocolError(Exception):
    """响应缺少终态，或工具调用无法可靠配对。"""


@asynccontextmanager
async def open_model(config: ModelConfig) -> AsyncIterator[ChatOpenAI]:
    """创建 LangChain 模型，并在请求结束或取消后关闭 HTTP 资源。"""
    with httpx.Client() as sync_http:
        async with httpx.AsyncClient() as async_http:
            yield ChatOpenAI(
                model=config.model,
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.timeout_seconds,
                max_retries=0,
                stream_usage=True,
                use_responses_api=False,
                http_client=sync_http,
                http_async_client=async_http,
            )


async def read_response(
    stream: AsyncGenerator[AIMessage, None],
    listeners: Sequence[Listener],
) -> AIMessage:
    """读完并关闭模型流，发布增量，校验后返回完整响应。"""
    complete: AIMessageChunk | None = None
    async with aclosing(stream):
        async for chunk in stream:
            if not isinstance(chunk, AIMessageChunk):
                raise ModelProtocolError("expected an assistant message chunk")
            if chunk.text:
                emit(TextDelta(text=chunk.text), listeners)
            for call in chunk.tool_call_chunks:
                if call["index"] is None:
                    raise ModelProtocolError("missing tool call index")
                emit(
                    ToolCallDelta(
                        index=call["index"],
                        arguments_delta=call["args"] or "",
                    ),
                    listeners,
                )
            complete = chunk if complete is None else complete + chunk
    if complete is None or complete.additional_kwargs.get("function_call") is not None:
        raise ModelProtocolError("empty stream or legacy function_call")
    reason = complete.response_metadata.get("finish_reason")
    if reason not in {
        "stop",
        "tool_calls",
        "length",
        "content_filter",
    }:
        raise ModelProtocolError("missing or unsupported finish_reason")
    message = message_chunk_to_message(complete)
    if not isinstance(message, AIMessage):
        raise ModelProtocolError("expected an assistant message")
    if message.invalid_tool_calls:
        raise ModelProtocolError("invalid tool arguments")
    ids = [call["id"] for call in message.tool_calls]
    if any(
        not (call["id"] or "").strip() or not call["name"].strip()
        for call in message.tool_calls
    ) or len(ids) != len(set(ids)):
        raise ModelProtocolError("missing or duplicate tool identity")
    if (reason == "tool_calls" and not message.tool_calls) or (
        reason == "stop" and message.tool_calls
    ):
        raise ModelProtocolError("tool calls disagree with finish_reason")
    return message


async def stream_once(
    client: ChatOpenAI,
    config: ModelConfig,
    instructions: str,
    messages: Sequence[AgentMessage],
    tools: Sequence[ToolSchema],
    *,
    tracer: Tracer,
    artifacts: Artifacts,
    listeners: Sequence[Listener] = (),
    before_attempt: Callable[[], int] | None = None,
    input_sources: JsonValue = None,
) -> AIMessage:
    """完成一次 LangChain 异步流请求，发布增量并返回完整 AIMessage。

    不执行工具、不重试、不提交历史；错误和取消由调用方处理。
    """
    if client.max_retries != 0:
        raise ValueError("要求关闭模型内部重试，确保 Attempt 计数真实")
    langchain_messages = [SystemMessage(content=instructions), *messages]
    schema = list(tools)
    bound = client.bind_tools(schema) if schema else client
    # 记录应用提交给 LangChain 的输入；不是 HTTP 原始报文。
    snapshot: JsonValue = TypeAdapter(JsonValue).validate_python(
        {
            "messages": messages_to_dict(langchain_messages),
            "model": config.model,
            "max_completion_tokens": config.max_completion_tokens,
            "tools": schema,
            "config_version": "deta-langchain-native-v1",
            "sdk_retries": 0,
            "input_sources": input_sources,
        }
    )
    with tracer.start_as_current_span(
        "deta.model.input", record_exception=False, set_status_on_exception=False
    ) as request:
        request.set_attribute("deta.model", config.model)
        request.set_attribute("deta.config_version", "deta-langchain-native-v1")
        ref = artifacts.save("request", snapshot)
        request.set_attribute(
            "deta.request_body", "captured_redacted" if ref else "unavailable"
        )
        if ref is not None:
            request.set_attribute("deta.request_artifact", ref)
        try:
            async with asyncio.timeout(config.timeout_seconds):
                attempt_number = before_attempt() if before_attempt is not None else 1
                with tracer.start_as_current_span(
                    "deta.model.attempt",
                    record_exception=False,
                    set_status_on_exception=False,
                ) as attempt:
                    attempt.set_attribute("deta.attempt", attempt_number)
                    try:
                        stream = cast(
                            AsyncGenerator[AIMessage, None],
                            bound.astream(
                                langchain_messages,
                                model=config.model,
                                max_completion_tokens=config.max_completion_tokens,
                            ),
                        )
                        message = await read_response(stream, listeners)
                        reason = message.response_metadata["finish_reason"]
                        attempt.set_attribute("deta.stop_reason", reason)
                        counts = message.usage_metadata
                        attempt.set_attribute("deta.usage_known", counts is not None)
                        if counts is not None:
                            for key in (
                                "input_tokens",
                                "output_tokens",
                                "total_tokens",
                            ):
                                attempt.set_attribute(f"deta.usage.{key}", counts[key])
                    except BaseException as exc:
                        attempt.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                        raise
            response_ref = artifacts.save("response", message.model_dump(mode="json"))
            if response_ref is not None:
                request.set_attribute("deta.response_artifact", response_ref)
            request.set_attribute("deta.tool_calls_proposed", len(message.tool_calls))
            emit(ModelDone(message=message), listeners)
            return message
        except BaseException as exc:
            request.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


class ModelBoundary(Protocol):
    """真实请求和录制响应共享的单次调用形状；不执行 Loop 或工具。"""

    async def __call__(
        self,
        config: ModelConfig,
        instructions: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolSchema],
        *,
        listeners: Sequence[Listener] = (),
        before_attempt: Callable[[], int] | None = None,
        input_sources: JsonValue = None,
    ) -> AIMessage: ...
