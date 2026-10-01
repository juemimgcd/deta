# Day 6：历史配对与整批工具执行

[总览](summary.md) · [前一天](day5.md) · [下一天](day7.md)

## 核心问题

模型一次返回两个工具调用，但剩余额度只够执行一个，能先执行第一个再停止吗？不能这样处理，否则会留下半批执行和缺失结果。主 Loop 之前，先固定历史配对与整批执行规则。

本章新建 loop.py，只放 pending_calls、failed_result、execute_tool_batch 三个完整辅助函数。它们复用 Day 4 的执行器和 Day 5 的请求预算约定；Day 7 只追加 run_loop，三个函数的签名和函数体不再改写。

## 三个函数各自处理什么

| 函数 | 输入与返回 |
| --- | --- |
| pending_calls | 消息序列 → 尚欠结果的调用 ID/名称；顺序错误、重复 ID 或错配结果直接失败 |
| failed_result | 原调用、失败码、原因 → 带原调用身份的 ToolMessage；不执行工具 |
| execute_tool_batch | 完整 AIMessage、RequestPlan、绑定与预算 → 按调用顺序提交的结果列表 |

```text
主 Loop 先提交完整 AIMessage
  → execute_tool_batch
      → 检查是否还有下一次请求额度来消费结果
      → 对 finish_reason=tool_calls 的整批预占工具额度
      → 每个调用前检查取消
      → 不可执行：生成配对失败结果
        可执行：调用 bindings.execute_tool
      → commit(ToolMessage) 成功后发布 tool_end
      → 全批结清后返回，或报告硬额度停止
```

on_start 由单工具执行器在校验和前置 Hook 放行后调用；on_output 只发布增量。结果提交与工具执行是两个步骤，事件不能代替提交。

## 完整辅助代码

直接放入下面文件，理解调用顺序后再进入主循环练习。这里没有 Agent 或 runtime 导入，三个函数可以独立导入；LoopBindings 只是参数里的回调契约。

### src/deta/loop.py

<details>
<summary>直接提供：src/deta/loop.py（完整文件）</summary>

```python
from collections.abc import Callable, Sequence

from langchain_core.messages import AIMessage, ToolCall, ToolMessage

from deta.events import AgentEvent
from deta.hooks import LoopBindings, RequestPlan
from deta.types import AgentMessage, RunBudget, RunLimitError, RunOptions


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
    blocked = ""
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
                status=(result.artifact or {}).get("error_code", None) or "success",
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
```

</details>

## 沿一批调用检查结果

以实际完整响应中的调用顺序为准。对于两个调用，结果也按同样顺序逐个提交；tool_call_id 来自各自调用，不能用工具名替代。

| 条件 | 行为 |
| --- | --- |
| 整批工具额度不足 | 全批生成 budget_exhausted 结果，结清后抛 RunLimitError |
| 没有下一次请求额度消费结果 | 不开始工具，结清本批并停止 |
| 响应截断或被过滤 | 为仍可配对的调用生成 incomplete_response，不进入 handler |
| 名称或字段错误、工具普通失败 | 由 Day 4 返回 ToolMessage，正常提交 |
| 提交失败、内部错误或取消 | 向 Agent 传播；是否恢复由 Day 8 的持久化机制处理 |

不能把尚未执行的调用标为成功，也不能在预算拒绝后只保存一半结果。取消或内部故障中断批次时，内存历史可能仍有 pending 调用；Day 7 的 continue_ 必须拒绝自动重放，Day 8 才接入可靠恢复。

## 与主 Loop 的分工

execute_tool_batch 只执行和结清当前批次，不取 Steering/Follow-up，不调用 finish_turn，也不请求下一次模型。主 Loop 在完整结果提交后再作结束或继续决定。

ToolOutput.terminate 在 Day 3 已存在。Day 7 只需读取最终 ToolMessage.artifact 中的同一字段：非空整批全部要求终止，才停止工具引起的自然续轮。它不会覆盖用户显式取消或 finish_turn=end。

## 怎样核对

将本章代码写入实际源码后，再运行：

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
```

静态检查、真实工具调用和真实模型运行分别记录；本文中的代码与调用示例不代表已经完成运行验收。

本章的检查重点是历史顺序、预算先于执行、结果先提交再通知，以及异常传播位置。已有真实响应时沿调用 ID 核对；没有实际响应时保留待验收，不构造虚假模型记录。完整 Run 的行为在下一章一起验证。

下一阶段 [Day 7：最后组装 Agent 与主 Loop](day7.md) 使用上述固定函数完成首次端到端运行。
