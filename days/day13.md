# Day 13：Badcase 定位与回放

[总览](summary.md) · [前一天](day12.md) · [下一天](day14.md)

## 核心问题

一次任务没有抛异常，却改错了文件。怎样从“回答看起来成功”走到可检查的失败原因？已经保存模型响应之后，怎样复查程序的调度行为，同时确保回放不会再次运行 bash 或修改原项目？

今天把失败现象整理为 Badcase，再通过已有模型与工具边界录制响应和消费记录。观察时间线、录制响应回放、真实环境复跑有不同的输入和结论，不能统称为“复现成功”。

## 今天新增什么

| 文件 | 本日变化 |
| --- | --- |
| `observability/artifacts.py` | EvidenceRef 与 Badcase，把预期、实际和根因证据分开 |
| `model.py` | ModelBoundary 描述真实单次请求与录制响应需要共享的调用形状 |
| `runtime.py` | model_call 默认为同一个 stream_once；正文与摘要都经过它 |
| `observability/replay.py` | 时间线读取、完整边界录制、严格参数匹配与离线消费 |

Loop、工具处理器、Session 与 Compaction 算法不重写。离线实例不持有真实 SDK 客户端，工具执行绑定整体替换为录制结果；资料不足时只能失败，没有调用真实模型或工具补齐的分支。

## 先定位和复跑，再完成严格回放

| 步骤 | 做什么 | 交付后去哪里 |
| --- | --- | --- |
| 13A：一条真实 Badcase | 使用已有 Session 时间线、Span 和请求/工具 artifact，记录预期、实际和最早偏离点；应用本章 model.py、runtime.py、artifacts.py 接入补丁 | 转到 Day 14 的 14A/14B，从干净项目复跑原任务并做小批回归 |
| 13B：严格录制回放 | 返回本章完成 replay.py；选择满足支持范围的真实 Run，录制模型与工具边界并离线消费 | 核对当前调度对这份记录是否一致，再完善重复运行与版本对照 |

13A 不需要先写 Recorder/Replay。Day 14 通过本章的 model_call 边界统一计量，依赖上述接入补丁，但不导入 replay.py。先修复和复跑已定位的问题，避免为了处理一条真实失败先实现完整回放器。Day 15 的最终交付仍核对 13B，不能把中间跳过解释为回放已完成。

完整答案保持按 Day 编号累计；如果在 Day 14 之后补写 replay.py，沿用当前 model_call 和预算接线，不覆盖 runtime.py 为 Day 13 的旧版本。下面的诊断顺序适用于所有 Badcase，完整录制回放只适用于下文列出的范围。

## 先区分三种复查

| 方式 | 实际做什么 | 能支持的结论 |
| --- | --- | --- |
| 时间线查看 | 读取已保存 Span、事件、请求与结果 artifact | 已记录的顺序、输入、输出和缺失位置 |
| 录制响应回放 | 正式 Loop 发出调用，边界校验参数后返回已录制响应 | 对这些录制输入，当前调度、提交和配对是否仍一致 |
| 隔离真实复跑 | 从干净项目重新调用真实模型与工具 | 本次真实任务是否满足验收；模型输出可能变化 |

离线回放不会重新实现文件修改效果，因此不能凭它证明代码产物正确。工具返回的“写入成功”只是原运行的录制信息，磁盘验收留给 Day 14。网络时序、提供方采样、真实取消窗口和进程副作用也不由这份回放模拟。

## 从现象到证据的定位顺序

1. 写下预期和实际。预期来自原任务和约束；实际来自最终文件、已有检查或外部结果，不能只复制 Agent 的完成声明。
2. 找到 run_id 与 Trace，确认这次运行是否正常结束、有限额停止、失败或采集缺失。
3. 找到最早可确认的偏离点。模型没有看到约束、工具参数选错、执行结果丢失、摘要遗漏约束和评分口径有误，是不同问题。
4. 从该点向前读最终请求，再看 Context 来源、资源版本和 Session Entry；从该点向后读完整响应、工具原始/最终结果及提交状态。
5. 记录证据支持的结论。根因尚不能确认时 root_cause 保持 None，给出下一步调查，不把分类标签当根因。

