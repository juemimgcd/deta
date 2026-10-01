# Day 14：隔离评测与版本比较

[总览](summary.md) · [前一天](day13.md) · [下一天](day15.md)

## 核心问题

修复一个 Badcase 后，原任务成功了，能否说明改动整体有效？如果另一版本少跑了几条失败任务，怎样防止报告把缺失结果算成提升？

今天先固定任务、初始项目、版本、重复次数和验收口径，再调用正式 AgentSession 逐项运行。每次试验有独立工作区、Session 和诊断目录；报告从全部计划出发，不只读取成功落盘的结果。

## 今天新增什么

| 文件 | 本日变化 |
| --- | --- |
| `evaluation/graders.py` | 文件快照、允许修改范围、最终文件约束与已有检查评分 |
| `evaluation/runner.py` | 任务协议、全部试验计划、干净目录复制、正式运行入口与结果落盘 |
| `evaluation/report.py` | 全计划汇总、重复运行明细、版本配对、缺失与波动说明 |
| `types.py`、`runtime.py`、`loop.py` | 主请求和摘要统一核算 token；连续相同工具失败的停止策略 |

本页给出实现骨架和参考答案，不预先创建任务集、初始项目或测试案例。约 10～20 个任务应从已有人工验收与真实 Badcase 中逐步整理；数量不足就记录现状，不用随意编造的任务凑数。

## 从单任务逐步扩到版本比较

进入本章前完成 Day 13 的 13A 接入补丁；Recorder/Replay 可以在第一次小批回归后补齐。本章全部代码仍调用同一 AgentSession。

| 步骤 | 本次只解决什么 | 可检查的结果 |
| --- | --- | --- |
| 14A：原案例复跑 | 先完成 snapshot/grade、plan_trials/run_batch；只传一个真实任务、一个 Variant、repeats=1 | 初始项目未变、Agent 产物先固定、已有检查有效，原案例有独立 Run 和结果 |
| 14B：小批回归 | 加入已有且验收清楚的任务，完成 summarize，保留全部计划状态 | 每项都有通过、失败或明确缺失；先检查退化，不急于给提升结论 |
| 14C：重复与版本比较 | 完成 compare/render_report，在相同协议下重复运行并比较两个提示词；代码修复按下文双环境流程比较 | 配对方向固定、阻塞配对保留、耗时与 usage 的样本范围明确 |

预算补丁随正式运行接入：先沿用已有请求/工具/时限限制，max_total_tokens 默认关闭；需要验证 token 停止策略时再显式配置。完整参考答案是最终累计版本，不要求在完成第一次原案例复跑前手写所有统计函数。固定任务数依据现有材料逐步增加。

本 runner 的 EvalTask 描述从空 Session 开始的一次 prompt 和最终文件验收。取消时机、重启继续、队列注入和特定压缩边界仍按 Day 7–11 的真实 API/运行记录验收，单独写进 Day 15 清单；普通文件任务的批次报告不能自动证明这些路径已经覆盖。

## 先把 Task、Trial、Run 分开

```text
EvalTask
  id、prompt、initial、acceptance、options、skills
       │ 与 Variant 和重复编号组合
       ▼
Trial
  id、task_id、variant_id、repeat
       │ 独立复制初始项目并组装运行时
       ▼
AgentSession.prompt
  Session / Run / Trace / 工具实际执行
       │ 完整结束后交给可信评分器
       ▼
Observation
  计划身份、状态、Run/Trace 引用、耗时、usage、grade 与依据
       │ 按全部 Trial 左连接
       ▼
报告
```

EvalTask 是可重复使用的任务定义，Trial 是一次计划试验，Run 是正式 Agent 的实际执行。复制失败时可以存在 Trial 而没有 Run，评分器故障时可以存在完整 Run 而没有有效 grade；两者都必须在报告中占有位置。

## 任务和评分口径先冻结

| 字段 | 应从哪里取得 |
| --- | --- |
| prompt | 原任务的完整要求，包含需要 Agent 遵守的约束 |
| initial | 干净初始项目的相对目录，内容指纹在 protocol 中保存 |
| allowed_changes | 实际允许改变的路径模式；删除、新增也计入变化 |
| files | 需要存在的真实文件、必要/禁止内容，必要时指定精确指纹 |
| checks | 项目原本已有且已确认可运行的检查命令，不从模型回答里提取 |
| check_outputs | 检查命令允许生成的缓存或构建输出路径；不豁免 Agent 修改范围 |
| options | 请求次数、工具次数、总时限、可选 token 上限与重复失败上限 |
| skills | 本任务确实需要的显式技能名称 |

任务要求对模型可见，评分实现和基准值由评测器持有。不要把完整 acceptance、隐藏的期望文件和通过标记作为工作文件交给 Agent 随意修改。模型自己写的“完成”文件或最终回答不是通过依据。

首版确定性评分包括文件存在/内容、允许修改范围和已有命令。某条 contains 检查只证明出现了指定文本，不证明语义正确；原样字节比对也可能拒绝另一种正确实现。按任务选择真实验收条件，并用已有已知正确产物和真实错误产物校准评分器。需要人工或语义判断的任务先标明缺口，不把弱检查的通过解释成全面正确。

## 工作区隔离到什么程度

每个 Trial 使用 `workspaces/<trial_id>`；Session、Trace、工具输出和评分输出位于工作区外的 `runs/<trial_id>`。复制前固定初始快照，复制后再次比对；初始项目发生变化就中止该试验并保留错误。首版只接受普通文件与目录，拒绝符号链接。

这解决文件和会话的串扰，不是操作系统安全沙箱。bash 仍以当前进程权限运行，知道外部路径时可能访问工作区之外。需要对不可信任务隔离网络、权限或评分秘密时，另配容器/虚拟机等执行环境，并单独验证；不能把 shutil.copytree 写成安全隔离证明。

先准备固定依赖和环境，再进行批次运行，不在每个试验里自动安装依赖或清理原项目。检查会生成缓存时，提前通过 check_outputs 声明具体路径，或把缓存配置到试验输出目录；这个字段只描述检查命令的输出，不放宽 Agent 的 allowed_changes。

grade 先保存 Agent 结束时的 candidate_files 指纹并核对文件约束，再执行已有检查。每条检查结束后重新扫描；如果它改写了声明输出范围之外的产物，本次评分标为无效，runner 记录 grader_error。格式化或自动修复命令不能替 Agent 修正文件后再算通过。目录扫描的权限或 I/O 失败同样传播，不把缺读的文件树当成完整快照。

## 先认识本日的类与函数

