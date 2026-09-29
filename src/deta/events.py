import logging
from collections.abc import Callable, Sequence
from typing import Literal

from deta.types import AssistantMessage, Data

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
    # 完整的 AssistantMessage，供监听器读取正文、调用、用量和结束原因。
    message: AssistantMessage


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
            listener(event)
        except Exception as exc:
            logger.warning("listener failed: %s", type(exc).__name__)