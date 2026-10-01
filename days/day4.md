# Day 4：固定工具调度与运行契约

[总览](summary.md) · [前一天](day3.md) · [下一天](day5.md)

## 核心问题

四个工具都已存在。模型返回 ToolCall 后，怎样查到正确函数、校验参数、调用它，并把带原调用 ID 的 ToolMessage 交回调用者？

今天一次提供完整 tools.py。工具表、异步 handler、输出回调、前后 Hook 和结果形状在此确定；后续写 Loop 时直接调用，不再先写一个 read 专用执行器再反复扩展。

## 今天安装什么

| 文件 | 内容与后续调用方 |
| --- | --- |
| `types.py` | 完整 RunOptions、RunResult、RunBudget 与 RunLimitError；请求和工具批次共用 |
| `events.py` | 完整事件形状，包括 tool_update.text；模型与后续 Loop 共用 |
| `hooks.py` | RequestPlan、TurnReport、Hooks、LoopBindings；先固定签名，运行时在 Day 7 绑定 |
| `tools.py` | 四工具注册、schema、参数校验、单工具执行、raw/final 结果记录 |

types.py 和 events.py 使用下方完整文件替换 Day 1 版本，hooks.py 与 tools.py 新建。这里只安装数据、回调和执行边界，不创建 Agent 或 Loop。Day 5 解释并接入请求预算，Day 6 使用工具预算，Day 7 组装这些契约。

## 一个完整调用怎样经过执行器

```text
同一个 ToolSpec
  ├─ args_model.model_json_schema() → 发给模型的 schema
  └─ args_model.model_validate(call["args"]) → 已验证参数
       → before_tool 决定是否放行
       → on_start 通知真正开始
       → await spec.handler(args, context)
       → ToolOutput → 带原调用 ID 的 raw ToolMessage
       → after_tool → 校验调用身份 → final ToolMessage
```

ToolSpec 保存类对象和函数对象，注册时不执行文件或命令。read/write/edit 在注册时经 async_file_handler 包装；bash 直接注册 run_bash。所有调度使用一个必需的 ToolContext，不保留第二套执行入口。

`registry` 是本次可执行工具的快照。之后 RequestPlan.tools 同时用于生成模型声明和实际查表，保证模型看到的工具与执行器允许的工具一致。`strict=False` 是提供方 schema 设置，本地参数模型的 `strict=True` 仍会拒绝字段错误与宽松类型转换。

## 先认识运行契约

| 对象 | 现在需要理解的内容 |
| --- | --- |
| RequestPlan | instructions、messages、tools 描述同一次请求 |
| TurnReport | 一轮已提交的助手响应、完整工具结果和历史快照 |
| RunBudget | 同一 Run 的实际请求尝试数与已预占工具次数 |
| BeforeToolDecision | allow、reason、terminate 描述工具前置决定 |
| Hooks | 六个可选控制回调；默认 Hooks() 全部关闭 |
| LoopBindings | 七个必需的运行操作；Day 7 用 AgentSession 的方法绑定 |

普通 Listener 用于显示和观测，它的返回值不控制执行。Hook 的返回值会改变计划、结果或决策，因此异常向上传播，不能当成一次普通日志失败吞掉。

## 直接提供的完整文件

先复制 types.py、events.py、hooks.py，再复制 tools.py。hooks.py 只在 TYPE_CHECKING 分支引用 ToolSpec，避免与 tools.py 形成运行时导入环。

### src/deta/types.py

<details>
<summary>直接提供：src/deta/types.py（完整文件）</summary>