| 对象或函数 | 输入、输出与调用关系 |
| --- | --- |
| `FileRule / Acceptance` | 可信的文件与检查口径，放在评测器侧 |
| `Grade` | passed、valid、reasons、evidence、candidate_files；区分任务结果与评分过程是否有效 |
| `snapshot` | 目录 → 相对文件路径与 SHA-256，不运行 Agent |
| `grade` | 最终工作区、初始快照、Acceptance → 验收结果 |
| `EvalTask / Variant / Trial` | 任务定义、提示词版本、一次已计划试验 |
| `plan_trials` | 任务 × 版本 × 重复次数 → 完整计划，重复批次交替版本顺序 |
| `run_trial` | 单条试验：核对并复制工作区 → 运行 Agent → 评分；逐步更新传入 row |
| `run_batch` | 固定配置 → 独立目录和正式 AgentSession（安装 Day 12 事件监听函数）→ 全部已有结果 |
| `index_results` | 核对计划和结果身份，返回按 Trial ID 建立的索引 |
| `summarize / summarize_indexed` | 独立调用时先校验；已校验索引直接生成各版本汇总 |
| `compare` | 相同任务/重复编号两两配对 → 改善、退化、阻塞与可发布差值 |
| `render_report` | 计划与结果 → 可保存的 Markdown 报告 |

## token 与重复失败怎样计数

Day 7 已有实际尝试、工具批次和总时间限制，今天只补齐剩余策略。正文与摘要都经过 _metered_model；成功响应核算 usage，已进入边界但失败且无 usage 的尝试记 unknown_usage_attempts。usage_metadata 为 None 时记录未知；有报告时读取其中的 total_tokens，不把未知换成零。

max_total_tokens 是“基于已报告用量，阻止后续请求并报告超额”的策略，不是提供方硬计费上限。最后一次请求可能使总量跨过上限；准确费用还受提供方统计和定价影响。本页只报告 token 与耗时，不生成没有依据的货币成本。

当启用了 token 上限而尝试用量未知时，后续运行不能继续声称预算合规，会以 token_usage_unknown 停止。普通响应已经产生的工具调用仍需按 Loop 原有的整批失败结果结清，不能为了停止丢下缺失配对。摘要生成结束后也立即检查 token_stop_reason，确认通过才提交 Compaction；手动压缩没有下一次模型请求，不能把超额或未知用量的检查推迟到下次请求。

重复失败按完整工具批次比较“工具名、结构化参数和错误码”。连续相同失败达到上限时停止；有成功结果或参数改变就重新计数。它是 Deta 的保守策略，不能声称能识别所有语义相同的失败；首次错误仍允许模型正常修正。

## 对运行预算与停止策略的接入补丁

以下补丁相对于 Day 13 完成状态。统一计量放在 model_call 外层，所以录制回放和真实模型仍经过同一个 RunBudget；回放用量属于录制来源，报告必须标注为 replay，不能混进本日真实评测批次。

<details>
<summary>接入补丁：src/deta/types.py（相对 Day 13 完成状态）</summary>

```diff
--- a/src/deta/types.py
+++ b/src/deta/types.py
@@ -1,7 +1,7 @@
 from dataclasses import dataclass
 from typing import Any, Literal

-from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
+from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, UsageMetadata
 from pydantic import BaseModel, ConfigDict, Field

 # Any 仅用于 LangChain 的工具声明边界；实际工具参数仍由 Pydantic 校验。
@@ -28,6 +28,9 @@

     # 一次 Run 的实际 SDK 尝试总额度，包括重试；在调用 SDK 前扣减。
     max_requests: int = Field(default=10, ge=1)
+    # 提供方已报告用量的停止预算；缺失 usage 时不能证明额度合规。
+    max_total_tokens: int | None = Field(default=None, ge=1)
+    max_repeated_failures: int = Field(default=3, ge=1)
     # 一次 Run 允许的工具调用总数；设为零表示不给工具执行额度。
     max_tool_calls: int = Field(default=20, ge=0)
     # 整次 Run 的总时间额度，单位为秒，与单次模型请求超时分别管理。
@@ -75,9 +78,34 @@
     tool_calls: int = 0
     # 每个逻辑助手请求最多一次提供方溢出恢复；Loop 在新请求开始时重置。
     overflow_recovery_used: bool = False
+    known_tokens: int = 0
+    unknown_usage_attempts: int = 0
+
+    @property
+    def token_stop_reason(self) -> str:
+        limit = self.options.max_total_tokens
+        if limit is None:
+            return ""
+        if self.unknown_usage_attempts:
+            return "token_usage_unknown"
+        return "token_budget" if self.known_tokens > limit else ""
+
+    def observe_usage(self, usage: UsageMetadata | None) -> None:
+        total = usage["total_tokens"] if usage is not None else None
+        if total is None:
+            self.unknown_usage_attempts += 1
+        else:
+            self.known_tokens += total

     def take_request(self) -> int:
         """在真正开始 SDK 尝试前检查额度并计数，返回该 Run 内的尝试编号。"""
+        if self.token_stop_reason:
+            raise RunLimitError(self.token_stop_reason)
+        if (
+            self.options.max_total_tokens is not None
+            and self.known_tokens >= self.options.max_total_tokens
+        ):
+            raise RunLimitError("token_budget")
         if self.request_attempts >= self.options.max_requests:
             raise RunLimitError("实际模型请求尝试额度耗尽")
         self.request_attempts += 1
```

</details>

<details>
<summary>接入补丁：src/deta/runtime.py（相对 Day 13 完成状态）</summary>

