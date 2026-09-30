# Day 2：单次流式模型请求与基础 Trace

[总览](summary.md) · [前一天](day1.md) · [下一天](day3.md)

## 核心问题

一次请求怎样从 Deta 消息进入 LangChain，再把流式碎片收集为完整 AIMessage？请求失败后，怎样找到当时发出的输入和结束状态？今天只做这一次请求，让 Day 4 的 Loop 复用它。

本文是可手写的实现指南。先完成 Day 1；下面给出的源码目前仍是参考答案，真实模型验收需要你实施后使用自己的配置运行。

## 今天新增什么

| 文件 | 变化 |
| --- | --- |
| `src/deta/model.py` | 配置对象、单次流式请求与分片合并 |
| `src/deta/observability/__init__.py` | 只声明包，不自动初始化服务 |
| `src/deta/observability/artifacts.py` | 可关闭、可脱敏的诊断正文采集 |
| `src/deta/observability/tracing.py` | OTel 上下文、本地 JSONL Span 导出和退出收尾 |
| `src/deta/cli.py` | 在 Day 1 的 help/version 上添加 prompt 与采集开关；本页给完整接入文件 |

复用 Day 1 类型和通知函数。本日不执行工具，不自动续轮，不保存 Session。模型提出工具调用时，保留调用并返回；参数 JSON 即使无效，也交给 Day 3 的参数校验处理。今天 CLI 默认不传工具声明，所以手工文本请求没有读取本地文件的能力。

## 模型接入选择与核对基线

采用 `langchain-openai` 的 ChatOpenAI 接入 OpenAI 官方 **Chat Completions**。LangChain 负责消息对象、工具绑定与流分片合并；Deta 自己实现 Loop、工具执行、Session、Context 和 Compaction。具体模型由 `OPENAI_MODEL` 显式指定，须对当前账户可用并支持文本流、函数工具和 `max_completion_tokens`。没有实际请求前不宣称某个模型已验证可用。

本次接入锁定 `langchain-openai==1.6.6`、`langchain-core==1.6.6` 和 `httpx==0.28.1`，以 `uv.lock` 为准。OpenAI SDK 仍是底层依赖；Day 7 的错误分类继续使用其异常类。业务模块不再导入 `ChatCompletion…Param` 类型，工具声明统一使用 Day 1 的 `ToolSchema`，也不再手写分片累积对象。