```python
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field

# Any 仅用于 LangChain 的工具声明边界；实际工具参数仍由 Pydantic 校验。
type ToolSchema = dict[str, Any]


class Data(BaseModel):
    """Deta 业务数据的公共基类（消息直接使用 LangChain 类型），集中设置字段校验与冻结规则。
    子类负责声明具体业务字段，创建对象时由 Pydantic 校验这些字段。
    """

    # Pydantic 的类级配置：拒绝未声明字段，并禁止创建后重新赋值对象字段。
    model_config = ConfigDict(extra="forbid", frozen=True)


# 消息联合类型：用户输入、助手响应或工具结果，用于历史与请求边界的类型标注。
type AgentMessage = HumanMessage | AIMessage | ToolMessage


class RunOptions(Data):
    """保存一次 Agent 运行的资源限制，由入口组装后交给后续 Loop 使用。
    本类只定义限制数据，实际计数、超时和停止处理由运行控制代码完成。
    """

    # 一次 Run 的实际 SDK 尝试总额度，包括重试；在调用 SDK 前扣减。
    max_requests: int = Field(default=10, ge=1)
    # 一次 Run 允许的工具调用总数；设为零表示不给工具执行额度。
    max_tool_calls: int = Field(default=20, ge=0)
    # 整次 Run 的总时间额度，单位为秒，与单次模型请求超时分别管理。
    timeout_seconds: float = Field(default=120, gt=0)
    # 单个逻辑请求最多额外重试几次，同时受 max_requests 总尝试额度约束。
    max_retries: int = Field(default=2, ge=0, le=5)
    # 指数退避的初始等待秒数，等待也计入总运行时限。
    retry_delay_seconds: float = Field(default=0.25, ge=0)


class RunResult(Data):
    """汇总一次 Agent 运行结束后的状态、原因、回答和消息。
    由后续运行层创建并返回给 Python 调用方或命令入口。
    """

    # Deta 为本次任务运行生成的业务编号，用于关联事件和诊断产物。
    run_id: str
    # 运行终态：完成、取消、达到限制或失败；与模型 stop_reason 分开。
    status: Literal["completed", "cancelled", "limited", "failed"]
    # 说明为什么以当前状态结束，正常完成时可以为空。
    reason: str = ""
    # 返回给用户的最终回答文本；未得到回答时可以为空。
    answer: str = ""
    # 随运行结果返回的消息元组，供调用方查看本次产生或使用的对话内容。
    messages: tuple[AgentMessage, ...] = ()


class RunLimitError(Exception):
    """表示 Run 额度耗尽，保留停止原因交给 Agent 构造 limited 结果。"""


@dataclass
class RunBudget:
    """保存当前 Run 的可变计数，由 Loop 创建并交给请求与工具边界共同使用。"""

    # 当前 Run 的不可变额度配置。
    options: RunOptions
    # 已进入 SDK 调用边界的尝试次数，重试也计数。
    request_attempts: int = 0
    # 已放行进入调度的工具调用数量，参数被拒绝也占用调度额度。
    tool_calls: int = 0

    def take_request(self) -> int:
        """在真正开始 SDK 尝试前检查额度并计数，返回该 Run 内的尝试编号。"""
        if self.request_attempts >= self.options.max_requests:
            raise RunLimitError("实际模型请求尝试额度耗尽")
        self.request_attempts += 1
        return self.request_attempts

    def reserve_tools(self, count: int) -> None:
        """在开始本批工具调度前一次性预占全部额度，避免执行半批后才发现不够。"""
        if self.tool_calls + count > self.options.max_tool_calls:
            raise RunLimitError("工具调度额度耗尽")
        self.tool_calls += count
```

</details>

### src/deta/events.py

<details>
<summary>直接提供：src/deta/events.py（完整文件）</summary>