```diff
--- a/src/deta/runtime.py
+++ b/src/deta/runtime.py
@@ -222,6 +222,9 @@
         self._resources: ResourceBundle | None = None
         self._maintenance = False
         self._threshold_tips: set[str] = set()
+        self._failure_signature = ""
+        self._failure_count = 0
+        self.usage_stats: dict[str, int] = {}
         # 活动运行、临时消息视图与队列所有者；最终历史由 Session 保存。
         self.agent = Agent(
             LoopBindings(
@@ -295,6 +298,9 @@
             }
         )
         self._threshold_tips.clear()
+        self._failure_signature = ""
+        self._failure_count = 0
+        self.usage_stats = {}
         # 先读资源再登记 Run；加载失败时不留下无法收尾的 running 记录。
         self._resources = None
         resources = load_resources(self.workspace, self._active_skills)
@@ -332,6 +338,7 @@
             "capture-status",
             {
                 "run_id": result.run_id,
+                "usage": dict(self.usage_stats),
                 "saved": self.artifacts.saved,
                 "skipped": self.artifacts.skipped,
                 "failed": self.artifacts.failed,
@@ -461,13 +468,13 @@
                     listener(event.model_copy(deep=True))

                 try:
-                    message = await self.model_call(
+                    message = await self._metered_model(
+                        budget,
                         self.config,
                         prepared.instructions,
                         prepared.messages,
                         prepared.schemas,
                         listeners=[observe],
-                        before_attempt=budget.take_request,
                         input_sources=prepared.sources,
                     )
                     self._last_tools = prepared.current_tools
@@ -568,6 +575,27 @@

     async def _finish_turn(self, report: TurnReport) -> TurnDecision:
         """把完整报告副本交给结束 Hook，未配置时返回 auto 自然决策。"""
+        failures = tuple(
+            (
+                call["name"],
+                call["args"],
+                (result.artifact or {}).get("error_code", None),
+            )
+            for call, result in zip(report.response.tool_calls, report.results)
+        )
+        signature = (
+            json.dumps(failures, ensure_ascii=False, sort_keys=True)
+            if failures and all((result.status == "error") for result in report.results)
+            else ""
+        )
+        self._failure_count = (
+            self._failure_count + 1
+            if signature and signature == self._failure_signature
+            else int(bool(signature))
+        )
+        self._failure_signature = signature
+        if self._failure_count >= self.agent.options.max_repeated_failures:
+            raise RunLimitError("repeated_tool_failure")
         if self.hooks.finish_turn is None:
             return "auto"
         return await self.hooks.finish_turn(copy_report(report))
@@ -620,12 +648,12 @@
             set_status_on_exception=False,
         ) as stage_span:
             try:
-                return await self.model_call(
+                return await self._metered_model(
+                    budget,
                     config,
                     prompt,
                     messages,
                     (),
-                    before_attempt=budget.take_request,
                     input_sources={
                         "purpose": "compaction",
                         "session_id": self.session.id,
@@ -685,6 +713,9 @@
                     preparation_ref=ref,
                 )
                 draft = await generate_compaction(preparation, request)
+                if budget.token_stop_reason:
+                    # 手动压缩也必须在最后一次摘要返回后检查，不能等不存在的下一次请求。
+                    raise RunLimitError(budget.token_stop_reason)
                 candidate_messages = (
                     HumanMessage(
                         content="此前会话摘要（历史参考）：\n" + draft.record.summary
@@ -782,3 +813,42 @@
             before_attempt=before_attempt,
             input_sources=input_sources,
         )
+
+    async def _metered_model(
+        self,
+        budget: RunBudget,
+        config: ModelConfig,
+        instructions: str,
+        messages: Sequence[AgentMessage],
+        tools: Sequence[ToolSchema],
+        *,
+        listeners: Sequence[Listener] = (),
+        input_sources: JsonValue = None,
+    ) -> AIMessage:
+        """统一核算正文和摘要请求；真实失败尝试没有 usage 时记录未知。"""
+        before = budget.request_attempts
+        try:
+            response = await self.model_call(
+                config,
+                instructions,
+                messages,
+                tools,
+                listeners=listeners,
+                before_attempt=budget.take_request,
+                input_sources=input_sources,
+            )
+        except BaseException:
+            if budget.request_attempts > before:
+                budget.observe_usage(None)
+            raise
+        else:
+            budget.observe_usage(response.usage_metadata)
+            return response
+        finally:
+            self.usage_stats = {
+                "request_attempts": budget.request_attempts,
+                "known_tokens": budget.known_tokens,
+                "unknown_usage_attempts": budget.unknown_usage_attempts,
+            }
+            for key, value in self.usage_stats.items():
+                get_current_span().set_attribute("deta.usage." + key, value)
```

</details>

<details>
<summary>接入补丁：src/deta/loop.py（相对 Day 13 完成状态）</summary>

```diff
--- a/src/deta/loop.py
+++ b/src/deta/loop.py
@@ -73,7 +73,7 @@
     """预占整批预算，按序执行并提交配对结果；整批结清后再报告额度或截断。"""
     calls = response.tool_calls
     results: list[ToolMessage] = []
-    blocked = ""
+    blocked = budget.token_stop_reason
     if calls and budget.request_attempts >= options.max_requests:
         blocked = "没有剩余请求额度消费工具结果"
     if (
```

</details>

## 完整练习骨架

新建 evaluation 包，__init__.py 只放下面的模块说明。依次完成 snapshot/grade、plan_trials/run_batch、summarize_indexed/compare_indexed/render_report；类型、索引校验和独立调用入口直接提供。render_report 只调用一次 index_results，再将同一索引交给汇总和配对函数；单独调用 summarize 或 compare 仍会校验输入。

### src/deta/evaluation/__init__.py

```python
"""调用正式运行时进行隔离试验与结果汇总。"""
```

### src/deta/evaluation/graders.py

```python
import fnmatch
import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from deta.builtin_tools import ToolContext
from deta.builtin_tools.bash import BashArgs, run_bash
from deta.types import Data


class FileRule(Data):
    """可信评分配置，只描述真实任务的最终文件约束。"""

    path: str
    contains: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    sha256: str | None = None


class Acceptance(Data):
    allowed_changes: tuple[str, ...]
    files: tuple[FileRule, ...]
    # 人工确认的已有命令，原样交给 shell；不从模型回答中提取命令。
    checks: tuple[str, ...] = ()
    # 仅供检查命令生成缓存或构建输出；不能用它豁免修改待评分源码。
    check_outputs: tuple[str, ...] = ()


class Grade(Data):
    passed: bool
    reasons: tuple[str, ...]
    evidence: tuple[str, ...]
    # False 表示检查过程改写了不应改写的产物，不能给任务通过/失败结论。
    valid: bool = True
    # 评分命令开始前固定的 Agent 产物；无法完整读取时为 None。
    candidate_files: dict[str, str] | None = None


def snapshot(root: Path) -> dict[str, str]:
    """完整读取普通文件树；拒绝链接，不把扫描权限或 I/O 失败当成文件不存在。"""
    # TODO：完成 snapshot，保留本文约定的输入、输出与失败边界。
    raise NotImplementedError("请完成 snapshot")


async def grade(
    workspace: Path,
    before: dict[str, str],
    acceptance: Acceptance,
    output_dir: Path,
    environment: Mapping[str, str],
) -> Grade:
    """先固定 Agent 产物，再执行检查；检查自身不能替 Agent 修正答案。"""
    # TODO：完成 grade，保留本文约定的输入、输出与失败边界。
    raise NotImplementedError("请完成 grade")
```

### src/deta/evaluation/runner.py