`astream` 返回 AIMessageChunk，使用 `complete + chunk` 合并文本、工具调用和 usage；usage 可能在最后一个独立块到达，不能在首次看到 finish_reason 时提前结束。工具参数仍只在完整响应后交给运行时处理。依据：[ChatOpenAI](https://docs.langchain.com/oss/python/integrations/chat/openai)、[消息与流式合并](https://docs.langchain.com/oss/python/langchain/messages)。

直接使用普通 `ChatOpenAI`，不继承它、不覆写私有方法。接受 LangChain 对工具参数的解析与重新序列化，不承诺保留原始 JSON 字节；本版不保证流中独立的拒绝正文或提供方原始响应 ID 完整保留，`AIMessage.id` 也可能由框架生成。这里不使用 create_agent 或 LangGraph。

## 先认识本日的类与函数

| 对象 | 字段与职责 |
| --- | --- |
| `ModelConfig` | `model` 是模型标识；`api_key` 用 SecretStr 避免普通 repr 显示原文；`timeout_seconds` 限制整次流读取；`max_completion_tokens` 是输出预算 |
| `AIMessageChunk` | LangChain 的流式消息块；`tool_call_chunks` 保存参数片段，`+` 按 index 合并；它不是已提交消息 |
| `ModelProtocolError` | 缺少最终结束原因、调用编号重复或响应结构不受支持时抛出的错误 |
| `Artifacts` | `root` 是文件目录；`capture_body` 控制正文采集；`redact` 是对字符串脱敏的普通函数 |
| `TracerProvider` / `Tracer` | OTel 对象；前者拥有导出处理器，后者创建 Span；不管理 Deta 消息历史 |
| `ChatOpenAI` | langchain-openai 提供的模型类；绑定工具、发送请求；本项目设置 `max_retries=0` |
| `ToolSchema` | `dict[str, Any]`：与 LangChain 接口一致的普通工具声明，交给 bind_tools，不依赖 SDK 类型 |

`SecretStr` 不是加密存储；入口将它显式传给模型。`open_model` 管理 HTTP 客户端的生命周期，取消后也关闭资源。核心模块没有在导入时读取环境变量或安装全局 tracer。

| 函数 | 调用方 → 输入 → 结果给谁 |
| --- | --- |
| `open_model(config)` | CLI 或评测入口进入异步上下文；创建模型，退出时关闭 HTTP 资源 |
| `read_response(stream, listeners)` | stream_once 调用；读取并关闭流、发布增量、验证响应，返回 AIMessage |
| `stream_once(...)` | CLI、以后 Loop 调用；绑定工具并只请求一次，发布增量，返回完整 AIMessage；错误/取消向外传播 |
| `Artifacts.save(kind, payload)` | 请求与工具边界调用；可选地脱敏并写 JSON；返回文件路径或 None；不能作为 Session 提交成功的依据 |
| `local_tracing(path)` | CLI 进入上下文；创建局部 provider 并返回 tracer；退出时尝试导出，最多等待 1 秒 |
| `show(event)` | `emit` 调用的终端观察者；仅输出 TextDelta，不追加历史 |
| `request(prompt, capture_body)` | CLI 调用；加载配置、组装依赖、await 单次请求、输出结束信息与退出码 |
| `main()` | 解析 CLI 参数，调用 asyncio.run；输出简洁错误类型和非零退出码 |

`stream_once` 是协程，不是异步生成器：调用方 `await` 它得到最终消息；流式更新通过 Listener 接收。这让“过程通知”和“最终结果”各有一个明确出口。

## 先走一遍数据流

```text
main → request("用一句话说明 Agent", capture_body=True)
  ├─ 读取环境 → ModelConfig
  ├─ local_tracing → tracer
  ├─ Artifacts → 正文采集器
  └─ stream_once(client, config, instructions, [HumanMessage], [])
       ├─ [SystemMessage(content=instructions), *messages]
       ├─ 保存最终请求参数快照 → request Span 引用
       ├─ bind_tools → 绑定声明，消息对象直接传入
       ├─ astream → attempt Span；complete + chunk 合并分片
       ├─ chunk → TextDelta → emit → show → 终端
       ├─ message_chunk_to_message → AIMessage，检查结束原因与调用身份
       ├─ 保存响应快照；emit(ModelDone)
       └─ return AIMessage → request → 退出码
```

`instructions` 创建 SystemMessage，其余历史直接使用 HumanMessage、AIMessage 和 ToolMessage。ToolMessage.status 标识成败，artifact 保存内部诊断；模型通过 content 读取结果或错误说明。

## 完整练习骨架

手写 `read_response` 与 `stream_once`：前者负责流读取、块合并和响应校验；后者负责请求配置、超时和诊断记录。普通 ChatOpenAI 和 open_model 负责接入与资源管理；不再写消息转换函数、模型子类或分片累积类。

填写顺序：先完成文本流，再处理工具参数增量，最后读取结束原因、用量并完成异常收尾。参数分片不能直接执行。

### src/deta/model.py

只填写：`read_response`、`stream_once`。保留导入、字段和其他已给实现。

```python
# ruff: noqa: F401  # 为练习体预留的导入。
import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import aclosing, asynccontextmanager
from typing import cast

import httpx
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UsageMetadata,
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
    raise NotImplementedError("请完成 read_response")


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
) -> AIMessage:
    """完成一次 LangChain 异步流请求，发布增量并返回完整 AIMessage。

    不执行工具、不重试、不提交历史；错误和取消由调用方处理。

        TODO：按下方参考答案完成本函数。
    """
    raise NotImplementedError("请完成 stream_once")
```

## 直接提供的观测与入口代码

### src/deta/observability/__init__.py

直接提供的完整文件。

```python
"""Local diagnostic artifacts and tracing; not a session store."""
```

### src/deta/observability/artifacts.py

直接提供的完整文件。

```python
import json
import logging
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

logger = logging.getLogger(__name__)


class Artifacts:
    """管理请求、响应和工具结果的诊断正文采集。
    调用方显式传入目录、采集开关和脱敏函数，保存失败不会代替业务结果。
    """

    def __init__(
        self,
        root: Path,
        *,
        capture_body: bool = False,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        """用 root、capture_body 和 redact 初始化正文采集器，并检查采集开关与脱敏函数的组合。
        这里只保存实例配置，创建对象不会写文件；构造完成后由 save 按需保存产物。
        """
        if capture_body and redact is None:
            raise ValueError("正文采集必须显式传入脱敏函数")
        # 该采集器写入诊断 JSON 文件的目录，实际保存时按需创建。
        self.root = root
        # 是否采集正文；为 False 时 save 直接返回 None，不写正文文件。
        self.capture_body = capture_body
        # 调用方提供的字符串脱敏函数；打开正文采集时必须显式提供。
        self.redact = redact

    def save(self, kind: str, payload: JsonValue) -> str | None:
        """接收产物类别 kind 和 JSON 数据 payload，按配置脱敏并保存为独立文件。
        请求或工具边界调用它取得文件路径；未开启采集或保存失败时返回 None。
        """
        if not self.capture_body:
            return None

        def clean(value: JsonValue) -> JsonValue:
            """递归处理待保存的 JSON 数据，对字符串值调用实例的 redact 函数。
            列表和字典保持原有层级，数字等值直接返回；结果交给 save 写入文件。
            """
            if isinstance(value, str):
                return self.redact(value) if self.redact is not None else value
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{kind}-{uuid4().hex}.json"
            with path.open("x", encoding="utf-8") as output:
                json.dump(clean(payload), output, ensure_ascii=False, indent=2)
            return str(path)
        except Exception as exc:
            logger.warning("artifact unavailable: %s", type(exc).__name__)
            return None
```

### src/deta/observability/tracing.py

直接提供的完整文件。

```python
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)


@contextmanager
def local_tracing(path: Path) -> Iterator[Tracer]:
    """根据 path 创建本地 Span 导出环境，在 with 语句中把 Tracer 交给调用方。
    退出时启动后台收尾并最多等待一秒，将导出资源的生命周期限制在这个上下文内。
    """
    provider = TracerProvider(
        resource=Resource.create({"service.name": "deta"}),
        shutdown_on_exit=False,
    )
    output = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        output = path.open("a", encoding="utf-8")
        exporter = ConsoleSpanExporter(
            out=output,
            # 将一个 Span 序列化为单行 JSON，便于逐行读取导出文件。
            formatter=lambda span: span.to_json(indent=None) + "\n",
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=256,
                max_export_batch_size=64,
                schedule_delay_millis=200,
                export_timeout_millis=1000,
            )
        )
    except OSError as exc:
        logger.warning("trace export unavailable: %s", type(exc).__name__)
    try:
        yield provider.get_tracer("deta", "day2-v1")
    finally:

        def shutdown() -> None:
            """关闭外层 local_tracing 创建的 provider，并在最后关闭输出文件。
            该函数由收尾线程调用，普通关闭异常只记录类型，返回 None。
            """
            try:
                provider.shutdown()
            except Exception as exc:
                logger.warning("trace shutdown failed: %s", type(exc).__name__)
            finally:
                if output is not None:
                    output.close()

        worker = threading.Thread(target=shutdown, daemon=True)
        worker.start()
        worker.join(timeout=1.0)
        if worker.is_alive():
            logger.warning("trace flush incomplete: shutdown timeout")
```

### src/deta/cli.py

直接提供的完整文件。

```python
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from deta import __version__
from deta.events import Event, TextDelta
from deta.model import ModelConfig, open_model, stream_once
from deta.observability.artifacts import Artifacts
from deta.observability.tracing import local_tracing


def show(event: Event) -> None:
    """接收 emit 发来的事件，将 TextDelta 的新增正文立即打印到终端。
    其他事件不显示，函数返回 None，不负责保存消息或判断请求是否成功。
    """
    if isinstance(event, TextDelta):
        print(event.text, end="", flush=True)


async def request(prompt: str, capture_body: bool) -> int:
    """接收用户问题与正文采集开关，读取环境配置并组装模型客户端、Trace 和产物采集器。
    等待一次 stream_once，显示结束原因与诊断目录，再把退出码返回给 main。
    """
    model = os.environ.get("OPENAI_MODEL", "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not model or not key:
        raise ValueError("请设置 OPENAI_MODEL 和 OPENAI_API_KEY")
    config = ModelConfig(model=model, api_key=SecretStr(key))
    run_id = uuid4().hex
    root = Path(".deta/runs") / run_id
    artifacts = Artifacts(
        root / "artifacts",
        capture_body=capture_body,
        # 对写入产物的字符串遮住本次 API key，再交给采集器保存。
        redact=lambda value: value.replace(key, "[REDACTED_API_KEY]"),
    )
    with local_tracing(root / "spans.jsonl") as tracer:
        with tracer.start_as_current_span(
            "deta.request_probe",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("deta.run_id", run_id)
            span.set_attribute("deta.mode", "single_request")
            async with open_model(config) as client:
                message = await stream_once(
                    client,
                    config,
                    "You are a helpful assistant.",
                    [HumanMessage(content=prompt)],
                    [],
                    tracer=tracer,
                    artifacts=artifacts,
                    listeners=[show],
                )
    print()
    print(
        f"stop_reason={message.response_metadata.get('finish_reason')}; usage={message.usage_metadata}",
        file=sys.stderr,
    )
    if message.additional_kwargs.get("refusal"):
        print(f"refusal={message.additional_kwargs.get('refusal')}", file=sys.stderr)
    print(f"diagnostics={root}", file=sys.stderr)
    return 0 if message.response_metadata.get("finish_reason") == "stop" else 1


def main() -> int:
    """作为命令入口解析问题和采集开关，通过 asyncio.run 启动单次模型请求。
    将请求的退出码返回给启动器，并将取消或异常转换成相应退出码和简洁提示。
    """
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="deta", description="Deta 单次模型请求")
    parser.add_argument("--version", action="version", version=f"deta {__version__}")
    parser.add_argument("-p", "--prompt")
    parser.add_argument("--capture-body", action="store_true")
    args = parser.parse_args()
    if args.prompt is None:
        parser.print_help()
        return 0
    if not args.prompt.strip():
        parser.error("prompt 不能为空白")
    try:
        return asyncio.run(request(args.prompt, args.capture_body))
    except KeyboardInterrupt:
        print("请求已取消", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"请求失败：{type(exc).__name__}", file=sys.stderr)
        if isinstance(exc, ValueError):
            print("检查必填配置与参数；未输出原始异常正文。", file=sys.stderr)
        return 1
```

## 完整参考答案

仅替换 model.py 的 TODO；上述基础文件就是其完整实现。

<details>
<summary>参考答案：src/deta/model.py（完整文件）</summary>

```python
import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
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
        "deta.model.request", record_exception=False, set_status_on_exception=False
    ) as request:
        request.set_attribute("deta.model", config.model)
        request.set_attribute("deta.config_version", "day2-langchain-native-v1")
        ref = artifacts.save("request", snapshot)
        request.set_attribute(
            "deta.request_body", "captured_redacted" if ref else "unavailable"
        )
        if ref is not None:
            request.set_attribute("deta.request_artifact", ref)
        try:
            async with asyncio.timeout(config.timeout_seconds):
                with tracer.start_as_current_span(
                    "deta.model.attempt",
                    record_exception=False,
                    set_status_on_exception=False,
                ) as attempt:
                    attempt.set_attribute("deta.attempt", 1)
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

## 像调试器一样看关键状态

### 1. 文本流与 usage

`complete` 是单次请求的局部 AIMessageChunk，初始为 None。第一个块直接赋值，后续使用 `complete + chunk` 合并。`chunk.text` 是这次增量，`complete.text` 是累计正文；终端已经打印过的文字不意味着响应成功提交。

`usage_metadata` 未提供时为 None；有报告时直接读取字典中的 input_tokens、output_tokens 和 total_tokens。`stream_usage=True` 请求用量块，结束原因到达后继续读完流，以接收末尾用量。

### 2. 同一个工具调用的参数分片

下面只是协议形状说明，不是录制数据或 mock：

```text
index=0, id="call_1", name="read", arguments='{"path":'
index=0, arguments='"target.md"}'
                     ↓ AIMessageChunk 的 + 按 index 合并
id="call_1", name="read", arguments='{"path":"target.md"}'
                     ↓ 流正常终止后构造
{"id": "call_1", "name": "read", "args": {"path": "target.md"}}
```

index 只在本次响应内分组，结果通过 id 配对。最终直接使用 AIMessage.tool_calls 中解析好的 args 字典；若 LangChain 标记 invalid_tool_calls，则本次请求作为协议失败退出。解析规则由 LangChain 管理，Deta 不再承诺原文保真；可解析但字段不符合工具 schema 的参数由 Day 3 返回配对错误。

缺少/重复 ID 的响应无法可靠配对，作为协议失败传播。`length` 或 `content_filter` 有明确终态，也可能保留部分内容；它们不授权执行其中的调用。Day 4 必须在执行前检查 stop_reason，对可配对但不可执行的调用构造失败结果；本日没有执行器，所以没有“半截参数误执行”。

### 3. Span 层级与计数

```text
deta.request_probe                 临时单次请求入口，不冒充 Agent Run
  └─ deta.model.request             逻辑请求、模型、请求/响应快照引用
       └─ deta.model.attempt        一次 SDK 尝试、耗时、usage、结束原因
```

三个 Span 共用 trace_id，通过 parent_id 关联；run_id 是 Deta 生成的业务编号，trace_id/span_id 由 OTel 创建，不能互相替代。本日没有 Turn Span，因为没有 Loop。Day 4 用实际 Run/Turn 包住同一个 stream_once。

SDK 重试关闭且 stream_once 不重试，因此一次逻辑请求最多有一个实际 Attempt。输入转换或配置在 SDK 调用前失败，就没有实际模型尝试；不能把这类错误统计成请求已发出。取消通常会在 attempt 中记录 CancelledError，外层总时限到期则由 asyncio.timeout 转为 TimeoutError。

### 4. 请求快照与正文采集边界

request 快照通过公开的 messages_to_dict 保存传入 LangChain 的消息、工具声明和模型配置；无工具时 tools 为 []。消息快照可能包含内部 metadata/artifact，因此它是应用输入记录，不是 HTTP 抓包或精确 wire payload。config_version 与 sdk_retries 是记录元数据；不采集授权头与密钥。

默认不采集正文，Span 的 `deta.request_body=unavailable` 明示限制。开 `--capture-body` 后，CLI 脱敏函数仅遮住本次 API key；其他敏感内容要在正式使用前按工作区需求扩展脱敏函数。脱敏后的快照有助于定位输入，不能宣称可逐字还原原始请求。

Artifacts.save 只服务诊断，采集关闭或导出失败返回 None，不改变模型结果。`deta.*` 是当前本地业务属性。Day 12 补充运行清单、资源来源和共享事件记录；本版沿用这些本地属性，标准 GenAI 字段映射留到确有对接需求时再做。

### 5. 有界导出与剩余限制

Span 通过容量 256 的 BatchSpanProcessor 队列导出；退出时在守护线程中执行 shutdown，主线程最多等待 1 秒。超时会警告，不能假装文件已经完整写好；这种方式不强行中断底层文件写操作，也不承诺崩溃时所有 Span 都保留。

本日的正文 JSON 写入仍是同步本地 I/O。Day 12 增加 manifest、事件记录，以及正文保存/跳过/失败计数，并明确 Trace 可能不完整。本版不实现精确 Span 丢失计数、异步正文队列、通用正文尺寸预算或阻塞 I/O 隔离；需要这些能力时另行安排。保存异常会被隔离，但同步磁盘操作仍可能延迟运行，不能把异常处理等同于完全的 I/O 隔离。

## 怎样核对

完成本日源码后，从项目根目录运行已有检查，再进行真实请求。配置项请在本地填入；不要把真实密钥写进文档或终端演示记录。

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
uv run deta --help
uv run deta -p "用一句中文说明什么是 Agent。"
uv run deta --capture-body -p "用一句中文说明什么是 Agent。"
```

验收时观察：终端逐段输出；结束后有 stop_reason 和 usage；打印的目录中 Span 关联一致；开启采集时 request Span 引用的文件确实存在，messages 与这次输入一致；关闭时没有正文文件。只看到文字但流未正常结束不能算成功。

在一个单独命令的环境中移除必填变量，核对缺配置时不发请求：

```bash
env -u OPENAI_API_KEY uv run deta -p "你好"
```

鉴权失败用一次临时的无效凭据请求检查 `AuthenticationError`、非零退出码及错误 Span；不要打印 SDK 原始异常正文。真实请求中按 Ctrl-C 应取消并关闭流；若没有终态 usage，仍记为未知。普通流成功、鉴权失败、取消分别记证据，未运行的项目标为未验证，不创建故障用例文件。

## Pi 对照与下一天

| Pi 位置 | 本日吸收的职责 | 差异 |
| --- | --- | --- |
| `agent-loop.ts` 的 streamAssistantResponse | 流更新与最终响应分开 | Pi 的上下文转换、消息状态和事件编排跨越多个职责；Deta 模型层只做 SDK 转换，状态提交由 Day 4 Loop 完成 |
| `types.ts` 的 StreamFn 与消息事件 | 提供方流通过边界转成内部类型 | 当前只支持文本/函数工具，无图片与 thinking 块 |
| `harness/telemetry.ts`、`docs/telemetry.md` | 参考操作层次与关联思路 | Deta 的 OTel 导出是本地方案，本日不宣称 Pi telemetry 或自身全链路已完成 |

将实际实现位置、请求目录和剩余缺口追加到 Day 1 建立的 `docs/pi-alignment.md`。下一阶段用同一 ToolCall/ToolMessage 契约完成[工具调度并接入现成 read](day3.md)，Day 4 再让模型自己调用工具并继续回答。