```python
import logging
from collections.abc import Callable, Sequence
from typing import Literal

from langchain_core.messages import AIMessage

from deta.types import Data

logger = logging.getLogger(__name__)


class TextDelta(Data):
    """表示模型流中新到达的一段正文，由 model.py 创建并通知监听器。
    界面可以立即显示增量，最终消息则由请求函数单独汇总。
    """

    # 事件类型标识，监听器用它区分正文增量与其他通知。
    kind: Literal["text_delta"] = "text_delta"
    # 本次新到达的正文片段，不是累计全文。
    text: str


class ToolCallDelta(Data):
    """表示某个工具调用新到达的参数片段，供观察者显示或记录进度。
    片段按本次响应内的 index 归组，完整拼接并校验之前不能用于执行工具。
    """

    # 事件类型标识，表示这是工具参数的流式增量。
    kind: Literal["tool_call_delta"] = "tool_call_delta"
    # 调用在当前模型响应中的槽位，同一 index 的参数片段需要拼到一起。
    index: int
    # 本次新到达的参数字符串片段，可能只是 JSON 的一部分。
    arguments_delta: str


class ModelDone(Data):
    """通知观察者本次模型响应已经完整收集，携带最终助手消息。
    请求函数还会将同一个最终结果返回给调用者，由调用者负责后续提交。
    """

    # 事件类型标识，表示本次模型流已收集为最终消息。
    kind: Literal["model_done"] = "model_done"
    # 完整的 AIMessage，供监听器读取正文、调用、用量和结束原因。
    message: AIMessage


# 模型流事件联合类型，区分正文增量、参数增量与完整响应通知。
type ModelEvent = TextDelta | ToolCallDelta | ModelDone


class AgentEvent(Data):
    """描述 Agent 运行、轮次、消息和工具处理过程中的一个通知。
    后续 Loop 在对应边界创建事件，监听器利用关联字段显示或记录过程。
    """

    # 发生的生命周期动作，例如运行开始、轮次结束或工具执行通知。
    kind: Literal[
        "run_start",
        "run_end",
        "turn_start",
        "turn_end",
        "message_update",
        "message_end",
        "tool_start",
        "tool_end",
        "tool_update",
    ]
    # 事件所属 Run 的业务编号，将同一次运行的通知关联起来。
    run_id: str
    # 事件所属的模型轮次；没有轮次语义的通知可以为 None。
    turn: int | None = None
    # 事件关联的工具调用编号；非工具事件通常为 None。
    tool_call_id: str | None = None
    # 当前动作的状态说明，是否填写及具体值由事件产生位置决定。
    status: str | None = None
    # 可选的模型流事件，用于把增量或最终响应包装进 Agent 通知。
    model_event: ModelEvent | None = None
    # 工具输出的本次增量；仅 tool_update 使用，不保存整段命令输出。
    text: str | None = None


# 观察者可接收的全部通知类型，既包含模型事件，也包含 Agent 生命周期事件。
type Event = ModelEvent | AgentEvent
# 监听函数的类型：接收一个 Event 并返回 None；传入的是函数对象。
type Listener = Callable[[Event], None]


def emit(event: Event, listeners: Sequence[Listener]) -> None:
    """把传入事件依次交给 listeners 中的观察函数，返回 None。
    模型边界或后续 Loop 调用它发送通知；普通监听器异常被隔离，通知不参与执行决策。
    """
    for listener in tuple(listeners):
        try:
            listener(event.model_copy(deep=True))
        except Exception as exc:
            logger.warning("listener failed: %s", type(exc).__name__)
```

</details>

### src/deta/hooks.py

<details>
<summary>直接提供：src/deta/hooks.py（完整文件）</summary>

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolCall, ToolMessage
from pydantic import BaseModel

from deta.events import Listener
from deta.types import AgentMessage, RunBudget

if TYPE_CHECKING:
    from deta.tools import ToolSpec


@dataclass(frozen=True)
class RequestPlan:
    """表示本次请求的准备结果，由会话编排层构造并交给 Loop。

    消息是当前请求的视图，工具表同时决定模型声明与本轮可执行工具。
    """

    # 本次请求的系统指令，不直接追加为用户历史。
    instructions: str
    # 本次准备发送的消息快照；变换该元组不会直接改写历史列表。
    messages: tuple[AgentMessage, ...]
    # 本轮工具定义快照；运行时用同一张表生成 schema 并执行调用。
    tools: Mapping[str, ToolSpec[Any]]