```python
import asyncio
import json
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from langchain_openai import ChatOpenAI
from pydantic import Field, JsonValue, TypeAdapter

from deta.evaluation.graders import Acceptance, Grade, grade, snapshot
from deta.model import ModelConfig
from deta.observability.artifacts import Artifacts, source_version
from deta.observability.tracing import artifact_listener, local_tracing
from deta.runtime import AgentSession
from deta.session import Session
from deta.storage import SQLiteStore
from deta.types import Data, RunOptions


class EvalTask(Data):
    id: str
    prompt: str
    initial: str
    acceptance: Acceptance
    options: RunOptions = Field(default_factory=RunOptions)
    skills: tuple[str, ...] = ()


class Variant(Data):
    """本实现首先比较同一份源码上的提示词版本，模型配置由批次统一提供。"""

    id: str
    instructions: str


class Trial(Data):
    id: str
    task_id: str
    variant_id: str
    repeat: int


def plan_trials(
    tasks: Sequence[EvalTask], variants: Sequence[Variant], repeats: int
) -> tuple[Trial, ...]:
    # TODO：完成 plan_trials，保留本文约定的输入、输出与失败边界。
    raise NotImplementedError("请完成 plan_trials")


def persist(path: Path, value: object) -> None:
    """评测结果是本工作流的交付物，保存失败必须显式传播。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            TypeAdapter(JsonValue).validate_python(value), ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


async def run_trial(
    trial: Trial,
    task: EvalTask,
    variant: Variant,
    row: dict[str, JsonValue],
    *,
    client: ChatOpenAI,
    config: ModelConfig,
    initial_root: Path,
    initial_version: dict[str, str],
    work: Path,
    run_root: Path,
    context_window: int,
    environment: Mapping[str, str],
    capture_body: bool,
    redact: Callable[[str], str] | None,
) -> None:
    "复制、运行和评分一条试验，逐步填充 row；外层负责保存及取消收尾记录。"
    raise NotImplementedError("请完成 run_trial")


async def run_batch(
    tasks: Sequence[EvalTask],
    variants: Sequence[Variant],
    *,
    repeats: int,
    initial_root: Path,
    batch_root: Path,
    config: ModelConfig,
    context_window: int,
    environment: Mapping[str, str],
    capture_body: bool = False,
    redact: Callable[[str], str] | None = None,
) -> list[dict[str, JsonValue]]:
    """先固定全部计划，再逐个复制、运行和评分；不复用 Session 或修改原始项目。"""
    # TODO：完成 run_batch，保留本文约定的输入、输出与失败边界。
    raise NotImplementedError("请完成 run_batch")
```

### src/deta/evaluation/report.py

```python
# ruff: noqa: F401  # 为 TODO 预留的导入。
import json
from collections import Counter
from collections.abc import Sequence
from statistics import median
from typing import Any

from deta.evaluation.runner import Trial


def index_results(
    trials: Sequence[Trial], rows: Sequence[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """验证计划与结果身份，建立供汇总和配对共同使用的索引。"""
    planned = {trial.id: trial for trial in trials}
    identities = {(trial.task_id, trial.variant_id, trial.repeat) for trial in trials}
    if len(planned) != len(trials) or len(identities) != len(trials):
        raise ValueError("计划包含重复 Trial 或配对位置")
    actual: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["id"] not in planned or row["id"] in actual:
            raise ValueError("结果包含未计划或重复试验")
        trial = planned[row["id"]]
        if (row.get("task_id"), row.get("variant_id"), row.get("repeat")) != (
            trial.task_id,
            trial.variant_id,
            trial.repeat,
        ):
            raise ValueError("结果的任务、版本或重复编号与计划不一致")
        actual[row["id"]] = row
    return actual


def summarize(
    trials: Sequence[Trial], rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """验证结果并按预先计划汇总；缺行、未开始和异常都保留在分母中。"""
    return summarize_indexed(trials, index_results(trials, rows))


def summarize_indexed(
    trials: Sequence[Trial], actual: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """汇总已验证索引，不重复构建或校验结果表。"""
    raise NotImplementedError("请完成 summarize_indexed")


def compare(
    trials: Sequence[Trial],
    rows: Sequence[dict[str, Any]],
    control: str,
    candidate: str,
) -> dict[str, Any]:
    """按任务和重复编号配对；有未完成配对时保留证据并暂不发布整体提升。"""
    return compare_indexed(trials, index_results(trials, rows), control, candidate)


def compare_indexed(
    trials: Sequence[Trial],
    actual: dict[str, dict[str, Any]],
    control: str,
    candidate: str,
) -> dict[str, Any]:
    """在已验证的结果索引上计算版本配对。"""
    raise NotImplementedError("请完成 compare_indexed")


def render_report(trials: Sequence[Trial], rows: Sequence[dict[str, Any]]) -> str:
    """生成可保存的 Markdown；详细证据仍在 protocol 和 observations 中。"""
    raise NotImplementedError("请完成 render_report")
```

## 完整参考答案

<details>
<summary>参考答案：src/deta/evaluation/graders.py（完整文件）</summary>

