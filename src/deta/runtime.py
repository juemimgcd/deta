import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from types import MappingProxyType

from langchain_core.messages import AIMessage, HumanMessage, ToolCall, ToolMessage
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APIStatusError
from opentelemetry.trace import Status, StatusCode, Tracer, get_current_span
from pydantic import JsonValue, TypeAdapter

from deta.agent import Agent
from deta.builtin_tools import ToolContext
from deta.compaction import CompactionOutcome, generate_compaction, prepare_compaction
from deta.context import (
    ContextEstimate,
    build_context,
    estimate_context,
    input_fingerprint,
    resolve_retained_tail,
    validate_context_items,
)
from deta.events import Event, Listener, TextDelta, ToolCallDelta
from deta.hooks import Hooks, LoopBindings, RequestPlan, TurnDecision, TurnReport
from deta.loop import pending_calls
from deta.model import ModelBoundary, ModelConfig, stream_once
from deta.observability.artifacts import Artifacts, source_version
from deta.resources import ResourceBundle, load_resources, render_resources
from deta.session import Session
from deta.tools import TOOLS, execute_tool, tool_schemas
from deta.types import (
    AgentMessage,
    RebuildRequest,
    RunBudget,
    RunLimitError,
    RunOptions,
    RunResult,
    ToolSchema,
)


def copy_report(report: TurnReport) -> TurnReport:
    """复制报告中的消息对象，避免 Hook 修改嵌套诊断字段时影响已提交历史。"""
    return replace(
        report,
        response=report.response.model_copy(deep=True),
        results=tuple(item.model_copy(deep=True) for item in report.results),
        history=tuple(item.model_copy(deep=True) for item in report.history),
    )


def retryable(exc: Exception) -> bool:
    """只将连接错误、请求超时、408/429 和服务端 5xx 视为可考虑重试的错误。"""
    if isinstance(exc, (APIConnectionError, TimeoutError)):
        return True
    return isinstance(exc, APIStatusError) and (
        exc.status_code in {408, 429} or 500 <= exc.status_code <= 599
    )


def is_context_overflow(exc: Exception) -> bool:
    """仅识别当前提供方的结构化上下文溢出码；其他 400 错误正常失败。"""
    if not isinstance(exc, APIStatusError) or exc.status_code != 400:
        return False
    body = exc.body
    if not isinstance(body, dict):
        return False
    error = body.get("error", body)
    return isinstance(error, dict) and error.get("code") == "context_length_exceeded"


@dataclass(frozen=True)
class ModelInput:
    """一次准备得到的输入；同一逻辑请求的重试复用此快照。"""

    schemas: list[ToolSchema]
    current_tools: dict[str, str]
    changes: dict[str, list[str]]
    instructions: str
    messages: tuple[AgentMessage, ...]
    estimate: ContextEstimate
    fingerprint: str
    sources: JsonValue


def prepare_model_input(
    plan: RequestPlan,
    last_tools: Mapping[str, str],
    config: ModelConfig,
    session_id: str,
    context_window: int,
    context_margin: int,
) -> ModelInput:
    """构造最终模型输入及诊断信息；不请求模型，不改变运行状态。"""
    messages = plan.messages
    schemas = tool_schemas(plan.tools)
    current = {
        name: json.dumps(schema, sort_keys=True, ensure_ascii=False)
        for name, schema in zip(plan.tools, schemas)
    }
    changes = {
        "added": sorted(current.keys() - last_tools.keys()),
        "removed": sorted(last_tools.keys() - current.keys()),
        "updated": sorted(
            name
            for name in current.keys() & last_tools.keys()
            if current[name] != last_tools[name]
        ),
    }
    instructions = plan.instructions
    if any(changes.values()):
        instructions += "\n本次可用工具变化：" + json.dumps(changes, ensure_ascii=False)
    estimate = estimate_context(
        messages,
        model=config.model,
        instructions=instructions,
        tools=schemas,
        window_tokens=context_window,
        output_tokens=config.max_completion_tokens,
        safety_tokens=context_margin,
    )
    fingerprint = input_fingerprint(config.model, instructions, messages, schemas)
    sources: JsonValue = TypeAdapter(JsonValue).validate_python(
        {
            "session_id": session_id,
            "context_tip": plan.context_tip,
            "system": "runtime, scoped resources, request Hook and tool-change notice",
            "resources": [list(item) for item in plan.resource_sources],
            "messages": [
                {
                    "provider_index": index + 1,
                    "entry_ids": list(item.entry_ids),
                    "source": item.source,
                    "note": item.note,
                }
                for index, item in enumerate(plan.context_items)
            ],
            "excluded_entries": [list(pair) for pair in plan.excluded_entries],
            "budget": estimate.model_dump(mode="json"),
        }
    )
    return ModelInput(
        schemas,
        current,
        changes,
        instructions,
        messages,
        estimate,
        fingerprint,
        sources,
    )