@dataclass(frozen=True)
class TurnReport:
    """在助手响应和配对工具结果全部提交后汇总一轮执行。

    finish_turn 接收这个对象，读取完整结果后决定是否继续。
    """

    # 当前 Run 中的模型轮次，从 1 开始。
    number: int
    # 本轮完整助手响应，已经进入历史。
    response: AIMessage
    # 本轮已提交的工具结果，按助手调用顺序排列。
    results: tuple[ToolMessage, ...]
    # 当前完整内存历史的快照，后续由 Session 提供事实来源。
    history: tuple[AgentMessage, ...]


# auto 表示遵循自然续轮规则；end 和 continue 表示明确的轮次决策。
type TurnDecision = Literal["auto", "end", "continue"]


@dataclass(frozen=True)
class BeforeToolDecision:
    """表示工具前置 Hook 的执行决策，不通过普通事件监听器隐式改变行为。"""

    # 是否允许进入工具 handler。
    allow: bool = True
    # 拒绝时写入工具结果的原因。
    reason: str = ""
    # 拒绝时附带的批次停止提示，仍需所有结果都要求终止才生效。
    terminate: bool = False


@dataclass(frozen=True)
class Hooks:
    """保存六个可选控制回调；未配置的位置使用 AgentSession 的默认行为。"""

    # 首次与后续每次请求都调用，可以替换本次请求计划。
    prepare_request: Callable[[RequestPlan], Awaitable[RequestPlan]] | None = None
    # 仅已完成一轮后调用，允许准备上下文并返回要先提交的用户消息。
    prepare_next_turn: (
        Callable[[TurnReport], Awaitable[tuple[HumanMessage, ...]]] | None
    ) = None
    # 在请求计划确定后调整消息副本，结果只用于本次请求。
    transform_context: (
        Callable[[tuple[AgentMessage, ...]], Awaitable[tuple[AgentMessage, ...]]] | None
    ) = None
    # 根据完整轮次报告显式继续或结束；异常会使 Run 失败。
    finish_turn: Callable[[TurnReport], Awaitable[TurnDecision]] | None = None
    # 已解析且有效的参数才到这里；回调读取参数副本，不能偷偷修改执行参数。
    before_tool: (
        Callable[[ToolCall, BaseModel], Awaitable[BeforeToolDecision]] | None
    ) = None
    # 每个普通结果都经过此处，包括拒绝和参数失败；允许修改正文与结果状态。
    after_tool: Callable[[ToolCall, ToolMessage], Awaitable[ToolMessage]] | None = None


@dataclass(frozen=True)
class LoopBindings:
    """把运行时提供的有限操作显式交给 Loop，避免 Loop 依赖存储和配置加载。

    这些是函数对象；创建本对象不执行请求、不提交消息，也不读取文件。
    """

    # 每次请求前调用，包含首次请求，返回本次消息与工具视图。
    prepare_request: Callable[[tuple[AgentMessage, ...]], Awaitable[RequestPlan]]
    # 在准备后转换请求视图，例如插入本次需要的上下文资料。
    transform_context: Callable[[RequestPlan], Awaitable[RequestPlan]]
    # 发起逻辑请求并管理重试；预算在实际 SDK 尝试边界消费。
    request: Callable[[RequestPlan, Listener, RunBudget], Awaitable[AIMessage]]
    # 执行一个完整调用；两个回调分别通知实际开始与输出增量。
    execute_tool: Callable[
        [ToolCall, RequestPlan, Callable[[], None], Callable[[str], None]],
        Awaitable[ToolMessage],
    ]
    # 保存一条最终消息；今天追加到 Agent 的内存列表，Day 8 接入持久化。
    commit: Callable[[AgentMessage], Awaitable[None]]
    # 完整轮次结束后返回决策；它是控制 Hook，不是普通事件监听器。
    finish_turn: Callable[[TurnReport], Awaitable[TurnDecision]]
    # 后续轮次开始前的准备，首次请求不调用。
    prepare_next_turn: Callable[[TurnReport], Awaitable[tuple[HumanMessage, ...]]]