例如，现象是“最终文件越过允许路径”，定位需要同时核对原任务约束是否出现在该次最终请求、模型给出的 path、before_tool 的决策以及真实文件差异。仅有一个 edit_failed 错误码，既不能证明模型看错要求，也不能证明数据库没有保存。

## 先认识本日的类、属性与函数

| 对象 | 字段与责任 |
| --- | --- |
| `EvidenceRef` | path、可选 span_id、note，定位一段可下钻的依据 |
| `Badcase` | expected、actual、environment、category、root_cause、evidence、status、复跑关联 |
| `ModelBoundary` | 同一个单次模型调用契约，不拥有密钥、不执行工具循环 |
| `Recorder.refs / complete / closed` | 有序步骤引用、采集完整性、Run 结束后的封口状态 |
| `Replay.steps / position` | 已预检的步骤与消费位置，不能跳过一个记录去找更像的下一条 |
| `ReplayMismatch` | 缺失、被改写、不匹配或未消费完整，明确拒绝完整回放 |

| 函数 | 谁调用、输入、返回给谁 |
| --- | --- |
| `model_key` | 录制与回放边界调用；配置、指令、消息、schemas → 实际输入对比对象 |
| `tool_key` | ToolCall 与本次 RequestPlan → 调用 ID、结构化参数和工具声明 |
| `Recorder.attach` | 在新 Runtime 启动前绑定包装函数，真实调用仍交给原边界 |
| `Recorder.finish` | Run 完整结束后封口，返回索引 artifact 路径 |
| `read_record` | artifact 路径 → 已核对范围、大小与原始指纹的正文 |
| `Replay.take` | kind 与实际输入 → 严格匹配的下一步记录 |
| `Replay.attach / finish` | 安装离线边界；运行后核对是否全部消费且终态一致 |
| `timeline` | spans.jsonl → 按开始时间排序的已保存 Span，不执行任何任务 |

ModelBoundary 是因为现在确实需要替换真实调用而引入的一个接口。没有给 Context、SQL、每个纯函数都再抽一层 Protocol。

## 首版完整回放的支持范围

首版要求一次从空 Session 开始、仅消费初始 prompt、最终 completed 的录制 Run。它可以包含普通工具业务错误及模型据此修正，也可以包含成功的阈值压缩和摘要请求。`runtime.model_call` 位于单次请求边界，所以摘要响应同样会录制，而不需要绕开正式 Compaction。

以下情况仍能查看已有诊断，但索引不允许声称完整回放：未封口、真实尝试曾抛异常、取消、缺失记录、正文关闭、采集超限、脱敏改变了必要字段。已重试后成功的运行如果包含失败尝试，也因首版没有该异常的可重放编码而拒绝完整回放。

首版没有录制队列注入时机。运行中消费了 Steering、Follow-up 或 Hook 追加的用户输入时，Recorder.finish 会记录 input_capture=additional_input_unsupported，并将 complete 置为 False。正式运行仍支持这些输入；这份回放不能凭模型响应猜出它们何时进入队列。

Day 12 已把系统资源固定在 Run 开始时。回放工作区需要提供与原 Run 开始时一致的 AGENTS.md 和已启用技能；录制工具不重做磁盘修改，原运行中的资源编辑也不会自动改变该 Run 的系统快照。外部状态的真实效果仍由隔离复跑检查。恢复会话的初始事实导入、失败流分片和真实时间间隔可以以后单独扩展，不能让它们默默回退到在线请求。

## 记录形状与完整性

```text
replay-index
  body: version、closed、complete、input_capture、prompt、run_id、status、answer、steps[]
  sha256: 对脱敏之前 body 的指纹

replay-step（按实际完成的串行边界排序）
  body:
    kind: model 或 tool
    input: 实际调用参数
    updates: 模型增量，或工具开始/输出通知
    result: 完整 AIMessage 或最终 ToolMessage
  sha256: 对脱敏之前 body 的指纹
```

两层原始指纹用于识别采集后内容变化，不是数字签名，也不证明不可信记录真实。Artifact 保存仍执行脱敏；如果脱敏改变了必要正文，重新计算的指纹就不匹配，回放拒绝，不能为了回放把密钥原样保存。

