import asyncio
from collections.abc import Callable, Sequence

from langchain_core.messages import AIMessage, HumanMessage, ToolCall, ToolMessage
from opentelemetry.trace import Status, StatusCode, Tracer

from deta.events import AgentEvent, Event, ModelDone
from deta.hooks import LoopBindings, RequestPlan, TurnReport
from deta.types import (
    AgentMessage,
    RebuildRequest,
    RunBudget,
    RunLimitError,
    RunOptions,
)


def pending_calls(messages: Sequence[AgentMessage]) -> dict[str, str]:
    """检查历史顺序与工具配对，返回尚欠结果的调用 ID 到工具名的映射。

    Agent 在接收新输入时使用返回值；Loop 在请求前要求映射为空。
    错配结果、重复调用编号或在未配对时插入普通消息会直接失败。
    """
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            if pending.get(message.tool_call_id) != message.name:
                raise ValueError("历史中的工具结果无法配对")
            del pending[message.tool_call_id]
            continue
        if pending:
            raise ValueError("工具结果尚未配齐就插入了普通消息")
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if (
                    not (call["id"] or "").strip()
                    or not call["name"].strip()
                    or (call["id"] or "") in seen
                ):
                    raise ValueError("历史中存在空白或重复的工具调用编号")
                seen.add((call["id"] or ""))
                pending[(call["id"] or "")] = call["name"]
    return pending


def failed_result(call: ToolCall, code: str, reason: str) -> ToolMessage:
    """为不能执行的调用构造失败消息，把原调用 ID 和名称完整交回 Loop。

    用于截断响应或整批额度不足等分支，此函数不会执行工具。
    """
    return ToolMessage(
        tool_call_id=call["id"] or "",
        name=call["name"],
        content=reason,
        status="error" if code else "success",
        artifact={"error_code": code},
    )


async def execute_tool_batch(
    response: AIMessage,
    plan: RequestPlan,
    bindings: LoopBindings,
    options: RunOptions,
    budget: RunBudget,
    *,
    run_id: str,
    turn: int,
    publish: Callable[[AgentEvent], None],
    check_cancel: Callable[[], None],
) -> list[ToolMessage]:
    """预占整批预算，按序执行并提交配对结果；整批结清后再报告额度或截断。"""
    calls = response.tool_calls
    results: list[ToolMessage] = []
    blocked = budget.token_stop_reason
    if calls and budget.request_attempts >= options.max_requests:
        blocked = "没有剩余请求额度消费工具结果"
    if (
        calls
        and response.response_metadata.get("finish_reason") == "tool_calls"
        and not blocked
    ):
        try:
            budget.reserve_tools(len(calls))
        except RunLimitError as exc:
            blocked = str(exc)
    for call in calls:
        check_cancel()
        if blocked:
            result = failed_result(call, "budget_exhausted", blocked)
        elif response.response_metadata.get("finish_reason") != "tool_calls":
            result = failed_result(
                call,
                "incomplete_response",
                "助手响应被截断或过滤，未执行本次调用，请重新提供完整参数。",
            )
        else:

            def on_start(call_id: str = (call["id"] or "")) -> None:
                """在参数与前置 Hook 放行之后通知真正的 handler 开始。"""
                publish(
                    AgentEvent(
                        kind="tool_start",
                        run_id=run_id,
                        turn=turn,
                        tool_call_id=call_id,
                        data={"name": call["name"], "arguments": call["args"]},
                    )
                )

            def on_output(text: str, call_id: str = (call["id"] or "")) -> None:
                """给输出增量附上调用身份；增量只用于观察，不独立提交历史。"""
                publish(
                    AgentEvent(
                        kind="tool_update",
                        run_id=run_id,
                        turn=turn,
                        tool_call_id=call_id,
                        text=text,
                    )
                )

            result = await bindings.execute_tool(call, plan, on_start, on_output)
        await bindings.commit(result)
        results.append(result)
        publish(
            AgentEvent(
                kind="tool_end",
                run_id=run_id,
                turn=turn,
                tool_call_id=(call["id"] or ""),
                status=((result.artifact or {}).get("error_code") or "error")
                if result.status == "error"
                else "success",
                data={"name": call["name"], "output": result.text},
            )
        )
    check_cancel()
    if blocked:
        raise RunLimitError(blocked)
    if (
        response.response_metadata.get("finish_reason") in {"length", "content_filter"}
        and not calls
    ):
        raise RunLimitError(
            f"模型未完整回答：{response.response_metadata.get('finish_reason')}"
        )
    return results