```

</details>

### src/deta/tools.py

<details>
<summary>直接提供：src/deta/tools.py（完整文件）</summary>

```python
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
            span.set_attribute(
                "deta.outcome",
                metadata.get("error_code", None) or "success",
            )
            span.set_attribute("deta.terminate", metadata.get("terminate", False))
            if result.status == "error":
                span.set_status(
                    Status(
                        StatusCode.ERROR,
                        metadata.get("error_code", None),
                    )
                )
            return result
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
```

</details>

## 工具前后 Hook 的四条路径

| 路径 | before_tool | handler | after_tool |
| --- | --- | --- | --- |
| 未知名称 | 跳过 | 不执行 | 接收 unknown_tool |
| 参数无效 | 跳过 | 不执行 | 接收 invalid_arguments |
| 前置 Hook 拒绝 | 执行并返回拒绝 | 不执行 | 接收 denied |
| 校验并放行 | 执行 | 执行 | 接收成功或普通工具失败 |

before_tool 得到参数副本；改变这个副本不能偷偷改变实际执行参数。after_tool 得到 raw 的副本，可调整正文、错误状态和 terminate，但必须保留 role、name、tool_call_id。原始与最终结果分别保存，不能用脱敏后的 final 覆盖 raw 的事实。

输出长度截断、整批预算拒绝等 Loop 硬保护在调度前生成结果，不让工具 Hook 重新放行。Hook 本身异常直接使 Run 失败，取消也保持取消路径；后置 Hook 失败时工具可能已产生效果，因此不能自动重放。

## 错误结果与取消

| 情况 | 执行器的结果 |
| --- | --- |
| 未知工具、参数错误、Hook 拒绝 | 返回 unknown_tool、invalid_arguments、denied，不进入 handler |
| 可解释的文件或命令失败 | 返回对应错误 ToolMessage，保留原调用 ID |
| Hook、产物之外的内部错误 | 抛出异常，由调用者处理；不能冒充工具未执行 |
| 文件线程收到取消 | 等待线程收尾，再传播取消 |
| bash 收到取消 | 完成进程组及输出任务清理，再传播取消 |

工具结果不会自动进入对话历史；这是后续 Loop 的提交职责。`terminate=True` 也只是批次提示，Day 7 再决定是否继续，执行器不自行启动或结束 Run。

## 怎样核对

将本章代码写入实际源码后，再运行：

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
```

静态检查、真实工具调用和真实模型运行分别记录；本文中的代码与调用示例不代表已经完成运行验收。

在 `uv run python` 中使用真实已有文件核对调度入口：

```python
import asyncio
from pathlib import Path

from langchain_core.messages import ToolCall

from deta.builtin_tools import ToolContext
from deta.observability.artifacts import Artifacts
from deta.observability.tracing import local_tracing
from deta.tools import execute_tool

root = Path.cwd()
context = ToolContext(root, root / ".deta/manual-tool/output", "/bin/zsh", {}, print)
call = ToolCall(id="manual_read_1", name="read", args={"path": "target.md", "limit": 20})
with local_tracing(root / ".deta/manual-tool/spans.jsonl") as tracer:
    result = asyncio.run(
        execute_tool(
            call,
            context,
            tracer=tracer,
            artifacts=Artifacts(root / ".deta/manual-tool/artifacts"),
        )
    )
print(result.model_dump_json(indent=2))
```

这是手工发起的真实工具调用，不能据此说模型已自主选择工具。用同一入口核对未知名称、实际不存在的文件和字段错误；检查结果始终保留调用 ID。核对 bash 时由调用方显式加入项目需要的环境变量。

下一阶段 [Day 5：单次模型请求与运行预算](day5.md) 接通 SDK 尝试计数，仍不写主循环。