模型参数比较排除重新创建的 session_id、entry_id、trace_id、span_id，因为这些本来应不同；实际提供方消息、系统指令、模型、输出上限与 schemas 必须相同。工具比较保留 call["id"]、args 字典与声明顺序，比较结构化参数值，不比较原始 JSON 的空格或键顺序。

## 对已有边界的接入补丁

相对于 Day 12 完成状态。Runtime._model_once 继续调用原来的 stream_once，默认在线行为保持在这一个实现里。允许 client=None 是为了离线入口；没有安装回放边界时，这种实例调用真实适配器会直接报错。

<details>
<summary>接入补丁：src/deta/model.py（相对 Day 12 完成状态）</summary>

```diff
--- a/src/deta/model.py
+++ b/src/deta/model.py
@@ -1,7 +1,7 @@
 import asyncio
 from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
 from contextlib import aclosing, asynccontextmanager
-from typing import cast
+from typing import Protocol, cast

 import httpx
 from langchain_core.messages import (
@@ -207,3 +207,19 @@
         except BaseException as exc:
             request.set_status(Status(StatusCode.ERROR, type(exc).__name__))
             raise
+
+
+class ModelBoundary(Protocol):
+    """真实请求和录制响应共享的单次调用形状；不执行 Loop 或工具。"""
+
+    async def __call__(
+        self,
+        config: ModelConfig,
+        instructions: str,
+        messages: Sequence[AgentMessage],
+        tools: Sequence[ToolSchema],
+        *,
+        listeners: Sequence[Listener] = (),
+        before_attempt: Callable[[], int] | None = None,
+        input_sources: JsonValue = None,
+    ) -> AIMessage: ...
```

</details>

<details>
<summary>接入补丁：src/deta/runtime.py（相对 Day 12 完成状态）</summary>

```diff
--- a/src/deta/runtime.py
+++ b/src/deta/runtime.py
@@ -19,7 +19,7 @@
 from deta.context import build_context, estimate_context, input_fingerprint, remap_items
 from deta.events import Event, Listener, TextDelta, ToolCallDelta
 from deta.hooks import Hooks, LoopBindings, RequestPlan, TurnDecision, TurnReport
-from deta.model import ModelConfig, stream_once
+from deta.model import ModelBoundary, ModelConfig, stream_once
 from deta.observability.artifacts import Artifacts, source_version
 from deta.resources import ResourceBundle, load_resources, render_resources
 from deta.session import Session
@@ -70,7 +70,7 @@

     def __init__(
         self,
-        client: ChatOpenAI,
+        client: ChatOpenAI | None,
         config: ModelConfig,
         workspace: Path,
         tracer: Tracer,
@@ -91,6 +91,7 @@
         """保存外部依赖与控制回调；核心对象不会在导入或构造时请求模型。"""
         # 调用方负责关闭的 SDK 客户端，必须关闭其内部重试。
         self.client = client
+        self.model_call: ModelBoundary = self._model_once
         # 单次模型请求参数。
         self.config = config
         # 工具路径与命令 cwd 的共同基准。
@@ -406,14 +407,11 @@
                     listener(event.model_copy(deep=True))

                 try:
-                    message = await stream_once(
-                        self.client,
+                    message = await self.model_call(
                         self.config,
                         instructions,
                         plan.messages,
                         schemas,
-                        tracer=self.tracer,
-                        artifacts=self.artifacts,
                         listeners=[observe],
                         before_attempt=budget.take_request,
                         input_sources=sources,
@@ -569,14 +567,11 @@
             set_status_on_exception=False,
         ) as stage_span:
             try:
-                return await stream_once(
-                    self.client,
+                return await self.model_call(
                     config,
                     prompt,
                     messages,
                     (),
-                    tracer=self.tracer,
-                    artifacts=self.artifacts,
                     before_attempt=budget.take_request,
                     input_sources={
                         "purpose": "compaction",
@@ -700,3 +695,30 @@
         selected = (*self._active_skills, name)
         load_resources(self.workspace, selected)
         self._active_skills = selected
+
+    async def _model_once(
+        self,
+        config: ModelConfig,
+        instructions: str,
+        messages: Sequence[AgentMessage],
+        tools: Sequence[ToolSchema],
+        *,
+        listeners: Sequence[Listener] = (),
+        before_attempt: Callable[[], int] | None = None,
+        input_sources: JsonValue = None,
+    ) -> AIMessage:
+        """绑定真实 SDK；离线运行没有客户端，未安装回放边界时直接失败。"""
+        if self.client is None:
+            raise RuntimeError("离线入口没有真实模型客户端")
+        return await stream_once(
+            self.client,
+            config,
+            instructions,
+            messages,
+            tools,
+            tracer=self.tracer,
+            artifacts=self.artifacts,
+            listeners=listeners,
+            before_attempt=before_attempt,
+            input_sources=input_sources,
+        )
```

