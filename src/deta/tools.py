import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolCall, ToolMessage
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from deta.builtin_tools import ToolContext, ToolOutput
from deta.builtin_tools._files import MutationError
from deta.builtin_tools.bash import BashArgs, run_bash
from deta.builtin_tools.edit import EditArgs, edit_file
from deta.builtin_tools.read import ReadArgs, ReadError, read_file
from deta.builtin_tools.write import WriteArgs, write_file
from deta.hooks import BeforeToolDecision, Hooks
from deta.observability.artifacts import Artifacts
from deta.types import ToolSchema


@dataclass(frozen=True)
class ToolSpec[Args: BaseModel]:
    """将一个工具的名称、说明、参数模型和执行函数绑定成一份定义。
    Args 表示该工具的参数类型，同一份定义同时用于生成 schema 与本地执行。
    """

    # 模型可见的工具名称，应与 TOOLS 中的字典键保持一致。
    name: str
    # 发给模型的工具用途说明，帮助模型选择何时调用该工具。
    description: str
    # 参数模型类对象，用于生成 JSON Schema 并验证实际传入的参数。
    args_model: type[Args]
    # 所有工具统一接收执行上下文，返回 ToolOutput。
    handler: Callable[[Args, ToolContext], Awaitable[ToolOutput]]


def async_file_handler[Args: BaseModel](
    handler: Callable[[Args, Path], str | ToolOutput],
) -> Callable[[Args, ToolContext], Awaitable[ToolOutput]]:
    """注册时适配同步文件函数；取消时等线程收尾，再传播取消。"""

    async def run(args: Args, context: ToolContext) -> ToolOutput:
        work = asyncio.create_task(asyncio.to_thread(handler, args, context.workspace))
        try:
            raw = await asyncio.shield(work)
        except asyncio.CancelledError:
            try:
                await work
            except Exception:
                pass
            raise
        return ToolOutput(raw) if isinstance(raw, str) else raw

    return run


TOOLS: dict[str, ToolSpec[Any]] = {
    "read": ToolSpec(
        name="read",
        description="读取 UTF-8 文本，返回行号和分页提示。",
        args_model=ReadArgs,
        handler=async_file_handler(read_file),
    ),
    "bash": ToolSpec(
        name="bash",
        description="在工作目录运行 shell 命令，保留输出并返回退出码。",
        args_model=BashArgs,
        handler=run_bash,
    ),
    "write": ToolSpec(
        name="write",
        description="创建或覆盖文本文件，返回 diff。",
        args_model=WriteArgs,
        handler=async_file_handler(write_file),
    ),
    "edit": ToolSpec(
        name="edit",
        description="按原文件唯一匹配批量替换，返回 diff。",
        args_model=EditArgs,
        handler=async_file_handler(edit_file),
    ),
}