```python
import fnmatch
import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from deta.builtin_tools import ToolContext
from deta.builtin_tools.bash import BashArgs, run_bash
from deta.types import Data


class FileRule(Data):
    """可信评分配置，只描述真实任务的最终文件约束。"""

    path: str
    contains: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    sha256: str | None = None


class Acceptance(Data):
    allowed_changes: tuple[str, ...]
    files: tuple[FileRule, ...]
    # 人工确认的已有命令，原样交给 shell；不从模型回答中提取命令。
    checks: tuple[str, ...] = ()
    # 仅供检查命令生成缓存或构建输出；不能用它豁免修改待评分源码。
    check_outputs: tuple[str, ...] = ()


class Grade(Data):
    passed: bool
    reasons: tuple[str, ...]
    evidence: tuple[str, ...]
    # False 表示检查过程改写了不应改写的产物，不能给任务通过/失败结论。
    valid: bool = True
    # 评分命令开始前固定的 Agent 产物；无法完整读取时为 None。
    candidate_files: dict[str, str] | None = None


def snapshot(root: Path) -> dict[str, str]:
    """完整读取普通文件树；拒绝链接，不把扫描权限或 I/O 失败当成文件不存在。"""
    result: dict[str, str] = {}

    def scan_error(error: OSError) -> None:
        raise error

    for directory, folders, files in os.walk(
        root, followlinks=False, onerror=scan_error
    ):
        for name in sorted((*folders, *files)):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(f"工作区包含符号链接：{path.relative_to(root)}")
            if stat.S_ISREG(mode):
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                result[path.relative_to(root).as_posix()] = digest.hexdigest()
            elif not stat.S_ISDIR(mode):
                raise ValueError("工作区仅支持普通文件与目录")
    return result


async def grade(
    workspace: Path,
    before: dict[str, str],
    acceptance: Acceptance,
    output_dir: Path,
    environment: Mapping[str, str],
) -> Grade:
    """先固定 Agent 产物，再执行检查；检查自身不能替 Agent 修正答案。"""
    reasons: list[str] = []
    evidence: list[str] = []
    try:
        candidate = snapshot(workspace)
    except ValueError as exc:
        return Grade(passed=False, reasons=(str(exc),), evidence=())
    changed = sorted(
        path
        for path in before.keys() | candidate.keys()
        if before.get(path) != candidate.get(path)
    )
    forbidden = [
        path
        for path in changed
        if not any(
            fnmatch.fnmatchcase(path, allowed) for allowed in acceptance.allowed_changes
        )
    ]
    if forbidden:
        reasons.append("修改越界：" + ", ".join(forbidden))
    for rule in acceptance.files:
        path = workspace / rule.path
        if Path(rule.path).is_absolute() or not path.resolve().is_relative_to(
            workspace.resolve()
        ):
            raise ValueError("评分路径必须位于试验工作区内")
        if rule.path not in candidate:
            reasons.append(f"缺少验收文件：{rule.path}")
            continue
        if rule.sha256 is not None and candidate[rule.path] != rule.sha256:
            reasons.append(f"文件指纹不匹配：{rule.path}")
        if rule.contains or rule.excludes:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                reasons.append(f"要求 UTF-8 的文件编码无效：{rule.path}")
                continue
            if any(value not in text for value in rule.contains) or any(
                value in text for value in rule.excludes
            ):
                reasons.append(f"文件内容约束未满足：{rule.path}")
    for command in acceptance.checks:
        output = await run_bash(
            BashArgs(command=command),
            ToolContext(
                workspace, output_dir, "/bin/zsh", environment, lambda text: None
            ),
        )
        evidence.append(output.content)
        try:
            after_check = snapshot(workspace)
        except ValueError as exc:
            return Grade(
                passed=False,
                valid=False,
                reasons=(*reasons, str(exc)),
                evidence=tuple(evidence),
                candidate_files=candidate,
            )
        checker_changes = sorted(
            path
            for path in candidate.keys() | after_check.keys()
            if candidate.get(path) != after_check.get(path)
            and not any(
                fnmatch.fnmatchcase(path, pattern)
                for pattern in acceptance.check_outputs
            )
        )
        if checker_changes:
            return Grade(
                passed=False,
                valid=False,
                reasons=(
                    *reasons,
                    "检查命令改写了待评产物：" + ", ".join(checker_changes),
                ),
                evidence=tuple(evidence),
                candidate_files=candidate,
            )
        if output.error_code:
            reasons.append(f"已有检查失败：{command} ({output.error_code})")
    return Grade(
        passed=not reasons,
        reasons=tuple(reasons),
        evidence=tuple(evidence),
        candidate_files=candidate,
    )
```

</details>

<details>
<summary>参考答案：src/deta/evaluation/runner.py（完整文件）</summary>

