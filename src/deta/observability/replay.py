import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    convert_to_openai_messages,
)
from pydantic import JsonValue, TypeAdapter

from deta.events import Event, Listener, emit
from deta.hooks import RequestPlan
from deta.model import ModelBoundary, ModelConfig
from deta.observability.artifacts import Artifacts
from deta.tools import tool_schemas
from deta.types import AgentMessage, RunResult, ToolSchema

if TYPE_CHECKING:
    from deta.runtime import AgentSession

JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
EVENT: TypeAdapter[Event] = TypeAdapter(Event)
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_UPDATES = 20000


def digest(value: JsonValue) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def model_key(
    config: ModelConfig,
    instructions: str,
    messages: Sequence[AgentMessage],
    tools: Sequence[ToolSchema],
) -> JsonValue:
    """比较提供方实际输入；新 Session 的 Entry/Run/Span ID 不参与语义匹配。"""
    return JSON.validate_python(
        {
            "model": config.model,
            "max_completion_tokens": config.max_completion_tokens,
            "messages": convert_to_openai_messages(
                [SystemMessage(content=instructions), *messages]
            ),
            "tools": list(tools),
        }
    )


def tool_key(call: ToolCall, plan: RequestPlan) -> JsonValue:
    """调用 ID、结构化参数和当时工具声明都必须一致，不按工具名猜匹配。"""
    return JSON.validate_python(
        {
            "call": dict(call),
            "tools": tool_schemas(plan.tools),
        }
    )


class Recorder:
    """为一次空会话开始的真实 Run 录制边界结果；普通采集失败不改变真实执行。"""

    def __init__(self, artifacts: Artifacts) -> None:
        self.artifacts = artifacts
        self.refs: list[str] = []
        self.complete = True
        self.closed = False

    def save(
        self, kind: str, key: JsonValue, updates: list[JsonValue], result: JsonValue
    ) -> None:
        body: JsonValue = {
            "kind": kind,
            "input": key,
            "updates": updates,
            "result": result,
        }
        if len(json.dumps(body).encode()) > MAX_RECORD_BYTES:
            self.complete = False
            return
        ref = self.artifacts.save("replay-step", {"body": body, "sha256": digest(body)})
        if ref is None:
            self.complete = False
        else:
            self.refs.append(ref)

    def append_update(
        self, updates: list[JsonValue], value: JsonValue, captured_bytes: int
    ) -> int:
        size = len(json.dumps(value, ensure_ascii=False).encode())
        if len(updates) >= MAX_UPDATES or captured_bytes + size > MAX_RECORD_BYTES:
            self.complete = False
            return captured_bytes
        updates.append(value)
        return captured_bytes + size

    def attach(self, runtime: "AgentSession") -> None:
        """只替换已有边界，不新建调度循环；摘要请求同样经过 model_call。"""
        if runtime.agent.running or runtime.session.entries():
            raise ValueError("首版录制入口要求空闲的新会话")
        original: ModelBoundary = runtime.model_call
        original_tool = runtime.agent.bindings.execute_tool

        async def model(
            config: ModelConfig,
            instructions: str,
            messages: Sequence[AgentMessage],
            tools: Sequence[ToolSchema],
            *,
            listeners: Sequence[Listener] = (),
            before_attempt: Callable[[], int] | None = None,
            input_sources: JsonValue = None,
        ) -> AIMessage:
            updates: list[JsonValue] = []
            captured_bytes = 0

            def observe(event: Event) -> None:
                nonlocal captured_bytes
                captured_bytes = self.append_update(
                    updates, event.model_dump(mode="json"), captured_bytes
                )
                emit(event, listeners)

            try:
                result = await original(
                    config,
                    instructions,
                    messages,
                    tools,
                    listeners=[observe],
                    before_attempt=before_attempt,
                    input_sources=input_sources,
                )
            except BaseException:
                self.complete = False
                raise
            self.save(
                "model",
                model_key(config, instructions, messages, tools),
                updates,
                result.model_dump(mode="json"),
            )
            return result

        async def tool(
            call: ToolCall,
            plan: RequestPlan,
            on_start: Callable[[], None],
            on_output: Callable[[str], None],
        ) -> ToolMessage:
            updates: list[JsonValue] = []
            captured_bytes = 0

            def start() -> None:
                nonlocal captured_bytes
                captured_bytes = self.append_update(
                    updates, {"kind": "start"}, captured_bytes
                )
                on_start()

            def output(text: str) -> None:
                nonlocal captured_bytes
                captured_bytes = self.append_update(
                    updates, {"kind": "output", "text": text}, captured_bytes
                )
                on_output(text)

            try:
                result = await original_tool(call, plan, start, output)
            except BaseException:
                self.complete = False
                raise
            self.save(
                "tool", tool_key(call, plan), updates, result.model_dump(mode="json")
            )
            return result

        runtime.model_call = model
        runtime.agent.bindings = replace(runtime.agent.bindings, execute_tool=tool)

    def finish(self, result: RunResult, prompt: str) -> str | None:
        """Run 彻底结束后再封口；缺失、失败尝试、取消或采集超限都拒绝完整回放。"""
        if self.closed:
            raise ValueError("录制已经封口")
        self.closed = True
        # 首版 tape 没有录制队列注入时机；额外用户输入不能被标成完整回放。
        inputs = tuple(
            message for message in result.messages if isinstance(message, HumanMessage)
        )
        initial_only = inputs == (HumanMessage(content=prompt),)
        body: JsonValue = {
            "version": 1,
            "closed": True,
            "complete": self.complete and result.status == "completed" and initial_only,
            "input_capture": "initial_only"
            if initial_only
            else "additional_input_unsupported",
            "steps": list(self.refs),
            "prompt": prompt,
            "run_id": result.run_id,
            "status": result.status,
            "answer": result.answer,
        }
        return self.artifacts.save(
            "replay-index", {"body": body, "sha256": digest(body)}
        )


