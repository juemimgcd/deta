# Day 5：单次模型请求与运行预算

[总览](summary.md) · [前一天](day4.md) · [下一天](day6.md)

## 核心问题

一次逻辑请求可能有多次网络尝试。预算应该在准备参数时扣减，还是在真正进入 SDK 调用边界时扣减？后面的 Loop 怎样让模型请求和工具批次使用同一份计数？

Day 4 已经提供 RunBudget。本章把它接到 Day 2 的单次模型边界，确定请求额度、工具额度、超时和重试的分工。工具包与 tools.py 直接沿用。

## 三种边界不要混在一起

| 边界 | 谁负责 | 计数或行为 |
| --- | --- | --- |
| 一次 SDK 尝试 | stream_once | 调用前执行 before_attempt；本函数始终只发一次请求 |
| 一个逻辑请求的有限重试 | Day 7 AgentSession._request | 重用同一输入，每次尝试都消费同一 RunBudget |
| 整次 Run | Day 7 Agent._drive 与 run_loop | 总时限、轮次调度和工具批次预算 |

```text
Loop 创建一份 RunBudget
  → runtime._request(plan, listener, budget)
      → stream_once(..., before_attempt=budget.take_request)
          → 准备消息与 schema、保存输入快照
          → take_request() 检查并扣减额度
          → 开始一次 SDK Attempt
      → 如允许重试，仍传同一份 budget
  → execute_tool_batch(..., budget)
      → reserve_tools(整批数量)
      → 按序调用 Day 4 execute_tool
```

额度不足在实际尝试之前退出。配置转换失败不会产生虚假的 Attempt；已经进入 SDK 边界但远端结果未知的请求仍占一次尝试额度。工具参数被拒绝也消耗调度额度，避免无限重复错误调用。

## 更新 model.py 一次

用下面完整文件替换 Day 2 的 model.py。主要变化是 before_attempt 回调和 model.input/model.attempt 两层 Span；函数不执行工具、不重试、不提交历史。Day 6–7 直接调用这个版本。

### src/deta/model.py

<details>
<summary>直接提供：src/deta/model.py（完整文件）</summary>

```python
import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import aclosing, asynccontextmanager
from typing import cast

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


class ModelConfig(Data):
    """由入口显式传入模型标识、凭据、总时限和输出额度。"""

    model: str = Field(min_length=1)
    api_key: SecretStr
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
                base_url="https://api.openai.com/v1",
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
                raise ModelProtocolError(
                    "expected an assistant message chunk"
                )
            if chunk.text:
                emit(TextDelta(text=chunk.text), listeners)
            for call in chunk.tool_call_chunks:
                if call["index"] is None:
                    raise ModelProtocolError(
                        "missing tool call index"
                    )
                emit(
                    ToolCallDelta(
                        index=call["index"],
                        arguments_delta=call["args"] or "",
                    ),
                    listeners,
                )
            complete = (
                chunk if complete is None else complete + chunk
            )
    if (
        complete is None
        or complete.additional_kwargs.get("function_call")
        is not None
    ):
        raise ModelProtocolError(
            "empty stream or legacy function_call"
        )
    reason = complete.response_metadata.get("finish_reason")
    if reason not in {
        "stop",
        "tool_calls",
        "length",
        "content_filter",
    }:
        raise ModelProtocolError(
            "missing or unsupported finish_reason"
        )
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
        raise ModelProtocolError(
            "missing or duplicate tool identity"
        )
    if (reason == "tool_calls" and not message.tool_calls) or (
        reason == "stop" and message.tool_calls
    ):
        raise ModelProtocolError(
            "tool calls disagree with finish_reason"
        )
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
            "config_version": "day2-langchain-native-v1",
            "sdk_retries": 0,
        }
    )
    with tracer.start_as_current_span(
        "deta.model.input", record_exception=False, set_status_on_exception=False
    ) as request:
        request.set_attribute("deta.model", config.model)
        request.set_attribute("deta.config_version", "day7-langchain-v1")
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
```

</details>

## 重试条件在这里先定清楚

只有连接错误、超时、408/429/5xx 可以在额度内重试，而且必须尚未发布文本或工具参数增量。一旦调用者已看到增量，再重试可能把两份响应拼在一起，应结束本次 Run。

SDK 的 max_retries 固定为零。Day 7 runtime._request 才实现唯一的有限重试循环，重试不会重新执行已提交的工具，也不重新消费队列输入。退避等待计入 Run 总时限。

| 情况 | 后续处理 |
| --- | --- |
| 完整 AIMessage，finish_reason=tool_calls | Day 6 批次执行器检查预算后调用工具 |
| length/content_filter 中有可配对调用 | 构造配对失败结果，不执行可能不完整的调用 |
| invalid_tool_calls、缺失或重复调用 ID | 模型协议错误直接传播 |
| 参数字典可解析但不符合工具字段规则 | Day 4 返回 invalid_arguments，供模型修正 |
| 内部异常或用户取消 | 保留异常/取消路径，不通过普通 Hook 决策恢复 |

## 正常 API 的接线位置

在 Day 2 的真实请求代码中，创建 `RunBudget(RunOptions(...))`，把 `budget.take_request` 这个函数对象传给 stream_once 的 before_attempt。不要提前调用它，否则准备参数阶段就会消耗额度。

当前 CLI 仍是 Day 2 的单次文本入口；完整 Agent CLI 到 Day 7 才安装。这里的可运行检查点是一次模型请求和真实 Attempt 记录，不宣称已有工具循环。

## 怎样核对

将本章代码写入实际源码后，再运行：

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
```

静态检查、真实工具调用和真实模型运行分别记录；本文中的代码与调用示例不代表已经完成运行验收。

沿真实请求的 Trace 核对输入快照、实际 Attempt 编号、最终响应和 usage。没有实际网络失败时，将重试行为标为未验证；不生成 mock 或故障测试来替代真实运行证据。

下一阶段 [Day 6：历史配对与整批工具执行](day6.md) 固定 Loop 将调用的三个辅助函数。