```python
import asyncio
import json
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from langchain_openai import ChatOpenAI
from pydantic import Field, JsonValue, TypeAdapter

from deta.evaluation.graders import Acceptance, Grade, grade, snapshot
from deta.model import ModelConfig, open_model
from deta.observability.artifacts import Artifacts, source_version
from deta.observability.tracing import artifact_listener, local_tracing
from deta.runtime import AgentSession
from deta.session import Session
from deta.storage import SQLiteStore
from deta.types import Data, RunOptions


class EvalTask(Data):
    id: str
    prompt: str
    initial: str
    acceptance: Acceptance
    options: RunOptions = Field(default_factory=RunOptions)
    skills: tuple[str, ...] = ()


class Variant(Data):
    """本实现首先比较同一份源码上的提示词版本，模型配置由批次统一提供。"""

    id: str
    instructions: str


class Trial(Data):
    id: str
    task_id: str
    variant_id: str
    repeat: int


def plan_trials(
    tasks: Sequence[EvalTask], variants: Sequence[Variant], repeats: int
) -> tuple[Trial, ...]:
    if repeats < 1 or not tasks or not variants:
        raise ValueError("任务、版本和重复次数不能为空")
    if len({task.id for task in tasks}) != len(tasks) or len(
        {value.id for value in variants}
    ) != len(variants):
        raise ValueError("任务或版本 ID 重复")
    return tuple(
        Trial(id=uuid4().hex, task_id=task.id, variant_id=variant.id, repeat=index)
        for index in range(1, repeats + 1)
        for task in tasks
        for variant in (variants if index % 2 else tuple(reversed(variants)))
    )


def persist(path: Path, value: object) -> None:
    """评测结果是本工作流的交付物，保存失败必须显式传播。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            TypeAdapter(JsonValue).validate_python(value), ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


async def run_trial(
    trial: Trial,
    task: EvalTask,
    variant: Variant,
    row: dict[str, JsonValue],
    *,
    client: ChatOpenAI,
    config: ModelConfig,
    initial_root: Path,
    initial_version: dict[str, str],
    work: Path,
    run_root: Path,
    context_window: int,
    environment: Mapping[str, str],
    capture_body: bool,
    redact: Callable[[str], str] | None,
) -> None:
    """复制、运行和评分一条试验，逐步填充 row；外层负责保存及取消收尾记录。"""
    initial = (initial_root / task.initial).resolve(strict=True)
    if snapshot(initial) != initial_version:
        raise ValueError("初始项目已变化，不能沿用本批协议")
    shutil.copytree(initial, work, symlinks=True)
    # 复制前后都核对；拒绝复制中出现的链接或内容变化。
    if snapshot(work) != initial_version:
        raise ValueError("复制结果与已固定初始项目不一致")
    artifacts = Artifacts(
        run_root / "artifacts", capture_body=capture_body, redact=redact
    )
    with (
        closing(SQLiteStore(run_root / "session.sqlite3")) as store,
        local_tracing(run_root / "spans.jsonl") as tracer,
    ):
        recorded = Session(store, work)
        runtime = AgentSession(
            client,
            config,
            work,
            tracer,
            artifacts,
            session=recorded,
            context_window=context_window,
            instructions=variant.instructions,
            options=task.options,
            environment=environment,
            listeners=[artifact_listener(artifacts)],
        )
        for name in task.skills:
            runtime.use_skill(name)
        run_started = time.monotonic()
        try:
            result = await runtime.prompt(task.prompt, run_id=trial.id)
        finally:
            row["run_elapsed_seconds"] = time.monotonic() - run_started
        row.update(
            {
                "run_id": result.run_id,
                "session_id": recorded.id,
                "run_status": result.status,
                "reason": result.reason,
                "usage": dict(runtime.usage_stats),
                "trace": str(run_root / "spans.jsonl"),
            }
        )
    try:
        result_grade: Grade = await grade(
            work,
            initial_version,
            task.acceptance,
            run_root / "grader-output",
            environment,
        )
    except Exception as exc:
        row.update({"status": "grader_error", "error": type(exc).__name__})
    else:
        row["grade"] = result_grade.model_dump(mode="json")
        row["status"] = (
            "grader_error"
            if not result_grade.valid
            else "passed"
            if result.status == "completed" and result_grade.passed
            else "failed"
        )


async def run_batch(
    tasks: Sequence[EvalTask],
    variants: Sequence[Variant],
    *,
    repeats: int,
    initial_root: Path,
    batch_root: Path,
    config: ModelConfig,
    context_window: int,
    environment: Mapping[str, str],
    capture_body: bool = False,
    redact: Callable[[str], str] | None = None,
) -> list[dict[str, JsonValue]]:
    """先固定全部计划，再逐个复制、运行和评分；不复用 Session 或修改原始项目。"""
    trials = plan_trials(tasks, variants, repeats)
    initial_root = initial_root.resolve(strict=True)
    batch_root = batch_root.resolve()
    if batch_root.is_relative_to(initial_root) or initial_root.is_relative_to(
        batch_root
    ):
        raise ValueError("批次目录必须与初始项目目录分离")
    batch_root.mkdir(parents=True, exist_ok=False)
    task_map = {task.id: task for task in tasks}
    variant_map = {variant.id: variant for variant in variants}
    initial_versions: dict[str, dict[str, str]] = {}
    for task in tasks:
        initial = (initial_root / task.initial).resolve(strict=True)
        if not initial.is_relative_to(initial_root) or not initial.is_dir():
            raise ValueError("初始项目路径越界或不是目录")
        if not task.acceptance.files and not task.acceptance.checks:
            raise ValueError("每个任务至少有一项产物或已有检查验收")
        initial_versions[task.id] = snapshot(initial)
    protocol: dict[str, JsonValue] = {
        "version": 1,
        "model": config.model,
        "max_completion_tokens": config.max_completion_tokens,
        "context_window": context_window,
        "source_sha256": source_version(Path(__file__).parents[1]),
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "variants": [variant.model_dump(mode="json") for variant in variants],
        "trials": [trial.model_dump(mode="json") for trial in trials],
        "initial_versions": {
            key: dict(value) for key, value in initial_versions.items()
        },
        "environment_keys": [name for name in sorted(environment)],
    }
    persist(batch_root / "protocol.json", protocol)
    rows: list[dict[str, JsonValue]] = [
        {**trial.model_dump(mode="json"), "status": "planned"} for trial in trials
    ]
    persist(batch_root / "observations.json", rows)
    async with open_model(config) as client:
        for position, trial in enumerate(trials):
            row = rows[position]
            task, variant = task_map[trial.task_id], variant_map[trial.variant_id]
            work = batch_root / "workspaces" / trial.id
            run_root = batch_root / "runs" / trial.id
            row["status"] = "running"
            persist(batch_root / "observations.json", rows)
            started = time.monotonic()
            try:
                await run_trial(
                    trial,
                    task,
                    variant,
                    row,
                    client=client,
                    config=config,
                    initial_root=initial_root,
                    initial_version=initial_versions[task.id],
                    work=work,
                    run_root=run_root,
                    context_window=context_window,
                    environment=environment,
                    capture_body=capture_body,
                    redact=redact,
                )
            except asyncio.CancelledError:
                row["status"] = "cancelled"
                raise
            except Exception as exc:
                row.update({"status": "runner_error", "error": type(exc).__name__})
            finally:
                row["elapsed_seconds"] = time.monotonic() - started
                persist(batch_root / "observations.json", rows)
    return rows
```

</details>

<details>
<summary>参考答案：src/deta/evaluation/report.py（完整文件）</summary>

