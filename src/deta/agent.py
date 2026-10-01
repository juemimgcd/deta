import asyncio
from collections import deque
from collections.abc import Sequence
from dataclasses import replace
from typing import Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from opentelemetry.trace import Status, StatusCode, Tracer

from deta.events import AgentEvent, Listener, ModelEvent, emit
from deta.hooks import LoopBindings
from deta.loop import pending_calls, run_loop
from deta.types import AgentMessage, RunLimitError, RunOptions, RunResult


class InputQueue:
    """保存尚未提交历史的用户输入，选取后仍保留，提交成功才移出。"""

    def __init__(self, mode: Literal["one", "all"] = "one") -> None:
        """初始化消费模式与独立队列；入队不会立刻影响正在发送的请求。"""
        if mode not in {"one", "all"}:
            raise ValueError("队列模式必须是 one 或 all")
        # 每个正常轮询消费一条还是全部待处理消息。
        self.mode = mode
        # 已选取但尚未提交的消息也留在队首，取消或失败无需重新入队。
        self.items: deque[HumanMessage] = deque()

    def push(self, text: str) -> None:
        """校验非空输入并排队，留给下一次合适的调度边界消费。"""
        if not text.strip():
            raise ValueError("排队输入不能为空")
        self.items.append(HumanMessage(content=text))

    def peek(self) -> tuple[HumanMessage, ...]:
        """按当前模式选取一批消息，不改变队列；提交时逐条确认。"""
        if not self.items:
            return ()
        if self.mode == "all":
            return self.peek_all()
        return (self.items[0],)

    def peek_all(self) -> tuple[HumanMessage, ...]:
        """选取当前全部消息；之后新入队的消息不加入这批输入。"""
        return tuple(self.items)

    def acknowledge(self, message: AgentMessage) -> None:
        """仅移出已成功提交的队首对象，避免同文的普通输入或 Hook 消息误消费队列。"""
        if self.items and self.items[0] is message:
            self.items.popleft()