class ReplayMismatch(Exception):
    """资料不足或实际调用与录制不一致；绝不能转去真实模型或工具补齐。"""


def read_record(path: Path, root: Path) -> dict[str, Any]:
    """限制证据读取范围，并拒绝脱敏改写、截断或不完整的文件。"""
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or resolved.stat().st_size > MAX_RECORD_BYTES:
        raise ReplayMismatch("录制文件越界或超限")
    with resolved.open("rb") as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ReplayMismatch("录制文件超限")
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("body"), dict):
        raise ReplayMismatch("录制封装无效")
    body = value["body"]
    if digest(JSON.validate_python(body)) != value.get("sha256"):
        raise ReplayMismatch("录制已被脱敏改写、损坏或编辑")
    return cast(dict[str, Any], body)


class Replay:
    """按顺序消费已封口的单次请求和工具结果，不具备真实执行的后备路径。"""

    def __init__(self, index_path: Path) -> None:
        root = index_path.resolve(strict=True).parent
        self.index = read_record(index_path, root)
        if (
            self.index.get("version") != 1
            or not self.index.get("closed")
            or not self.index.get("complete")
        ):
            raise ReplayMismatch("记录没有满足完整回放条件")
        self.steps = [read_record(Path(path), root) for path in self.index["steps"]]
        self.position = 0

    def take(self, kind: str, key: JsonValue) -> dict[str, Any]:
        if self.position >= len(self.steps):
            raise ReplayMismatch("实际调用多于录制")
        step = self.steps[self.position]
        if step["kind"] != kind or step["input"] != key:
            raise ReplayMismatch(f"第 {self.position + 1} 个边界参数或顺序不一致")
        self.position += 1
        return step

    def attach(self, runtime: "AgentSession") -> None:
        """新工作区、新 Store 和空 Session；回放的 intent 仅写入这份临时数据库。"""
        if (
            runtime.agent.running
            or runtime.session.entries()
            or runtime.client is not None
        ):
            raise ReplayMismatch("离线入口必须使用空会话，且没有真实 SDK 客户端")

        async def model(
            config: ModelConfig,
            instructions: str,
            messages: Sequence[AgentMessage],
            tools: Sequence[ToolSchema],
            *,
            listeners: Sequence[Listener] = (),
            before_attempt: Callable[[], int] | None = None,
            input_sources: JsonValue = None,
        ) -> AIMessage:
            await asyncio.sleep(0)
            step = self.take("model", model_key(config, instructions, messages, tools))
            if before_attempt is not None:
                before_attempt()
            for data in step["updates"]:
                emit(EVENT.validate_python(data), listeners)
            return AIMessage.model_validate(step["result"])

        async def tool(
            call: ToolCall,
            plan: RequestPlan,
            on_start: Callable[[], None],
            on_output: Callable[[str], None],
        ) -> ToolMessage:
            await asyncio.sleep(0)
            step = self.take("tool", tool_key(call, plan))
            for data in step["updates"]:
                if data["kind"] == "start":
                    runtime.session.begin_tool(call)
                    on_start()
                elif data["kind"] == "output":
                    on_output(data["text"])
                else:
                    raise ReplayMismatch("不支持的工具通知")
            return ToolMessage.model_validate(step["result"])

        runtime.model_call = model
        runtime.agent.bindings = replace(runtime.agent.bindings, execute_tool=tool)

    def finish(self, result: RunResult) -> None:
        """Loop 把内部异常变成 RunResult 后，这一步仍需检查消费完整性和终态。"""
        if self.position != len(self.steps):
            raise ReplayMismatch("运行提前结束，仍有录制未消费")
        if (
            result.status != self.index["status"]
            or result.answer != self.index["answer"]
        ):
            raise ReplayMismatch("运行终态或最终回答与录制不同")


def timeline(path: Path) -> list[dict[str, Any]]:
    """按开始时间查看已经导出的 Span；缺失父节点保持缺失，不推测执行成功。"""
    spans: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Span 行格式无效")
                spans.append(value)
    return sorted(spans, key=lambda value: value.get("start_time", ""))