class AgentSession:
    """绑定模型、工具、Hooks 和消息提交，让评测与普通调用以后复用同一个 Agent。"""

    def __init__(
        self,
        client: ChatOpenAI | None,
        config: ModelConfig,
        workspace: Path,
        tracer: Tracer,
        artifacts: Artifacts,
        *,
        session: Session,
        context_window: int,
        context_margin: int = 1024,
        keep_recent_tokens: int = 4096,
        summary_output_tokens: int = 1024,
        instructions: str,
        shell: str = "/bin/zsh",
        environment: Mapping[str, str] | None = None,
        options: RunOptions | None = None,
        listeners: Sequence[Listener] = (),
        hooks: Hooks | None = None,
    ) -> None:
        """保存外部依赖与控制回调；核心对象不会在导入或构造时请求模型。"""
        # 调用方负责关闭的 SDK 客户端，必须关闭其内部重试。
        self.client = client
        self.model_call: ModelBoundary = self._model_once
        # 单次模型请求参数。
        self.config = config
        # 工具路径与命令 cwd 的共同基准。
        self.workspace = workspace.resolve(strict=True)
        # 与入口观测环境绑定的 tracer。
        self.tracer = tracer
        # 请求、原始结果与最终结果的诊断采集器。
        self.artifacts = artifacts
        # 每次重建请求计划都会安装的系统指令。
        self.instructions = instructions
        # 命令工具所用 shell。
        self.shell = shell
        # 从调用方复制的命令环境，不保存到 Trace 正文。
        self.environment = dict(environment or {})
        # 下一次请求的默认可用工具集合；实际请求会再冻结副本。
        self.tools = dict(TOOLS)
        # 六个有限 Hook 的配置，默认全部为空。
        self.hooks = hooks or Hooks()
        # 最近一次成功响应使用的工具 schema，用于声明下次请求的工具变化。
        self._last_tools: dict[str, str] = {}
        # 持久化事实来源；由调用方打开，运行时不自行选择数据库。
        self.session = session
        # 所选模型的窗口大小，由调用方明确给出，不按模型名字猜测。
        self.context_window = context_window
        # 为分词差异和提供方额外开销留下的估算余量。
        self.context_margin = context_margin
        if (
            context_margin < 0
            or context_window <= config.max_completion_tokens + context_margin
        ):
            raise ValueError("模型窗口不足以容纳输出预留与估算余量")
        if keep_recent_tokens <= 0 or summary_output_tokens <= 0:
            raise ValueError("压缩保留目标和摘要输出额度必须为正")
        self.keep_recent_tokens = keep_recent_tokens
        self.summary_output_tokens = summary_output_tokens
        self._active_skills: tuple[str, ...] = ()
        # 每次 Run 开始时读取一次；本次请求与自动压缩共享同一资源版本。
        self._resources: ResourceBundle | None = None
        self._maintenance = False
        self._threshold_tips: set[str] = set()
        self._failure_signature = ""
        self._failure_count = 0
        self.usage_stats: dict[str, int] = {}
        # 活动运行、临时消息视图与队列所有者；最终历史由 Session 保存。
        self.agent = Agent(
            LoopBindings(
                prepare_request=self._prepare_request,
                transform_context=self._transform_context,
                request=self._request,
                execute_tool=self._execute_tool,
                commit=self._commit,
                finish_turn=self._finish_turn,
                prepare_next_turn=self._prepare_next_turn,
                begin_run=self._begin_run,
                end_run=self._end_run,
            ),
            options or RunOptions(),
            tracer,
            listeners,
        )

    async def prompt(self, text: str, *, run_id: str | None = None) -> RunResult:
        """接受新的用户输入并等待完整 Run 结果。"""
        self._reload()
        self.agent.start(text, run_id=run_id)
        return await self._wait_for_run()

    async def continue_(self, *, run_id: str | None = None) -> RunResult:
        """继续合法历史或助手末尾的排队输入，不把结果未知的工具自动重放。"""
        self._reload()
        self.agent.continue_(run_id=run_id)
        return await self._wait_for_run()

    async def _wait_for_run(self) -> RunResult:
        """等待已有任务；入口取消时先终止 Agent 并等待收尾，再传播取消。"""
        try:
            return await self.agent.wait()
        except asyncio.CancelledError:
            # 公共任务入口被取消时先结束 Agent，保持客户端等依赖直到清理完成。
            self.agent.abort()
            await self.agent.wait()
            raise

    def _reload(self) -> None:
        """只在空闲时恢复未结清记录，再从数据库重建同一个 Agent 消息列表。"""
        if self.agent.running or self._maintenance:
            raise RuntimeError("Agent 仍在运行或收尾")
        with self.tracer.start_as_current_span(
            "deta.session.recover",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("deta.session_id", self.session.id)
            try:
                count = self.session.recover()
                self.agent.messages[:] = self.session.messages()
                span.set_attribute("deta.recovered_tools", count)
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise

    async def _begin_run(self, run_id: str) -> None:
        """登记运行身份及可复查配置，不把模型密钥或命令环境保存到数据库。"""
        get_current_span().set_attribute("deta.session_id", self.session.id)
        config: JsonValue = TypeAdapter(JsonValue).validate_python(
            {
                "model": self.config.model,
                "max_completion_tokens": self.config.max_completion_tokens,
                "workspace": str(self.workspace),
                "instructions": self.instructions,
                "tools": tool_schemas(self.tools),
                "context_window": self.context_window,
                "context_margin": self.context_margin,
            }
        )
        self._threshold_tips.clear()
        self._failure_signature = ""
        self._failure_count = 0
        self.usage_stats = {}
        # 先读资源再登记 Run；加载失败时不留下无法收尾的 running 记录。
        self._resources = None
        resources = load_resources(self.workspace, self._active_skills)
        self.session.start_run(run_id, config)
        self._resources = resources
        # 诊断失败不回滚已成功登记的 Run；这里只记录不含正文和秘密的清单。
        try:
            manifest = {
                "run_id": run_id,
                "session_id": self.session.id,
                "source_sha256": source_version(Path(__file__).parent),
                "model": self.config.model,
                "max_completion_tokens": self.config.max_completion_tokens,
                "instructions_sha256": hashlib.sha256(
                    self.instructions.encode()
                ).hexdigest(),
                "schemas_sha256": hashlib.sha256(
                    json.dumps(tool_schemas(self.tools), sort_keys=True).encode()
                ).hexdigest(),
                "resources": [list(item) for item in resources.versions],
                "active_skills": list(self._active_skills),
                "capture_body": self.artifacts.capture_body,
                "format_version": 1,
            }
            self.artifacts.metadata(
                "manifest", TypeAdapter(JsonValue).validate_python(manifest)
            )
        except Exception:
            self.artifacts.failed += 1

    async def _end_run(self, result: RunResult) -> None:
        """在 Agent 结束通知前保存终态；失败会改变向调用方返回的运行结果。"""
        self.session.finish_run(result)
        self.artifacts.metadata(
            "capture-status",
            {
                "run_id": result.run_id,
                "usage": dict(self.usage_stats),
                "saved": self.artifacts.saved,
                "skipped": self.artifacts.skipped,
                "failed": self.artifacts.failed,
                "redacted_strings": self.artifacts.redacted,
                "trace_delivery": "not_guaranteed; inspect exporter warnings and span completeness",
            },
        )

    async def _prepare_request(self, messages: tuple[AgentMessage, ...]) -> RequestPlan:
        """从 Session 构建视图，再应用请求 Hook；Agent 的列表只用于活动状态和协议检查。"""
        with self.tracer.start_as_current_span(
            "deta.context.build",
            record_exception=False,
            set_status_on_exception=False,
        ) as stage_span:
            try:
                view = build_context(self.session.entries())
                resources = self._resources
                if resources is None:
                    raise RuntimeError("本次 Run 尚未加载资源快照")
            except BaseException as exc:
                stage_span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
        plan = RequestPlan(
            self.instructions + "\n\n" + render_resources(resources),
            tuple(item.model_copy(deep=True) for item in view.items),
            MappingProxyType(dict(self.tools)),
        )
        if self.hooks.prepare_request is not None:
            plan = await self.hooks.prepare_request(plan)
        if not isinstance(plan, RequestPlan):
            raise TypeError("prepare_request 必须返回 RequestPlan")
        if any(name != spec.name for name, spec in plan.tools.items()):
            raise ValueError("工具表键与 ToolSpec.name 不一致")
        items, missing = validate_context_items(
            view.items, plan.context_items, "prepare_request"
        )
        return replace(
            plan,
            tools=MappingProxyType(dict(plan.tools)),
            context_items=items,
            context_tip=view.tip_id,
            resource_sources=resources.versions,
            excluded_entries=(*view.excluded, *missing),
        )

    async def _transform_context(self, plan: RequestPlan) -> RequestPlan:
        """转换本次消息副本并同步来源；完整工具配对仍由原有 Loop 在请求边界检查。"""
        if self.hooks.transform_context is None:
            return plan
        transformed = await self.hooks.transform_context(
            tuple(item.model_copy(deep=True) for item in plan.context_items)
        )
        items, missing = validate_context_items(
            plan.context_items, transformed, "transform_context"
        )
        return replace(
            plan,
            context_items=items,
            excluded_entries=(*plan.excluded_entries, *missing),
        )

    async def _prepare_next_turn(self, report: TurnReport) -> tuple[HumanMessage, ...]:
        """后续轮次才调用准备 Hook，返回先于本批队列消息提交的用户输入。"""
        if self.hooks.prepare_next_turn is None:
            return ()
        messages = await self.hooks.prepare_next_turn(copy_report(report))
        if not isinstance(messages, tuple) or any(
            not isinstance(item, HumanMessage) for item in messages
        ):
            raise TypeError("prepare_next_turn 必须返回 HumanMessage 元组")
        return messages

    async def _request(
        self,
        plan: RequestPlan,
        listener: Listener,
        budget: RunBudget,
    ) -> AIMessage:
        """为一个逻辑请求管理有限重试，所有尝试复用相同输入且由 SDK 边界计数。"""
        prepared = prepare_model_input(
            plan,
            self._last_tools,
            self.config,
            self.session.id,
            self.context_window,
            self.context_margin,
        )
        with self.tracer.start_as_current_span(
            "deta.model.request", record_exception=False, set_status_on_exception=False
        ) as span:
            signature = hashlib.sha256(
                json.dumps(prepared.current_tools, sort_keys=True).encode()
            ).hexdigest()
            span.set_attribute("deta.tools_hash", signature)
            for key, names in prepared.changes.items():
                span.set_attribute(f"deta.tools.{key}", names)
            span.set_attribute(
                "deta.context.tokens_estimated", prepared.estimate.tokens
            )
            span.set_attribute(
                "deta.context.input_limit", prepared.estimate.input_limit
            )
            if prepared.estimate.reported_tokens is not None:
                span.set_attribute(
                    "deta.context.reported_tokens", prepared.estimate.reported_tokens
                )
            if prepared.estimate.needs_compaction:
                tip = plan.context_tip
                if tip is None or tip in self._threshold_tips:
                    raise RunLimitError("同一触发点不能重复阈值压缩")
                self._threshold_tips.add(tip)
                outcome = await self._compact(
                    "threshold", budget, prepared.instructions, prepared.schemas
                )
                if outcome.status != "compacted":
                    raise RunLimitError(outcome.reason)
                raise RebuildRequest("阈值压缩已提交，重新准备本次请求")
            for retry_index in range(budget.options.max_retries + 1):
                observed = False

                def observe(event: Event) -> None:
                    """记录是否已经发布文本或参数增量，并把同一个事件交给 Loop。"""
                    nonlocal observed
                    if isinstance(event, (TextDelta, ToolCallDelta)):
                        observed = True
                    listener(event.model_copy(deep=True))

                try:
                    message = await self._metered_model(
                        budget,
                        self.config,
                        prepared.instructions,
                        prepared.messages,
                        prepared.schemas,
                        listeners=[observe],
                        input_sources=prepared.sources,
                    )
                    self._last_tools = prepared.current_tools
                    span.set_attribute("deta.retry_count", retry_index)
                    return message.model_copy(
                        update={
                            "response_metadata": {
                                **message.response_metadata,
                                "deta_request_fingerprint": prepared.fingerprint,
                            }
                        }
                    )
                except asyncio.CancelledError:
                    span.set_status(Status(StatusCode.ERROR, "CancelledError"))
                    raise
                except Exception as exc:
                    if is_context_overflow(exc) and not observed:
                        if budget.overflow_recovery_used:
                            raise RunLimitError("本次请求的溢出恢复额度耗尽") from exc
                        budget.overflow_recovery_used = True
                        outcome = await self._compact(
                            "overflow", budget, prepared.instructions, prepared.schemas
                        )
                        if outcome.status != "compacted":
                            raise RunLimitError(outcome.reason) from exc
                        raise RebuildRequest(
                            "溢出压缩已提交，重新准备本次请求"
                        ) from exc
                    if (
                        observed
                        or not retryable(exc)
                        or retry_index == budget.options.max_retries
                    ):
                        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                        raise
                    if budget.request_attempts >= budget.options.max_requests:
                        span.set_status(Status(StatusCode.ERROR, "RunLimitError"))
                        raise RunLimitError("没有剩余尝试额度用于重试") from exc
                    delay = budget.options.retry_delay_seconds * (2**retry_index)
                    span.add_event(
                        "retry_scheduled",
                        {
                            "error.type": type(exc).__name__,
                            "deta.retry": retry_index + 1,
                            "deta.delay_seconds": delay,
                        },
                    )
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        span.set_status(Status(StatusCode.ERROR, "CancelledError"))
                        raise
        raise RuntimeError("重试循环缺少退出结果")

    async def _execute_tool(
        self,
        call: ToolCall,
        plan: RequestPlan,
        on_start: Callable[[], None],
        on_output: Callable[[str], None],
    ) -> ToolMessage:
        """复用工具执行器，在前置 Hook 放行后、进入 handler 前提交执行意图。"""

        def begin_execution() -> None:
            """先可靠保存意图，再发布实际开始通知；任何保存失败都会阻止 handler。"""
            self.session.begin_tool(call)
            on_start()

        return await execute_tool(
            call,
            ToolContext(
                self.workspace,
                self.artifacts.root.parent / "tool-output",
                self.shell,
                MappingProxyType(self.environment),
                on_output,
            ),
            tracer=self.tracer,
            artifacts=self.artifacts,
            registry=plan.tools,
            on_start=begin_execution,
            hooks=self.hooks,
        )

    async def _commit(self, message: AgentMessage) -> None:
        """先提交数据库，再更新内存视图；保存失败时不发布虚假的已提交消息。"""
        owned = message.model_copy(deep=True)
        with self.tracer.start_as_current_span(
            "deta.session.commit", record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                entry_id = self.session.commit(owned)
                self.agent.messages.append(owned)
                span.set_attribute("deta.entry_id", entry_id)
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise

    async def _finish_turn(self, report: TurnReport) -> TurnDecision:
        """把完整报告副本交给结束 Hook，未配置时返回 auto 自然决策。"""
        failures = tuple(
            (
                call["name"],
                call["args"],
                (result.artifact or {}).get("error_code", None),
            )
            for call, result in zip(report.response.tool_calls, report.results)
        )
        signature = (
            json.dumps(failures, ensure_ascii=False, sort_keys=True)
            if failures and all((result.status == "error") for result in report.results)
            else ""
        )
        self._failure_count = (
            self._failure_count + 1
            if signature and signature == self._failure_signature
            else int(bool(signature))
        )
        self._failure_signature = signature
        if self._failure_count >= self.agent.options.max_repeated_failures:
            raise RunLimitError("repeated_tool_failure")
        if self.hooks.finish_turn is None:
            return "auto"
        return await self.hooks.finish_turn(copy_report(report))

    async def compact(self) -> CompactionOutcome:
        """空闲时手动压缩；占用维护状态直到生成与提交完全结束。"""
        self._reload()
        self._maintenance = True
        try:
            async with asyncio.timeout(self.agent.options.timeout_seconds):
                return await self._compact(
                    "manual",
                    RunBudget(self.agent.options),
                    self.instructions
                    + "\n\n"
                    + render_resources(
                        load_resources(self.workspace, self._active_skills)
                    ),
                    tool_schemas(self.tools),
                )
        finally:
            self._maintenance = False

    async def _request_summary(
        self,
        prompt: str,
        messages: tuple[HumanMessage, ...],
        *,
        config: ModelConfig,
        budget: RunBudget,
        snapshot_tip: str,
        preparation_ref: str | None,
    ) -> AIMessage:
        """检查摘要输入预算，再通过现有模型边界请求；不递归触发压缩。"""
        # 摘要也受当前 Run 的请求次数与总时限约束，不递归触发压缩。
        size = estimate_context(
            messages,
            model=config.model,
            instructions=prompt,
            tools=(),
            window_tokens=self.context_window,
            output_tokens=config.max_completion_tokens,
            safety_tokens=self.context_margin,
        )
        if size.needs_compaction:
            raise RunLimitError("摘要输入自身超过窗口；保留原会话")
        with self.tracer.start_as_current_span(
            "deta.compaction.summary",
            record_exception=False,
            set_status_on_exception=False,
        ) as stage_span:
            try:
                return await self._metered_model(
                    budget,
                    config,
                    prompt,
                    messages,
                    (),
                    input_sources={
                        "purpose": "compaction",
                        "session_id": self.session.id,
                        "snapshot_tip": snapshot_tip,
                        "preparation_artifact": preparation_ref,
                    },
                )
            except BaseException as exc:
                stage_span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise

    async def _compact(
        self,
        reason: str,
        budget: RunBudget,
        instructions: str,
        schemas: Sequence[ToolSchema],
    ) -> CompactionOutcome:
        """复用当前会话视图，生成候选、预算复核、原子提交；失败沿调用链传播。"""
        with self.tracer.start_as_current_span(
            "deta.compaction", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("deta.compaction.reason", reason)
            span.set_attribute("deta.session_id", self.session.id)
            try:
                view = build_context(self.session.entries())
                estimate = estimate_context(
                    view.messages,
                    model=self.config.model,
                    instructions=instructions,
                    tools=schemas,
                    window_tokens=self.context_window,
                    output_tokens=self.config.max_completion_tokens,
                    safety_tokens=self.context_margin,
                )
                selected = prepare_compaction(view, self.keep_recent_tokens, estimate)
                if selected.preparation is None:
                    span.set_attribute("deta.compaction.outcome", selected.status)
                    return CompactionOutcome(
                        status=selected.status, reason=selected.reason
                    )
                preparation = selected.preparation
                ref = self.artifacts.save(
                    "compaction-input", preparation.model_dump(mode="json")
                )
                if ref:
                    span.set_attribute("deta.compaction.input_artifact", ref)
                config = self.config.model_copy(
                    update={"max_completion_tokens": self.summary_output_tokens}
                )

                request = partial(
                    self._request_summary,
                    config=config,
                    budget=budget,
                    snapshot_tip=preparation.snapshot_tip_id,
                    preparation_ref=ref,
                )
                draft = await generate_compaction(preparation, request)
                if budget.token_stop_reason:
                    # 手动压缩也必须在最后一次摘要返回后检查，不能等不存在的下一次请求。
                    raise RunLimitError(budget.token_stop_reason)
                candidate_messages = (
                    HumanMessage(
                        content="此前会话摘要（历史参考）：\n" + draft.record.summary
                    ),
                    *(
                        item.message
                        for item in resolve_retained_tail(
                            draft.record, self.session.entries()
                        )
                    ),
                )
                if pending_calls(candidate_messages):
                    raise ValueError("摘要尾部仍缺少工具结果")
                after = estimate_context(
                    candidate_messages,
                    model=self.config.model,
                    instructions=instructions,
                    tools=schemas,
                    window_tokens=self.context_window,
                    output_tokens=self.config.max_completion_tokens,
                    safety_tokens=self.context_margin,
                )
                if after.needs_compaction:
                    raise RunLimitError("候选摘要加保留尾部仍超限；不提交无效压缩")
                # 让已到达的取消在同步事务前生效；事务完成之后的取消不能撤销已提交事实。
                await asyncio.sleep(0)
                with self.tracer.start_as_current_span(
                    "deta.compaction.commit",
                    record_exception=False,
                    set_status_on_exception=False,
                ) as stage_span:
                    try:
                        entry_id = self.session.commit_compaction(
                            preparation.snapshot_tip_id, draft.record
                        )
                    except BaseException as exc:
                        stage_span.set_status(
                            Status(StatusCode.ERROR, type(exc).__name__)
                        )
                        raise
                span.set_attribute("deta.entry_id", entry_id)
                span.set_attribute("deta.compaction.tokens_before", estimate.tokens)
                span.set_attribute("deta.compaction.tokens_after", after.tokens)
                span.set_attribute("deta.compaction.outcome", "compacted")
                self.artifacts.save(
                    "compaction-result",
                    {
                        "entry_id": entry_id,
                        "reason": reason,
                        "tokens_after": after.tokens,
                        **draft.model_dump(mode="json"),
                    },
                )
                return CompactionOutcome(
                    status="compacted",
                    entry_id=entry_id,
                    tokens_before=estimate.tokens,
                    tokens_after=after.tokens,
                )
            except BaseException as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise

    def use_skill(self, name: str) -> None:
        """空闲时选择技能；下一次 Run 或手动压缩读取正文，活动 Run 不刷新资源。"""
        if self.agent.running or self._maintenance:
            raise RuntimeError("只能在空闲时修改显式技能选择")
        selected = (*self._active_skills, name)
        load_resources(self.workspace, selected)
        self._active_skills = selected

    async def _model_once(
        self,
        config: ModelConfig,
        instructions: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolSchema],
        *,
        listeners: Sequence[Listener] = (),
        before_attempt: Callable[[], int] | None = None,
        input_sources: JsonValue = None,
    ) -> AIMessage:
        """绑定真实 SDK；离线运行没有客户端，未安装回放边界时直接失败。"""
        if self.client is None:
            raise RuntimeError("离线入口没有真实模型客户端")
        return await stream_once(
            self.client,
            config,
            instructions,
            messages,
            tools,
            tracer=self.tracer,
            artifacts=self.artifacts,
            listeners=listeners,
            before_attempt=before_attempt,
            input_sources=input_sources,
        )

    async def _metered_model(
        self,
        budget: RunBudget,
        config: ModelConfig,
        instructions: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolSchema],
        *,
        listeners: Sequence[Listener] = (),
        input_sources: JsonValue = None,
    ) -> AIMessage:
        """统一核算正文和摘要请求；真实失败尝试没有 usage 时记录未知。"""
        before = budget.request_attempts
        try:
            response = await self.model_call(
                config,
                instructions,
                messages,
                tools,
                listeners=listeners,
                before_attempt=budget.take_request,
                input_sources=input_sources,
            )
        except BaseException:
            if budget.request_attempts > before:
                budget.observe_usage(None)
            raise
        else:
            budget.observe_usage(response.usage_metadata)
            return response
        finally:
            self.usage_stats = {
                "request_attempts": budget.request_attempts,
                "known_tokens": budget.known_tokens,
                "unknown_usage_attempts": budget.unknown_usage_attempts,
            }
            for key, value in self.usage_stats.items():
                get_current_span().set_attribute("deta.usage." + key, value)