</details>

<details>
<summary>接入补丁：src/deta/observability/artifacts.py（相对 Day 12 完成状态）</summary>

```diff
--- a/src/deta/observability/artifacts.py
+++ b/src/deta/observability/artifacts.py
@@ -2,9 +2,12 @@
 import logging
 from collections.abc import Callable
 from pathlib import Path
+from typing import Literal
 from uuid import uuid4

 from pydantic import JsonValue
+
+from deta.types import Data

 logger = logging.getLogger(__name__)

@@ -89,3 +92,27 @@
         digest.update(path.relative_to(package).as_posix().encode() + b"\0")
         digest.update(path.read_bytes() + b"\0")
     return digest.hexdigest()
+
+
+class EvidenceRef(Data):
+    """一条可以下钻的证据；path 可以引用请求、Span 导出或验收产物。"""
+
+    path: str
+    span_id: str | None = None
+    note: str
+
+
+class Badcase(Data):
+    """记录现象与推断的边界；没有定位依据时允许根因为空。"""
+
+    case_id: str
+    run_id: str
+    expected: str
+    actual: str
+    environment: dict[str, JsonValue]
+    category: str
+    root_cause: str | None = None
+    evidence: tuple[EvidenceRef, ...]
+    status: Literal["open", "diagnosed", "fixed", "verified"] = "open"
+    fix_revision: str | None = None
+    rerun_ids: tuple[str, ...] = ()
```

</details>

## 完整练习骨架

本节属于 13B。完成 13A 接入补丁后，可以先完成 Day 14 的原任务复跑和小批回归，再回来填写本节。

### src/deta/observability/replay.py

只填写 model_key、tool_key、read_record、Replay.take、Replay.attach、Replay.finish。Recorder 的真实调用包装与有界采集直接提供；不要在回放的 TODO 中调用 stream_once、execute_tool、read/write/edit/bash 或原工作区。

```python
import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import AIMessage, HumanMessage, ToolCall, ToolMessage
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
    # TODO：完成 model_key，保留本文约定的输入、输出与失败边界。
    raise NotImplementedError("请完成 model_key")


def tool_key(call: ToolCall, plan: RequestPlan) -> JsonValue:
    """调用 ID、结构化参数和当时工具声明都必须一致，不按工具名猜匹配。"""
    # TODO：完成 tool_key，保留本文约定的输入、输出与失败边界。
    raise NotImplementedError("请完成 tool_key")


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
    # TODO：完成 read_record，保留本文约定的输入、输出与失败边界。
    raise NotImplementedError("请完成 read_record")


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
        # TODO：完成 Replay.take，保留本文约定的输入、输出与失败边界。
        raise NotImplementedError("请完成 Replay.take")

    def attach(self, runtime: "AgentSession") -> None:
        """新工作区、新 Store 和空 Session；回放的 intent 仅写入这份临时数据库。"""
        # TODO：完成 Replay.attach，保留本文约定的输入、输出与失败边界。
        raise NotImplementedError("请完成 Replay.attach")

    def finish(self, result: RunResult) -> None:
        """Loop 把内部异常变成 RunResult 后，这一步仍需检查消费完整性和终态。"""
        # TODO：完成 Replay.finish，保留本文约定的输入、输出与失败边界。
        raise NotImplementedError("请完成 Replay.finish")


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
```

## 完整参考答案

<details>
<summary>参考答案：src/deta/observability/replay.py（完整文件）</summary>

```python
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
```

</details>

append_update 接收当前 captured_bytes，返回追加后的字节数；观察回调用 nonlocal 更新该整数。达到原有条数或字节上限时返回原计数、标记录制不完整，仍照常转发事件。无需用单元素列表模拟可变整数。

## 像调试器一样看一次工具回放