```python
import json
from collections import Counter
from collections.abc import Sequence
from statistics import median
from typing import Any

from deta.evaluation.runner import Trial


def index_results(
    trials: Sequence[Trial], rows: Sequence[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """验证计划与结果身份，建立供汇总和配对共同使用的索引。"""
    planned = {trial.id: trial for trial in trials}
    identities = {(trial.task_id, trial.variant_id, trial.repeat) for trial in trials}
    if len(planned) != len(trials) or len(identities) != len(trials):
        raise ValueError("计划包含重复 Trial 或配对位置")
    actual: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["id"] not in planned or row["id"] in actual:
            raise ValueError("结果包含未计划或重复试验")
        trial = planned[row["id"]]
        if (row.get("task_id"), row.get("variant_id"), row.get("repeat")) != (
            trial.task_id,
            trial.variant_id,
            trial.repeat,
        ):
            raise ValueError("结果的任务、版本或重复编号与计划不一致")
        actual[row["id"]] = row
    return actual


def summarize(
    trials: Sequence[Trial], rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """验证结果并按预先计划汇总；缺行、未开始和异常都保留在分母中。"""
    return summarize_indexed(trials, index_results(trials, rows))


def summarize_indexed(
    trials: Sequence[Trial], actual: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """汇总已验证索引，不重复构建或校验结果表。"""
    result: list[dict[str, Any]] = []
    for variant in sorted({trial.variant_id for trial in trials}):
        selected = [trial for trial in trials if trial.variant_id == variant]
        values = [actual.get(trial.id, {"status": "missing"}) for trial in selected]
        counts = Counter(value["status"] for value in values)
        elapsed = [
            value["run_elapsed_seconds"]
            for value in values
            if "run_elapsed_seconds" in value
        ]
        complete_usage = [
            value["usage"]["known_tokens"]
            for value in values
            if value.get("usage", {}).get("request_attempts", 0) > 0
            and value["usage"].get("unknown_usage_attempts") == 0
        ]
        result.append(
            {
                "variant": variant,
                "planned": len(selected),
                "states": dict(counts),
                "passed": counts["passed"],
                "pass_rate_over_planned": counts["passed"] / len(selected),
                "run_seconds_median": median(elapsed) if elapsed else None,
                "elapsed_samples": len(elapsed),
                "tokens_known_subset": sum(complete_usage) if complete_usage else None,
                "by_repeat": {
                    index: {
                        "planned": sum(trial.repeat == index for trial in selected),
                        "passed": sum(
                            trial.repeat == index
                            and actual.get(trial.id, {}).get("status") == "passed"
                            for trial in selected
                        ),
                    }
                    for index in sorted({trial.repeat for trial in selected})
                },
                "trials_with_complete_usage": len(complete_usage),
                "trials_without_complete_usage": len(selected) - len(complete_usage),
            }
        )
    return result


def compare(
    trials: Sequence[Trial],
    rows: Sequence[dict[str, Any]],
    control: str,
    candidate: str,
) -> dict[str, Any]:
    """按任务和重复编号配对；有未完成配对时保留证据并暂不发布整体提升。"""
    return compare_indexed(trials, index_results(trials, rows), control, candidate)


def compare_indexed(
    trials: Sequence[Trial],
    actual: dict[str, dict[str, Any]],
    control: str,
    candidate: str,
) -> dict[str, Any]:
    """在已验证的结果索引上计算版本配对。"""
    variants = {trial.variant_id for trial in trials}
    if control == candidate or control not in variants or candidate not in variants:
        raise ValueError("对照必须选择计划中两个不同的版本")
    arms: dict[tuple[str, int], dict[str, str]] = {}
    for trial in trials:
        if trial.variant_id in {control, candidate}:
            key = (trial.task_id, trial.repeat)
            if trial.variant_id in arms.setdefault(key, {}):
                raise ValueError("同一个配对位置重复")
            arms[key][trial.variant_id] = actual.get(trial.id, {}).get(
                "status", "missing"
            )
    improved = regressed = blocked = 0
    for pair in arms.values():
        a, b = pair.get(control), pair.get(candidate)
        if a not in {"passed", "failed"} or b not in {"passed", "failed"}:
            blocked += 1
        else:
            improved += a == "failed" and b == "passed"
            regressed += a == "passed" and b == "failed"
    return {
        "pairs": len(arms),
        "blocked_pairs": blocked,
        "improved_pairs": improved,
        "regressed_pairs": regressed,
        "headline_delta": (improved - regressed) / len(arms)
        if arms and not blocked
        else None,
    }


def render_report(trials: Sequence[Trial], rows: Sequence[dict[str, Any]]) -> str:
    """生成可保存的 Markdown；详细证据仍在 protocol 和 observations 中。"""
    actual = index_results(trials, rows)
    lines = [
        "# Deta 评测报告",
        "",
        "数值来自本批计划与结果。planned/running/missing 不等于模型已经失败。",
        "",
    ]
    for value in summarize_indexed(trials, actual):
        lines.extend(
            [
                f"## 版本 {value['variant']}",
                "",
                "```json",
                json.dumps(value, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    variants = sorted({trial.variant_id for trial in trials})
    if len(variants) == 2:
        lines.extend(
            [
                f"## 版本配对：{variants[0]} → {variants[1]}",
                "",
                "```json",
                json.dumps(
                    compare_indexed(trials, actual, *variants),
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## 解释范围",
            "",
            "耗时只汇总有记录的样本；token 总量仅覆盖 usage 完整的试验。货币成本未计算。",
            "单次重复不能说明稳定性。结合 by_repeat 和逐任务记录解释波动；小样本不外推总体能力。",
            "初始项目和评分器需预先校准。具体限制及真实失败案例另见关联验收记录。",
            "",
        ]
    )
    return "\n".join(lines)
```

</details>

## 像调试器一样看一条 Trial

run_batch 在调用模型之前先保存 protocol.json 和所有 status=planned 的记录。它将每条记录交给 run_trial 执行；run_trial 原位补充 row，因此运行途中取消时，批次层仍能保存已得到的耗时和状态。取消、runner_error 分类及 finally 落盘留在 run_batch，评分器异常仍在 run_trial 内分类。取出一个 Trial 后，先保存 running，再复制初始项目；Runtime 的数据库不在 Agent 工作区内。Agent 完全结束后，先记下 run_status、usage 和 Trace 引用，再由 grade 执行验收。

status=passed 要同时满足 Run completed、Grade valid 和 Grade passed。Run limited/failed/cancelled 即使偶然留下可用文件，也不会被当成本轮完整通过。grader_error 表示验收器本身无法给结论，runner_error 表示复制、组装或环境等阶段失败；取消向外传播，后续尚未开始的计划仍保留 planned。

进程突然退出时，最后一行可能仍是 running。汇总把它作为未完成数据，不能自行改成已完成失败或成功。评测结果保存是这条评测流程的关键交付，因此 persist 失败会传播；它与“普通 Trace 导出失败不改变 Agent 结果”的观测规则不同。

本实现用原子替换的 observations.json 保存整个小批次，方便保留所有计划状态；没有同时维护第二份 JSONL 事实来源。规模变大时可以改成追加日志，但仍需从固定计划核对完整性。

## 正常 API 使用示例

先按上面字段从已确认的真实任务整理任务定义。下面入口接收这些对象与已配置的模型，不生成初始项目或验收案例。初版 Variant 比较同一实际源码上的提示词；模型和输出上限在整个批次固定。

```python
from collections.abc import Mapping, Sequence
from pathlib import Path

from deta.evaluation.runner import EvalTask, Variant, run_batch
from deta.model import ModelConfig


async def evaluate_prompts(
    tasks: Sequence[EvalTask],
    variants: Sequence[Variant],
    config: ModelConfig,
    *,
    initial_root: Path,
    batch_root: Path,
    context_window: int,
    environment: Mapping[str, str],
    repeats: int,
) -> None:
    await run_batch(
        tasks,
        variants,
        repeats=repeats,
        initial_root=initial_root,
        batch_root=batch_root,
        config=config,
        context_window=context_window,
        environment=environment,
    )
```

默认关闭正文采集，仍记录 Run、Trace、版本和结果。需要将本批真实 Badcase 下钻到请求正文时，显式传 capture_body=True 和合适的 redact 函数；正文不可用时保留诊断限制。

runner 与 CLI 都通过 Day 12 的 artifact_listener 记录 AgentEvent。开启正文采集时，两个入口都应产生带 run_id/trace_id/span_id 的事件 artifact；关闭时不把缺少事件正文解释成没有执行。通用 Python 调用方也需显式订阅这个函数。

生成报告时读取已经保存的计划和结果，不根据当前任务目录重新推导分母：

```python
import json
from pathlib import Path

from deta.evaluation.report import render_report
from deta.evaluation.runner import Trial


def write_batch_report(batch_root: Path) -> Path:
    protocol = json.loads((batch_root / "protocol.json").read_text(encoding="utf-8"))
    rows = json.loads((batch_root / "observations.json").read_text(encoding="utf-8"))
    trials = tuple(Trial.model_validate(item) for item in protocol["trials"])
    path = batch_root / "report.md"
    path.write_text(render_report(trials, rows), encoding="utf-8")
    return path
```

### 代码修复怎样完成双版本回归

上述 runner 直接比较同一源码上的提示词。代码修复使用两个独立代码环境，继续调用同一个 run_batch；首版的小批任务按下面步骤人工核对跨批结果，不增加环境调度器。

1. **先固定基线。** 修改前保存一份可运行的代码目录及依赖锁文件；项目已有 Git 时可以使用明确的提交和独立 checkout。当前教程目录尚未形成实际包时，先完成实现。基线与候选各用自己的虚拟环境和 Python 进程，记录实际导入的 deta 路径及 protocol 的 source_sha256。
2. **固定共同输入。** 两边使用同一份 EvalTask 定义、同一干净 initial_root、相同模型/窗口/输出上限/系统指令、预算、技能与重复次数，固定 shell 和工具依赖。若只比较 Agent 代码，评分器实现也保持一致；environment_keys 只能证明变量名一致，非秘密环境值和依赖版本仍需另行核对。
3. **运行基线。** 在基线环境调用上面的 run_batch，variants 只传一个 `Variant(id="control", instructions=固定指令)`，写入全新的 control 批次目录。保存 protocol.json、observations.json、report.md 和运行证据。
4. **运行候选。** 只修改已定位的 Agent 代码，在候选环境使用同一调用方式，variants 只传 `Variant(id="candidate", instructions=同一固定指令)`，写入另一个全新的 candidate 批次目录。每次仍由 runner 从共同初始项目复制工作区，不能复用基线已修改的产物。
5. **核对两个协议。** 对照 tasks、initial_versions、模型参数、context_window、系统指令、预算和重复次数；说明预期变化的 source_sha256。若任务、评分器、资源或环境还变了，先说明额外变量并重新固定，不能直接把差异归因于修复。
6. **从计划逐项配对。** 按两个 protocol 中的 `(task_id, repeat)` 对齐全部计划，再查各自 observations。两个批次生成的随机 Trial ID 不相同，应分别保留，不按 ID 相等连接。缺行、planned/running/cancelled、runner_error、grader_error 或无效 grade 都记为阻塞；有效 passed/failed 才比较改善、退化或持平。
7. **保存人工对照表。** 将下表填入独立的 code-comparison.md，链接两边协议、报告和逐项证据。记录原 Badcase 是否解决、固定任务有没有退化，以及所有阻塞项。存在阻塞配对时不发布整体提升；数据不足时仅报告当前样本。更多重复批次可交替基线/候选执行顺序，并说明模型服务随时间变化的限制。

| task_id / repeat | 基线 Trial / 状态 / grade | 候选 Trial / 状态 / grade | 结论 | 两边产物与检查证据 |
| --- | --- | --- | --- | --- |
| [来自固定计划] | [保留真实 ID 与状态] | [保留真实 ID 与状态] | 改善 / 退化 / 持平 / 阻塞 | [链接] |

`render_report` / `compare` 继续服务于各自批次的固定计划；上述跨代码环境比较采用人工表，不把两个 observations 文件直接拼接当成已校验的统一协议。首版不承诺自动创建代码环境、自动合并跨批报告或完整环境可复现。Day 15 可以引用这份明确标为人工核对的代码对照记录。

## 报告怎样保留缺失和波动

| 报告字段 | 正确读法 |
| --- | --- |
| planned 与 states | 全部预先计划的试验及当前状态分布 |
| pass_rate_over_planned | 计划交付率，未完成也在分母中；不等同于已评分样本的模型成功率 |
| by_repeat | 各重复批次的计划和通过数，配合逐任务记录观察波动 |
| run_seconds_median / elapsed_samples | AgentSession.prompt 的耗时中位数及样本量，含运行收尾；elapsed_seconds 是包含复制、组装、导出收尾与评分的整条 Trial 总耗时 |
| tokens_known_subset / trials_with_complete_usage | 仅完整 usage 的子集；没有样本时为 None，不以零代替未知 |
| improved_pairs / regressed_pairs | 同任务、同重复编号下的方向变化 |
| blocked_pairs / headline_delta | 任何一侧缺失或无有效结论时阻塞；有阻塞配对则不发布整体提升 |

汇总先核对计划的 Trial ID 和任务/版本/重复编号组合是否唯一，再核对每条结果的身份与计划一致；不能只靠 ID 相同就接受错位数据。compare 要求选择计划中两个不同版本。

报告的双版本自动展示按版本 ID 排序标明方向。需要指定 control/candidate 时调用 compare 显式传入，不能看见正数才重新定义哪一边是基线。小样本、单次重复与模型服务的时变因素都需要在解释范围里说明。

## 怎样核对

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
uv run deta --help
```

完成实际源码后再运行这些命令。本文参考答案的静态检查与真实模型运行、存储恢复、回放和评测分别记录；没有相应产物时填写“未验证”。

先拿既有已知正确产物和真实错误产物执行 grade，核对原因与证据，再投入真实调用成本。评分器本身未校准时，不对外解释版本提升。

运行已整理的一批任务，核对每个工作区来自同一份干净初始项目，各自使用独立 SQLite 和 artifact 根，原项目未变化。检查计划数是否等于任务数 × 版本数 × 重复次数，以及每个 Trial 是否都有结果或明确缺失状态。

对照已发生的 limited、模型失败、取消、缺失 usage 和评分器异常，确认报告不删除对应计划。用真实 Badcase 的原任务做修复前后复跑，再检查固定任务集有没有退化。没有实际运行的路径继续标未验证，不创建额外测试案例冒充评测产物。

## Pi 对照与下一天

| Pi 位置或语义 | Deta 对应及差异 |
| --- | --- |
| `packages/evals/src/plan.ts` | 提前展开任务、版本和重复编号，交替版本顺序 |
| `packages/evals/src/report.ts` | 精确配对，缺失/异常阻塞整体比较，未知指标不补零 |
| `packages/evals/README.md` 的隔离运行 | Deta 本页采用目录和 Session 隔离，不等同于 Pi 的容器/权限隔离 |
| Deta 的 token 与重复失败停止 | 在同一运行边界计量和报告，不作为 Pi Loop 的原生语义冒认 |

下一天进入 [Day 15：核心对照验收与工程交付](day15.md)：用已经产生的真实记录检查核心行为、完成 Badcase 闭环，并把实现范围和未验证路径写进交付文档。