async def run_loop(
    messages: list[AgentMessage],
    bindings: LoopBindings,
    options: RunOptions,
    *,
    run_id: str,
    publish: Callable[[AgentEvent], None],
    tracer: Tracer,
    take_steering: Callable[[], tuple[HumanMessage, ...]],
    take_followups: Callable[[], tuple[HumanMessage, ...]],
    check_cancel: Callable[[], None],
    skip_initial_steering: bool = False,
) -> tuple[str, str]:
    """以同一套内外循环处理工具续轮、Steering、Follow-up 和显式继续。

    内层处理工具与 Steering，外层只在自然停止时取 Follow-up 或履行一次显式继续。
    所有消息提交仍走 bindings.commit，错误与取消不会被 finish_turn 的返回值覆盖。
    队列回调只选取消息；bindings.commit 成功后才确认消费。
    """
    budget = RunBudget(options)
    pending = () if skip_initial_steering else take_steering()
    last: TurnReport | None = None
    explicit = False
    turn = 0
    while True:
        has_tools = True
        while has_tools or pending:
            check_cancel()
            prepared: tuple[HumanMessage, ...] = ()
            if last is not None:
                prepared = await bindings.prepare_next_turn(last)
                check_cancel()
                # 如果前次轮询已经选到消息，就不能再取一次，否则 one 模式会变成两条。
                if not pending:
                    pending = take_steering()
            turn += 1
            with tracer.start_as_current_span(
                "deta.turn", record_exception=False, set_status_on_exception=False
            ) as span:
                span.set_attribute("deta.turn", turn)
                publish(AgentEvent(kind="turn_start", run_id=run_id, turn=turn))
                turn_status = "failed"
                try:
                    for message in (*prepared, *pending):
                        await bindings.commit(message)
                    pending = ()

                    def on_model(event: Event) -> None:
                        """只转发流式更新，最终消息在 commit 成功后才发布 message_end。"""
                        if not isinstance(event, (AgentEvent, ModelDone)):
                            publish(
                                AgentEvent(
                                    kind="message_update",
                                    run_id=run_id,
                                    turn=turn,
                                    model_event=event,
                                )
                            )

                    budget.overflow_recovery_used = False
                    for rebuild in range(3):
                        plan = await bindings.prepare_request(tuple(messages))
                        plan = await bindings.transform_context(plan)
                        check_cancel()
                        if pending_calls(plan.messages):
                            raise ValueError("本次请求仍缺少工具结果")
                        try:
                            response = await bindings.request(plan, on_model, budget)
                            break
                        except RebuildRequest:
                            check_cancel()
                            if rebuild == 2:
                                raise RunLimitError("本次请求重建次数耗尽") from None
                    check_cancel()
                    # 新响应的编号也要与已提交历史兼容，验证后才允许提交和执行。
                    pending_calls((*messages, response))
                    await bindings.commit(response)
                    publish(
                        AgentEvent(
                            kind="message_end",
                            run_id=run_id,
                            turn=turn,
                            model_event=ModelDone(message=response),
                        )
                    )
                    results = await execute_tool_batch(
                        response,
                        plan,
                        bindings,
                        options,
                        budget,
                        run_id=run_id,
                        turn=turn,
                        publish=publish,
                        check_cancel=check_cancel,
                    )
                    last = TurnReport(turn, response, tuple(results), tuple(messages))
                    decision = await bindings.finish_turn(last)
                    check_cancel()
                    if decision not in {"auto", "end", "continue"}:
                        raise TypeError("finish_turn 返回了非法决策")
                    turn_status = "completed"
                except BaseException as exc:
                    turn_status = (
                        "cancelled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "failed"
                    )
                    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                    raise
                finally:
                    span.set_attribute("deta.outcome", turn_status)
                    publish(
                        AgentEvent(
                            kind="turn_end",
                            run_id=run_id,
                            turn=turn,
                            status=turn_status,
                        )
                    )
            check_cancel()
            if decision == "end":
                # 显式结束在消费任何下一批队列之前生效，尚未取出的输入留在队列中。
                return response.text, "hook_end"
            explicit = decision == "continue"
            has_tools = bool(results) and not all(
                (result.artifact or {}).get("terminate", False) for result in results
            )
            pending = take_steering()
            if has_tools or pending:
                explicit = False
        pending = take_followups()
        if pending:
            explicit = False
            continue
        if explicit:
            explicit = False
            continue
        return response.text, "tool_terminated" if results else "answer"
