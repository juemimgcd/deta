from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolCall, ToolMessage
from pydantic import BaseModel

from deta.events import Listener
from deta.types import AgentMessage, RunBudget, RunResult

if TYPE_CHECKING:
    from deta.context import ContextItem
    from deta.tools import ToolSpec


@dataclass(frozen=True)
class RequestPlan:
    """表示本次请求的准备结果，由会话编排层构造并交给 Loop。

    消息是当前请求的视图，工具表同时决定模型声明与本轮可执行工具。
    """

    # 本次请求的系统指令，不直接追加为用户历史。
    instructions: str
    # 本次请求的消息与显式来源；Hook 保留原项，新增或改写时创建 source=hook 的项。
    context_items: tuple[ContextItem, ...]
    # 本轮工具定义快照；运行时用同一张表生成 schema 并执行调用。
    tools: Mapping[str, ToolSpec[Any]]
    # 构建请求时读取的会话末尾条目，用于定位输入对应的历史快照。
    context_tip: str | None = None
    # 本次资源来源及内容版本；指令正文仍以最终 instructions 为准。
    resource_sources: tuple[tuple[str, str], ...] = ()
    # 构建视图时没有进入请求的条目与原因；原记录不删除。
    excluded_entries: tuple[tuple[str, str], ...] = ()

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        """仅在协议检查或模型边界按顺序提取消息，不再维护第二份状态。"""
        return tuple(item.message for item in self.context_items)


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
        Callable[[tuple[ContextItem, ...]], Awaitable[tuple[ContextItem, ...]]] | None
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
    # 先可靠保存最终消息，再更新 Agent 的内存列表。
    commit: Callable[[AgentMessage], Awaitable[None]]
    # 完整轮次结束后返回决策；它是控制 Hook，不是普通事件监听器。
    finish_turn: Callable[[TurnReport], Awaitable[TurnDecision]]
    # 后续轮次开始前的准备，首次请求不调用。
    prepare_next_turn: Callable[[TurnReport], Awaitable[tuple[HumanMessage, ...]]]
    # Agent 在提交首条消息前保存 Run 身份；这是必需的持久化边界。
    begin_run: Callable[[str], Awaitable[None]]
    # Agent 在工具收尾后保存终态；完成前仍保持运行占用。
    end_run: Callable[[RunResult], Awaitable[None]]