def tool_schemas(
    registry: Mapping[str, ToolSpec[Any]] | None = None,
) -> list[ToolSchema]:
    """遍历显式工具表，为每个 ToolSpec 生成提供方需要的函数工具声明。
    参数 schema 来自该工具自己的参数模型，返回列表供调用方传给 stream_once。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.args_model.model_json_schema(),
                "strict": False,
            },
        }
        for spec in (TOOLS if registry is None else registry).values()
    ]


def resolve_tool_call(
    call: ToolCall,
    registry: Mapping[str, ToolSpec[Any]] | None = None,
) -> tuple[ToolSpec[Any], BaseModel]:
    """接收模型提出的 ToolCall，按名称查找工具并验证结构化参数字典。
    返回工具定义和参数对象给 execute_tool；未知名称抛 KeyError，参数错误抛 ValidationError。
    """
    spec = (TOOLS if registry is None else registry)[call["name"]]
    args = spec.args_model.model_validate(call["args"])
    return spec, args


async def run_tool(
    call: ToolCall,
    context: ToolContext,
    *,
    registry: Mapping[str, ToolSpec[Any]],
    hooks: Hooks,
    on_start: Callable[[], None] | None,
    tracer: Tracer,
    dispatch: Span,
) -> ToolOutput:
    """查表、校验、前置决策、执行；普通失败返回输出，交给外层统一后处理。"""
    if call["name"] not in registry:
        return ToolOutput("工具不存在", "unknown_tool")
    try:
        spec, args = resolve_tool_call(call, registry)
    except ValidationError:
        return ToolOutput(
            "参数错误，请按工具 schema 提供完整 JSON 对象。", "invalid_arguments"
        )
    decision = BeforeToolDecision()
    if hooks.before_tool is not None:
        decision = await hooks.before_tool(deepcopy(call), args.model_copy(deep=True))
    if not isinstance(decision, BeforeToolDecision):
        raise TypeError("before_tool 必须返回 BeforeToolDecision")
    if not decision.allow:
        return ToolOutput(
            decision.reason or "本次工具执行被 Hook 拒绝",
            "denied",
            terminate=decision.terminate,
        )
    if on_start is not None:
        on_start()
    dispatch.set_attribute("deta.execution_started", True)
    try:
        with tracer.start_as_current_span(
            "deta.tool.execute", record_exception=False, set_status_on_exception=False
        ) as execution:
            try:
                return await spec.handler(args, context)
            except BaseException as exc:
                execution.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
    except ReadError as exc:
        return ToolOutput(str(exc), "read_failed")
    except MutationError as exc:
        return ToolOutput(str(exc), f"{call['name']}_failed")
    except (OSError, UnicodeError) as exc:
        return ToolOutput(
            f"文件操作失败：{type(exc).__name__}", f"{call['name']}_failed"
        )


async def execute_tool(
    call: ToolCall,
    context: ToolContext,
    *,
    tracer: Tracer,
    artifacts: Artifacts,
    registry: Mapping[str, ToolSpec[Any]] | None = None,
    on_start: Callable[[], None] | None = None,
    hooks: Hooks | None = None,
) -> ToolMessage:
    """统一校验、执行决策、工具执行与结果后处理，保存原始和最终结果的对应关系。

    业务错误返回配对结果；Hook、产物之外的内部错误和取消向外传播。
    """
    table = TOOLS if registry is None else registry
    policy = hooks or Hooks()
    with tracer.start_as_current_span(
        "deta.tool.dispatch", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("deta.tool_call_id", (call["id"] or ""))
        span.set_attribute("deta.tool_name", call["name"])
        span.set_attribute("deta.execution_started", False)

        try:
            call_ref = artifacts.save(
                "tool-call", TypeAdapter(JsonValue).validate_python(call)
            )
            if call_ref is not None:
                span.set_attribute("deta.call_artifact", call_ref)
            output = await run_tool(
                call,
                context,
                registry=table,
                hooks=policy,
                on_start=on_start,
                tracer=tracer,
                dispatch=span,
            )
            raw = ToolMessage(
                tool_call_id=call["id"] or "",
                name=call["name"],
                content=output.content,
                status="error" if output.error_code else "success",
                artifact={
                    "error_code": output.error_code,
                    "details": output.details,
                    "terminate": output.terminate,
                },
            )
            raw_ref = artifacts.save("tool-raw", raw.model_dump(mode="json"))
            if raw_ref is not None:
                span.set_attribute("deta.raw_artifact", raw_ref)
            result = raw
            if policy.after_tool is not None:
                result = await policy.after_tool(
                    deepcopy(call), raw.model_copy(deep=True)
                )
            if not isinstance(result, ToolMessage) or (
                result.tool_call_id != (call["id"] or "")
                or result.name != call["name"]
                or result.type != "tool"
            ):
                raise ValueError("after_tool 改坏了工具结果的调用身份")
            result = ToolMessage.model_validate(result.model_dump())
            ref = artifacts.save("tool-result", result.model_dump(mode="json"))
            if ref is not None:
                span.set_attribute("deta.result_artifact", ref)
            metadata = result.artifact or {}
            outcome = (
                (metadata.get("error_code") or "error")
                if result.status == "error"
                else "success"
            )
            span.set_attribute(
                "deta.outcome",
                outcome,
            )
            span.set_attribute("deta.terminate", metadata.get("terminate", False))
            if result.status == "error":
                span.set_status(
                    Status(
                        StatusCode.ERROR,
                        outcome,
                    )
                )
            return result
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