Loop 先提交录制 AIMessage，Session 因此登记相同 call["id"] 的 announced。执行绑定进入 Replay 的 tool，先比较 tool_key；对应步骤里若记录了 start，就只在新的临时 Session 保存 intent，并发布 on_start。

后续输出通知来自 updates，返回值来自录制的最终 ToolMessage。Loop 仍执行 commit(result)，让同一份临时数据库原子结清工具状态。文件 handler、子进程以及 before_tool/after_tool 的原始执行效果不会再发生；这里回放的是经过原 Hook 后的结果。请求准备和 finish_turn 仍由正式运行链执行，需要使用可复查、无外部副作用的相同 Hook 配置。

输入不匹配时 take 不寻找“相似记录”。ReplayMismatch 可能被 Agent 包成 failed 的 RunResult，因此调用方还必须执行 Replay.finish；只看 prompt 返回了一个对象并不能认定回放成功。

## 正常 API 使用示例

录制函数接收一个启用了合适正文采集与脱敏配置、尚无历史的真实 Runtime。录制只包住这次任务，不创建虚构响应。

```python
from deta.observability.replay import Recorder
from deta.runtime import AgentSession
from deta.types import RunResult


async def record_run(
    runtime: AgentSession, prompt: str
) -> tuple[RunResult, str | None]:
    recorder = Recorder(runtime.artifacts)
    recorder.attach(runtime)
    result = await runtime.prompt(prompt)
    return result, recorder.finish(result, prompt)
```

回放入口必须先由调用方创建独立工作区、Store、Session、artifact 根和 client=None 的 Runtime，并安装与原运行一致的指令、资源和选项。不要指向原 SQLite；引用原录制 artifact 是只读操作。绝对路径包含工作区差异时，应报告参数不一致，本版不自动重写路径。

```python
from pathlib import Path

from deta.observability.replay import Replay
from deta.runtime import AgentSession
from deta.types import RunResult


async def replay_run(runtime: AgentSession, index_path: Path) -> RunResult:
    replay = Replay(index_path)
    replay.attach(runtime)
    result = await runtime.prompt(replay.index["prompt"])
    replay.finish(result)
    return result
```

Badcase 可用 artifacts.save("badcase", case.model_dump(mode="json")) 保存；保存返回 None 时明确告诉诊断调用方产物不可用。status 由证据推进：open → diagnosed → fixed → verified；改完代码至多是 fixed，原案例真实复跑与固定任务回归通过后才进入 verified。

本日提供 Python 诊断入口，不额外声称 CLI 已经有 replay 或 badcase 子命令。

## 怎样核对

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
uv run deta --help
```

完成实际源码后再运行这些命令。本文参考答案的静态检查与真实模型运行、存储恢复、回放和评测分别记录；没有相应产物时填写“未验证”。

从真实任务中选一条已有明确验收失败的记录，先完成 expected/actual 和证据引用。若旧记录缺正文或包含未支持的异常，就如实拒绝完整回放，保留时间线定位结果；在新的隔离任务中启用合适采集，不能拿虚构 tape 补齐旧记录。

回放时核对每个 model/tool 输入的顺序、完整消费和终态，并检查新 Session 的工具配对。确认边界中没有真实客户端或工具 handler 路径，原工作区与原 Session 保持不变。仅靠 Span 名里写着 replay 不能证明没有外部操作。

对于实际缺失或脱敏不完整的产物，记录拒绝原因。把“录制回放通过”与“修复后真实复跑通过”放在不同验收行；不要新增测试脚手架来代替这两类运行。

## Pi 对照与下一天

| 参考或新增能力 | 本日边界 |
| --- | --- |
| Pi 的会话身份与观测关联 | 沿用 Session / Run / Span 关联思路 |
| Pi telemetry 设计文档 | 只参考已经核对的职责，不宣称 Pi 提供了完整 Badcase 回放系统 |
| Badcase 字段、索引完整性、严格 tape 消费 | Deta 本版增加的诊断能力 |
| 正式运行链复用 | Loop、Session、Context 与 Compaction 不被录制回放另行实现 |

下一天进入 [Day 14：隔离评测与版本比较](day14.md)：用真实模型和工具重新执行固定任务，把“调度没有变坏”和“任务结果正确”分别检验。