class Agent:
    """拥有活动任务、内存消息、临时流状态和两类输入队列。

    队列操作只安排未来输入；请求、工具、状态提交顺序仍由唯一 Loop 决定。
    """

    def __init__(
        self,
        bindings: LoopBindings,
        options: RunOptions,
        tracer: Tracer,
        listeners: Sequence[Listener] = (),
    ) -> None:
        """保存依赖并建立独立状态，构造对象不会启动运行或读取环境变量。"""
        # 与会话编排层绑定的有限操作。
        self.bindings = bindings
        # 每次新 Run 使用的预算和重试配置。
        self.options = options
        # 观测调用关系与时长的追踪对象。
        self.tracer = tracer
        # 按订阅顺序调用的普通观察者。
        self.listeners = list(listeners)
        # 当前唯一的内存事实列表，所有追加经过 commit。
        self.messages: list[AgentMessage] = []
        # 尚未形成最终消息的流式更新，运行结束后清空。
        self.partial: ModelEvent | None = None
        # 在工具批次完成等指定边界优先消费的指令队列。
        self.steering = InputQueue()
        # 自然准备结束时才消费的后续任务队列。
        self.followups = InputQueue()
        # 最近一次活动任务，保留完成结果供 wait 使用。
        self._task: asyncio.Task[RunResult] | None = None
        # 标识协程是否已经进入，支持刚启动便取消。
        self._entered = False
        # 已请求取消时不再重复取消清理中的任务。
        self._cancel_requested = False
        # 最终通知阶段不再接受新的取消请求，但 running 仍保持 True。
        self._finishing = False

    @property
    def running(self) -> bool:
        """返回本实例是否仍在运行或清理；只有任务真正完成才允许下次启动。"""
        return self._task is not None and not self._task.done()

    def subscribe(self, listener: Listener) -> None:
        """追加一个观察者，观察者的返回值不会改变执行决策。"""
        self.listeners.append(listener)

    def publish(self, event: AgentEvent) -> None:
        """更新临时流状态并通知外部，不额外保存另一份最终消息。"""
        if event.kind == "message_update":
            self.partial = event.model_event
        elif event.kind in {"message_end", "run_end"}:
            self.partial = None
        emit(event, self.listeners)

    def steer(self, text: str) -> None:
        """排入影响后续请求的新指令，当前正在执行的工具不会被此操作中断。"""
        self.steering.push(text)

    def follow_up(self, text: str) -> None:
        """排入当前任务自然结束后再处理的任务；显式 end 会保留它不消费。"""
        self.followups.push(text)

    def _ensure_idle(self) -> None:
        """在消费队列或安排任务之前检查活动状态与完整历史。"""
        if self.running:
            raise RuntimeError("Agent 已在运行，请排队或等待结束")
        asyncio.get_running_loop()
        if pending_calls(self.messages):
            raise ValueError("历史存在结果未知的工具，必须先明确处理，不能自动重放")

    def _schedule(
        self,
        initial: tuple[HumanMessage, ...],
        run_id: str | None,
        *,
        skip_initial_steering: bool = False,
    ) -> None:
        """在调用方完成合法性检查后同步预占任务，实际运行由 _drive 执行。"""
        self._entered = False
        self._cancel_requested = False
        self._finishing = False
        self._task = asyncio.create_task(
            self._drive(
                initial,
                run_id or uuid4().hex,
                skip_initial_steering,
            )
        )

    def start(self, prompt: str, *, run_id: str | None = None) -> None:
        """接受一条新的用户输入，安排 Run；与复用已有末尾输入的 continue_ 分开。"""
        self._ensure_idle()
        if not prompt.strip():
            raise ValueError("prompt 不能为空")
        self._schedule((HumanMessage(content=prompt),), run_id)

    def continue_(self, *, run_id: str | None = None) -> None:
        """从已有合法历史继续；助手末尾必须先取排队输入，空历史直接拒绝。"""
        self._ensure_idle()
        if not self.messages:
            raise ValueError("空历史不能继续；系统指令本身也不构成任务输入")
        if isinstance(self.messages[-1], AIMessage):
            selected = self.steering.peek_all()
            if selected:
                self._schedule(selected, run_id, skip_initial_steering=True)
                return
            selected = self.followups.peek_all()
            if not selected:
                raise ValueError("助手已经结束，continue_ 需要排队的新输入")
            self._schedule(selected, run_id)
            return
        self._schedule((), run_id)

    def _check_cancel(self) -> None:
        """让 Loop 在关键边界观察显式取消标记，禁止用普通继续决策覆盖取消。"""
        if self._cancel_requested:
            raise asyncio.CancelledError

    def abort(self) -> None:
        """取消活动任务一次，保持命令终止和文件线程收尾期间的运行占用。"""
        if not self.running or self._cancel_requested or self._finishing:
            return
        self._cancel_requested = True
        if self._entered and self._task is not None:
            self._task.cancel()

    async def wait(self) -> RunResult:
        """等待完整终态；调用方仅取消 wait 不会取消仍在运行的 Agent。"""
        if self._task is None:
            raise RuntimeError("尚未启动运行")
        return await asyncio.shield(self._task)

    async def _commit(self, message: AgentMessage) -> None:
        """可靠提交成功后同步确认队列消费，中间不增加取消点。"""
        await self.bindings.commit(message)
        self.steering.acknowledge(message)
        self.followups.acknowledge(message)

    async def _drive(
        self,
        initial: tuple[HumanMessage, ...],
        run_id: str,
        skip_initial_steering: bool,
    ) -> RunResult:
        """包住同一个 Loop，提交初始输入并在总时限与清理完成后产生 RunResult。"""
        self._entered = True
        begun = False
        result = RunResult(run_id=run_id, status="failed")
        with self.tracer.start_as_current_span(
            "deta.run", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("deta.run_id", run_id)
            try:
                await self.bindings.begin_run(run_id)
                begun = True
                self.publish(AgentEvent(kind="run_start", run_id=run_id))
                self._check_cancel()
                async with asyncio.timeout(self.options.timeout_seconds):
                    for message in initial:
                        await self._commit(message)
                    answer, reason = await run_loop(
                        self.messages,
                        replace(self.bindings, commit=self._commit),
                        self.options,
                        run_id=run_id,
                        publish=self.publish,
                        tracer=self.tracer,
                        take_steering=self.steering.peek,
                        take_followups=self.followups.peek,
                        check_cancel=self._check_cancel,
                        skip_initial_steering=skip_initial_steering,
                    )
                await asyncio.sleep(0)
                self._check_cancel()
                result = RunResult(
                    run_id=run_id, status="completed", answer=answer, reason=reason
                )
            except asyncio.CancelledError:
                result = RunResult(run_id=run_id, status="cancelled", reason="请求取消")
            except (RunLimitError, TimeoutError) as exc:
                reason = (
                    str(exc) if isinstance(exc, RunLimitError) else "运行或请求超时"
                )
                result = RunResult(run_id=run_id, status="limited", reason=reason)
            except Exception as exc:
                result = RunResult(
                    run_id=run_id, status="failed", reason=type(exc).__name__
                )
            finally:
                self._finishing = True
                self.partial = None
                if begun:
                    try:
                        await self.bindings.end_run(result)
                    except Exception as exc:
                        result = result.model_copy(
                            update={
                                "status": "failed"
                                if result.status == "completed"
                                else result.status,
                                "reason": f"{result.reason}; 终态保存失败：{type(exc).__name__}",
                            }
                        )
                span.set_attribute("deta.outcome", result.status)
                if result.status != "completed":
                    span.set_status(Status(StatusCode.ERROR, result.reason))
                self.publish(
                    AgentEvent(kind="run_end", run_id=run_id, status=result.status)
                )
        return result.model_copy(update={"messages": tuple(self.messages)})
