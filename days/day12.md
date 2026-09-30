# Day 12：项目指令、Skills 与观测补齐

[总览](summary.md) · [前一天](day11.md) · [下一天](day13.md)

## 核心问题

Agent 读到了项目规则和技能之后，上下文压缩会不会把它们一起丢掉？两个独立 Agent 同时运行时，怎样从请求、工具和摘要记录中分清是谁产生的事件？

今天在 Run 开始时读取资源快照，并把这份快照安装到该 Run 每次请求的系统指令部分，同时补齐正文采集、运行清单和事件来源。资源加载仍由运行时调用，Context 继续投影 Session；观测仍只记录，不替代执行决策或数据库提交。

## 今天新增什么

| 文件 | 本日变化 |
| --- | --- |
| `resources.py` | 有目录作用范围的 AGENTS.md、技能目录、按名启用与资源版本 |
| `hooks.py`、`runtime.py` | Run 开始时读取一次资源；RequestPlan 携带来源，自动压缩沿用该快照，手动压缩单独读取一次 |
| `observability/artifacts.py` | 已保存、未采集、失败、脱敏计数；非秘密清单与源码指纹 |
| `observability/tracing.py`、`cli.py` | 提供共享的 artifact_listener，在事件产生时绑定 Run / Trace / Span；Day 14 评测入口复用 |

本日没有新增 Agent 编排器或插件系统。两个独立实例的并发验收，只检查状态、取消和观测是否隔离。

## 分步接入资源与观测

1. **根目录规则。** 先用已有根 AGENTS.md 走通“读取 → Run 快照 → 系统指令 → 实际任务”。一次 Run 中的后续模型请求复用同一资源版本。
2. **技能目录与作用域。** 加入技能目录、按需 read 和显式 use_skill，再用项目已有子目录规则核对 scope。完整答案仍发现工作区内所有规则；先理解根规则，再检查深层规则的优先级。
3. **两个入口的观测。** CLI 显式安装 artifact_listener；Day 14 runner 使用同一个函数。最后检查 manifest、正文采集开关和两个独立 Runtime 的身份隔离。

三个步骤可以分别验收。无需为资源增加后台监视器或通用缓存；本版用 Run 边界决定何时刷新。

## 先看资源从哪里进入请求

```text
工作区里的 AGENTS.md 与 .agents/skills/*/SKILL.md
  → begin_run 时 load_resources(workspace, active_names)，一次 Run 只读取一次
  → ResourceBundle
       instructions   带 scope 的项目指令正文
       skills         名称、描述、路径与指纹
       active         调用方已按名启用的技能正文
  → 后续请求与自动压缩复用该快照，render_resources(bundle)
  → RequestPlan.instructions + resource_sources
  → 请求 Hook / transform_context
  → 最终输入快照 → 提供方请求
```

项目资源不是 Session 的新 HumanMessage，不因为每次请求安装就不断追加历史。build_context 压缩的是会话视图；系统指令、资源正文和工具声明在后续请求重新安装，也一起计入输入预算。

### 资源什么时候更新

- `_begin_run` 先读取资源，再登记持久化 Run；加载失败返回 failed，并由 Run Span 记录失败，不留下数据库中的 running 记录。资源错误不能被 manifest 的诊断错误处理吞掉。
- `_prepare_request` 只使用该 Run 的 `_resources`，模型重试和压缩后的请求重建不会重新遍历目录。manifest 与 RequestPlan 引用同一份资源版本。
- 运行中修改 AGENTS.md 或技能文件，不改变该 Run 已安装的规则。规则更新在下一次 `prompt` / `continue_` 启动的新 Run 生效；需要提前采用新规则时，结束当前 Run 再启动下一次。普通 read 工具仍读取调用时的实际文件，读到的新正文不自动替换系统资源快照。
- `compact()` 在空闲维护期间单独读取一次资源，用于这次候选预算检查。后续新 Run 仍按自己的开始边界读取。
- `use_skill()` 只在空闲时校验并保存名称；其校验可以读取资源，但不在活动 Run 中刷新规则。正文版本由下一次 Run 或手动压缩固定。

## 项目指令的作用范围和合并顺序

本版资源根就是传入的 workspace。只发现该根内的 AGENTS.md，不自动向用户主目录搜索。目录遍历跳过 .git、.venv、.deta、node_modules 和 __pycache__，不跟随符号链接；指令单文件上限 32 KiB，发现的资源总量上限 128 KiB，超限明确失败。

以项目确实存在的目录关系为准：根 AGENTS.md 作用于整个工作区；子目录的 AGENTS.md 只作用于该目录和后代路径。同一目录链由浅到深，深层同主题规则优先；两个不同子目录的规则互不覆盖。当前用户要求优先于项目文件。

首版将发现的项目指令都按“深度、路径”排序，并把 scope 标在正文旁，交给模型按目标路径适用。它没有根据工具路径实时切换的权限系统，也不把某个后端目录规则无条件套到前端。bash 命令可能影响多个目录，路径标签不能构成进程沙箱。

## Skills 怎样渐进加载

| 阶段 | 读什么、谁消费 |
| --- | --- |
| 发现 | 加载本地文件以解析元数据和指纹；只把 name/description/path 放进模型目录 |
| 普通按需读取 | 模型可以用已有 read 工具读取工作区内的 SKILL.md，结果进入普通会话 |
| 显式启用 | 调用方空闲时执行 use_skill(name)，下一次 Run 固定技能正文，之后每次请求安装这份快照 |
| 解析引用 | 技能正文中的相对路径以 SKILL.md 所在目录为基准，工具实际路径仍按 workspace 解析 |

“正文按需”指不把所有技能正文发送给模型，不能误称本地完全没读过正文。指纹和元数据解析本身需要读取文件。首版只发现 `.agents/skills/<目录>/SKILL.md`，不支持外部技能库、自动执行技能脚本或完整 Pi 技能格式。

frontmatter 明确只支持单行 name 和 description；需要引号时用 JSON 双引号字符串。多行 YAML、额外字段、重名技能和无效文件会报错，不悄悄跳过并声称已加载。

项目指令与技能通过同一次目录遍历发现。扫描遇到权限或 I/O 错误会使本次加载失败，不能把无法读取解释成没有规则。每读取一个资源就累计字节并检查总额度，超限立即停止继续加载；不先把所有正文留在内存里再报错。

普通 read 的技能正文仍可能随历史被摘要。需要贯穿压缩的技能，应由调用方通过 use_skill 显式启用；启用列表属于本 Runtime，重启后由调用方重新指定，manifest 会记录当次选择。本日不把一次 read 成功暗中升级成永久技能配置。

## 先认识本日的类、属性与函数

| 对象或函数 | 输入、输出与调用方 |
| --- | --- |
| `ResourceFile` | path、scope、sha256、content，说明一份正文的身份与范围 |
| `SkillInfo` | name、description、path、sha256，只供目录发现和选择 |
| `ResourceBundle.versions` | 项目规则与技能文件的版本列表，随请求来源及 manifest 保存 |
| `AgentSession._resources` | 本次 Run 的资源快照；请求重建与自动压缩期间不变，下一 Run 重新读取 |
| `read_resource` | 工作区内路径 → 有界 UTF-8 正文，拒绝链接与越界 |
| `parse_skill` | SKILL.md 正文与来源 → 名称、描述；不执行内容 |
| `load_resources` | workspace、active_names → 一份完整资源快照 |
| `render_resources` | ResourceBundle → 明确标识作用范围的指令文本 |
| `AgentSession.use_skill` | 空闲时按名选择，下一次准备请求安装正文 |
| `source_version` | 当前加载包的 Python 源文件 → 内容指纹，未提交代码也能区分 |
| `event_record` | 同步产生的 AgentEvent → 附带产生位置 Trace/Span ID 的记录 |
| `artifact_listener` | Artifacts → 普通 Listener；CLI、评测和 Python 调用方显式订阅同一记录函数 |

## 对已有请求和观测边界的接入补丁

相对于 Day 11 的累计完成状态应用下列补丁。新增资源模块的练习与答案在后面。正文仍通过 Artifacts.capture_body 控制，metadata 入口只接收调用方筛选后的非秘密字段，不能拿它绕过正文采集开关。

<details>
<summary>接入补丁：src/deta/hooks.py（相对 Day 11 完成状态）</summary>

```diff
--- a/src/deta/hooks.py
+++ b/src/deta/hooks.py
@@ -32,6 +32,8 @@
     context_items: tuple[ContextItem, ...] = ()
     # 构建请求时读取的会话末尾条目，用于定位输入对应的历史快照。
     context_tip: str | None = None
+    # 本次资源来源及内容版本；指令正文仍以最终 instructions 为准。
+    resource_sources: tuple[tuple[str, str], ...] = ()
     # 构建视图时没有进入请求的条目与原因；原记录不删除。
     excluded_entries: tuple[tuple[str, str], ...] = ()

```

</details>

<details>
<summary>接入补丁：src/deta/runtime.py（相对 Day 11 完成状态）</summary>

```diff
--- a/src/deta/runtime.py
+++ b/src/deta/runtime.py
@@ -20,7 +20,8 @@
 from deta.events import Event, Listener, TextDelta, ToolCallDelta
 from deta.hooks import Hooks, LoopBindings, RequestPlan, TurnDecision, TurnReport
 from deta.model import ModelConfig, stream_once
-from deta.observability.artifacts import Artifacts
+from deta.observability.artifacts import Artifacts, source_version
+from deta.resources import ResourceBundle, load_resources, render_resources
 from deta.session import Session
 from deta.tools import TOOLS, execute_tool, tool_schemas
 from deta.types import (
@@ -125,6 +126,9 @@
             raise ValueError("压缩保留目标和摘要输出额度必须为正")
         self.keep_recent_tokens = keep_recent_tokens
         self.summary_output_tokens = summary_output_tokens
+        self._active_skills: tuple[str, ...] = ()
+        # 每次 Run 开始时读取一次；本次请求与自动压缩共享同一资源版本。
+        self._resources: ResourceBundle | None = None
         self._maintenance = False
         self._threshold_tips: set[str] = set()
         # 活动运行、临时消息视图与队列所有者；最终历史由 Session 保存。
@@ -200,17 +204,68 @@
             }
         )
         self._threshold_tips.clear()
+        # 先读资源再登记 Run；加载失败时不留下无法收尾的 running 记录。
+        self._resources = None
+        resources = load_resources(self.workspace, self._active_skills)
         self.session.start_run(run_id, config)
+        self._resources = resources
+        # 诊断失败不回滚已成功登记的 Run；这里只记录不含正文和秘密的清单。
+        try:
+            manifest = {
+                "run_id": run_id,
+                "session_id": self.session.id,
+                "source_sha256": source_version(Path(__file__).parent),
+                "model": self.config.model,
+                "max_completion_tokens": self.config.max_completion_tokens,
+                "instructions_sha256": hashlib.sha256(
+                    self.instructions.encode()
+                ).hexdigest(),
+                "schemas_sha256": hashlib.sha256(
+                    json.dumps(tool_schemas(self.tools), sort_keys=True).encode()
+                ).hexdigest(),
+                "resources": [list(item) for item in resources.versions],
+                "active_skills": list(self._active_skills),
+                "capture_body": self.artifacts.capture_body,
+                "format_version": 1,
+            }
+            self.artifacts.metadata(
+                "manifest", TypeAdapter(JsonValue).validate_python(manifest)
+            )
+        except Exception:
+            self.artifacts.failed += 1

     async def _end_run(self, result: RunResult) -> None:
         """在 Agent 结束通知前保存终态；失败会改变向调用方返回的运行结果。"""
         self.session.finish_run(result)
+        self.artifacts.metadata(
+            "capture-status",
+            {
+                "run_id": result.run_id,
+                "saved": self.artifacts.saved,
+                "skipped": self.artifacts.skipped,
+                "failed": self.artifacts.failed,
+                "redacted_strings": self.artifacts.redacted,
+                "trace_delivery": "not_guaranteed; inspect exporter warnings and span completeness",
+            },
+        )

     async def _prepare_request(self, messages: tuple[AgentMessage, ...]) -> RequestPlan:
         """从 Session 构建视图，再应用请求 Hook；Agent 的列表只用于活动状态和协议检查。"""
-        view = build_context(self.session.entries())
+        with self.tracer.start_as_current_span(
+            "deta.context.build",
+            record_exception=False,
+            set_status_on_exception=False,
+        ) as stage_span:
+            try:
+                view = build_context(self.session.entries())
+                resources = self._resources
+                if resources is None:
+                    raise RuntimeError("本次 Run 尚未加载资源快照")
+            except BaseException as exc:
+                stage_span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
+                raise
         plan = RequestPlan(
-            self.instructions,
+            self.instructions + "\n\n" + render_resources(resources),
             tuple(item.model_copy(deep=True) for item in view.messages),
             MappingProxyType(dict(self.tools)),
         )
@@ -227,6 +282,7 @@
             tools=MappingProxyType(dict(plan.tools)),
             context_items=items,
             context_tip=view.tip_id,
+            resource_sources=resources.versions,
             excluded_entries=(*view.excluded, *missing),
         )

@@ -298,7 +354,8 @@
             {
                 "session_id": self.session.id,
                 "context_tip": plan.context_tip,
-                "system": "runtime instructions and tool-change notice",
+                "system": "runtime, scoped resources, request Hook and tool-change notice",
+                "resources": [list(item) for item in plan.resource_sources],
                 "messages": [
                     {
                         "provider_index": index + 1,
@@ -473,7 +530,11 @@
                 return await self._compact(
                     "manual",
                     RunBudget(self.agent.options),
-                    self.instructions,
+                    self.instructions
+                    + "\n\n"
+                    + render_resources(
+                        load_resources(self.workspace, self._active_skills)
+                    ),
                     tool_schemas(self.tools),
                 )
         finally:
@@ -631,3 +692,11 @@
             except BaseException as exc:
                 span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                 raise
+
+    def use_skill(self, name: str) -> None:
+        """空闲时选择技能；下一次 Run 或手动压缩读取正文，活动 Run 不刷新资源。"""
+        if self.agent.running or self._maintenance:
+            raise RuntimeError("只能在空闲时修改显式技能选择")
+        selected = (*self._active_skills, name)
+        load_resources(self.workspace, selected)
+        self._active_skills = selected
```

</details>

<details>
<summary>接入补丁：src/deta/observability/artifacts.py（相对 Day 11 完成状态）</summary>

```diff
--- a/src/deta/observability/artifacts.py
+++ b/src/deta/observability/artifacts.py
@@ -32,12 +32,17 @@
         self.capture_body = capture_body
         # 调用方提供的字符串脱敏函数；打开正文采集时必须显式提供。
         self.redact = redact
+        self.saved = 0
+        self.skipped = 0
+        self.failed = 0
+        self.redacted = 0

     def save(self, kind: str, payload: JsonValue) -> str | None:
         """接收产物类别 kind 和 JSON 数据 payload，按配置脱敏并保存为独立文件。
         请求或工具边界调用它取得文件路径；未开启采集或保存失败时返回 None。
         """
         if not self.capture_body:
+            self.skipped += 1
             return None

         def clean(value: JsonValue) -> JsonValue:
@@ -45,7 +50,10 @@
             列表和字典保持原有层级，数字等值直接返回；结果交给 save 写入文件。
             """
             if isinstance(value, str):
-                return self.redact(value) if self.redact is not None else value
+                cleaned = self.redact(value) if self.redact is not None else value
+                if cleaned != value:
+                    self.redacted += 1
+                return cleaned
             if isinstance(value, list):
                 return [clean(item) for item in value]
             if isinstance(value, dict):
@@ -57,7 +65,27 @@
             path = self.root / f"{kind}-{uuid4().hex}.json"
             with path.open("x", encoding="utf-8") as output:
                 json.dump(clean(payload), output, ensure_ascii=False, indent=2)
+            self.saved += 1
             return str(path)
         except Exception as exc:
+            self.failed += 1
             logger.warning("artifact unavailable: %s", type(exc).__name__)
             return None
+
+    def metadata(self, name: str, payload: JsonValue) -> str | None:
+        """保存调用方筛选后的非秘密运行清单；与正文采集开关分开，仍脱敏并隔离错误。"""
+        writer = Artifacts(
+            self.root.parent, capture_body=True, redact=self.redact or str
+        )
+        return writer.save(name, payload)
+
+
+def source_version(package: Path) -> str:
+    """记录实际加载包内 Python 源码的内容指纹，未提交改动同样会改变它。"""
+    import hashlib
+
+    digest = hashlib.sha256()
+    for path in sorted(package.rglob("*.py")):
+        digest.update(path.relative_to(package).as_posix().encode() + b"\0")
+        digest.update(path.read_bytes() + b"\0")
+    return digest.hexdigest()
```

</details>

<details>
<summary>接入补丁：src/deta/observability/tracing.py（相对 Day 11 完成状态）</summary>

```diff
--- a/src/deta/observability/tracing.py
+++ b/src/deta/observability/tracing.py
@@ -1,5 +1,12 @@
 import logging
 import threading
+from typing import TYPE_CHECKING
+
+if TYPE_CHECKING:
+    from pydantic import JsonValue
+
+    from deta.events import AgentEvent, Event, Listener
+    from deta.observability.artifacts import Artifacts
 from collections.abc import Iterator
 from contextlib import contextmanager
 from pathlib import Path
@@ -62,3 +69,27 @@
         worker.join(timeout=1.0)
         if worker.is_alive():
             logger.warning("trace flush incomplete: shutdown timeout")
+
+
+def event_record(event: "AgentEvent") -> dict[str, "JsonValue"]:
+    """在同步事件产生边界取得 OTel 身份；不可延迟到后台再读取当前 Span。"""
+    from opentelemetry.trace import get_current_span
+
+    context = get_current_span().get_span_context()
+    return {
+        "run_id": event.run_id,
+        "trace_id": f"{context.trace_id:032x}" if context.is_valid else None,
+        "span_id": f"{context.span_id:016x}" if context.is_valid else None,
+        "event": event.model_dump(mode="json"),
+    }
+
+
+def artifact_listener(artifacts: "Artifacts") -> "Listener":
+    """为 CLI、评测和 Python 调用方提供同一个同步事件记录入口。"""
+    from deta.events import AgentEvent
+
+    def record(event: "Event") -> None:
+        if isinstance(event, AgentEvent):
+            artifacts.save("event", event_record(event))
+
+    return record
```

</details>

<details>
<summary>接入补丁：src/deta/cli.py（相对 Day 11 完成状态）</summary>

```diff
--- a/src/deta/cli.py
+++ b/src/deta/cli.py
@@ -13,7 +13,7 @@
 from deta.events import AgentEvent, Event, TextDelta
 from deta.model import ModelConfig, open_model
 from deta.observability.artifacts import Artifacts
-from deta.observability.tracing import local_tracing
+from deta.observability.tracing import artifact_listener, local_tracing
 from deta.runtime import AgentSession
 from deta.session import Session
 from deta.storage import SQLiteStore
@@ -76,7 +76,7 @@
                     for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
                     if name in os.environ
                 },
-                listeners=[show],
+                listeners=[artifact_listener(artifacts), show],
             )
             result = (
                 await runtime.continue_(run_id=run_id)
```

</details>

## 完整练习骨架

### src/deta/resources.py

只填写 parse_skill、load_resources、render_resources。路径检查和数据对象直接提供。练习重点是“目录作用范围、目录项与正文、启用后的持续安装”三种关系，不增加通用资源注册器。

```python
import hashlib
import json
import os
from pathlib import Path

from deta.types import Data

MAX_RESOURCE_BYTES = 32 * 1024
MAX_TOTAL_BYTES = 128 * 1024
SKIP = {".git", ".venv", ".deta", "node_modules", "__pycache__"}


class ResourceFile(Data):
    """一份有明确作用目录的指令正文；摘要与 Context 不拥有它。"""

    path: str
    scope: str
    sha256: str
    content: str


class SkillInfo(Data):
    """技能目录项；发现阶段不把正文放进模型输入。"""

    name: str
    description: str
    path: str
    sha256: str


class ResourceBundle(Data):
    """一次 Run 或手动压缩使用的资源快照，来源指纹随 RequestPlan 保存。"""

    instructions: tuple[ResourceFile, ...]
    skills: tuple[SkillInfo, ...]
    active: tuple[ResourceFile, ...]

    @property
    def versions(self) -> tuple[tuple[str, str], ...]:
        return tuple((item.path, item.sha256) for item in self.instructions) + tuple(
            (skill.path, skill.sha256) for skill in self.skills
        )


def read_resource(path: Path, root: Path) -> str:
    """资源只从工作区内普通 UTF-8 文件加载，不跟随符号链接。"""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"资源不能经符号链接加载：{relative}")
    if not path.is_file():
        raise ValueError(f"资源不是普通文件：{relative}")
    with path.open("rb") as source:
        data = source.read(MAX_RESOURCE_BYTES + 1)
    if len(data) > MAX_RESOURCE_BYTES:
        raise ValueError(f"资源超过单文件额度：{relative}")
    return data.decode("utf-8")


def parse_skill(text: str, path: str) -> tuple[str, str]:
    """首版只接受 frontmatter 中单行的 name/description，不假装实现完整 YAML。"""
    # TODO：完成 parse_skill，保留本文约定的输入、输出与失败边界。
    raise NotImplementedError("请完成 parse_skill")


def load_resources(root: Path, active_names: tuple[str, ...] = ()) -> ResourceBundle:
    """收集有作用范围的 AGENTS.md 和工作区技能，再按名称加载已选技能正文。"""
    # TODO：完成 load_resources，保留本文约定的输入、输出与失败边界。
    raise NotImplementedError("请完成 load_resources")


def render_resources(bundle: ResourceBundle) -> str:
    """生成一次请求的资源部分；目录规则有明确作用范围，技能引用以技能目录解析。"""
    # TODO：完成 render_resources，保留本文约定的输入、输出与失败边界。
    raise NotImplementedError("请完成 render_resources")
```

## 完整参考答案

<details>
<summary>参考答案：src/deta/resources.py（完整文件）</summary>

```python
import hashlib
import json
import os
from pathlib import Path

from deta.types import Data

MAX_RESOURCE_BYTES = 32 * 1024
MAX_TOTAL_BYTES = 128 * 1024
SKIP = {".git", ".venv", ".deta", "node_modules", "__pycache__"}


class ResourceFile(Data):
    """一份有明确作用目录的指令正文；摘要与 Context 不拥有它。"""

    path: str
    scope: str
    sha256: str
    content: str


class SkillInfo(Data):
    """技能目录项；发现阶段不把正文放进模型输入。"""

    name: str
    description: str
    path: str
    sha256: str


class ResourceBundle(Data):
    """一次 Run 或手动压缩使用的资源快照，来源指纹随 RequestPlan 保存。"""

    instructions: tuple[ResourceFile, ...]
    skills: tuple[SkillInfo, ...]
    active: tuple[ResourceFile, ...]

    @property
    def versions(self) -> tuple[tuple[str, str], ...]:
        return tuple((item.path, item.sha256) for item in self.instructions) + tuple(
            (skill.path, skill.sha256) for skill in self.skills
        )


def read_resource(path: Path, root: Path) -> str:
    """资源只从工作区内普通 UTF-8 文件加载，不跟随符号链接。"""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"资源不能经符号链接加载：{relative}")
    if not path.is_file():
        raise ValueError(f"资源不是普通文件：{relative}")
    with path.open("rb") as source:
        data = source.read(MAX_RESOURCE_BYTES + 1)
    if len(data) > MAX_RESOURCE_BYTES:
        raise ValueError(f"资源超过单文件额度：{relative}")
    return data.decode("utf-8")


def parse_skill(text: str, path: str) -> tuple[str, str]:
    """首版只接受 frontmatter 中单行的 name/description，不假装实现完整 YAML。"""
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"缺少技能 frontmatter：{path}")
    values: dict[str, str] = {}
    for line in lines[1 : lines[1:].index("---") + 1]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition(":")
        if not separator or key not in {"name", "description"} or key in values:
            raise ValueError(f"技能元数据必须使用单行 name/description：{path}")
        value = raw.strip()
        if value.startswith('"'):
            decoded = json.loads(value)
            if not isinstance(decoded, str):
                raise ValueError("技能元数据必须为文本")
            value = decoded
        elif not value or value[0] in "'|>[{&*!":
            raise ValueError(f"不支持的 frontmatter 写法：{path}")
        values[key] = value
    name, description = values.get("name", ""), values.get("description", "")
    if (
        not name.strip()
        or not description.strip()
        or len(name) > 64
        or len(description) > 1024
    ):
        raise ValueError(f"技能名称或描述无效：{path}")
    return name, description


def load_resources(root: Path, active_names: tuple[str, ...] = ()) -> ResourceBundle:
    """收集有作用范围的 AGENTS.md 和工作区技能，再按名称加载已选技能正文。"""
    root = root.resolve(strict=True)
    instructions: list[ResourceFile] = []
    total = 0
    skill_paths: list[Path] = []

    def scan_error(error: OSError) -> None:
        # 不能把权限或 I/O 失败解释成“这个目录没有项目指令”。
        raise error

    for directory, folders, files in os.walk(
        root, followlinks=False, onerror=scan_error
    ):
        folders[:] = sorted(
            name
            for name in folders
            if name not in SKIP and not (Path(directory) / name).is_symlink()
        )
        if (
            Path(directory).parent == root / ".agents" / "skills"
            and "SKILL.md" in files
        ):
            skill_paths.append(Path(directory) / "SKILL.md")
        if "AGENTS.md" not in files:
            continue
        path = Path(directory) / "AGENTS.md"
        body = read_resource(path, root)
        total += len(body.encode())
        if total > MAX_TOTAL_BYTES:
            raise ValueError("资源总量超过额度；停止继续加载")
        instructions.append(
            ResourceFile(
                path=path.relative_to(root).as_posix(),
                scope=path.parent.relative_to(root).as_posix(),
                sha256=hashlib.sha256(body.encode()).hexdigest(),
                content=body,
            )
        )
    instructions.sort(key=lambda item: (len(Path(item.path).parts), item.path))
    catalog: dict[str, SkillInfo] = {}
    bodies: dict[str, str] = {}
    # 明确一个目录约定，不混入用户主目录或外部技能库。
    for path in sorted(skill_paths):
        body = read_resource(path, root)
        total += len(body.encode())
        if total > MAX_TOTAL_BYTES:
            raise ValueError("资源总量超过额度；停止继续加载")
        relative = path.relative_to(root).as_posix()
        name, description = parse_skill(body, relative)
        if name in catalog:
            raise ValueError(f"技能重名：{name}")
        catalog[name] = SkillInfo(
            name=name,
            description=description,
            path=relative,
            sha256=hashlib.sha256(body.encode()).hexdigest(),
        )
        bodies[name] = body
    if len(active_names) != len(set(active_names)):
        raise ValueError("不能重复启用同一技能")
    active: list[ResourceFile] = []
    for name in active_names:
        if name not in catalog:
            raise ValueError(f"技能不存在：{name}")
        info = catalog[name]
        active.append(
            ResourceFile(
                path=info.path,
                scope=str(Path(info.path).parent),
                sha256=info.sha256,
                content=bodies[name],
            )
        )
    return ResourceBundle(
        instructions=tuple(instructions),
        skills=tuple(catalog.values()),
        active=tuple(active),
    )


def render_resources(bundle: ResourceBundle) -> str:
    """生成一次请求的资源部分；目录规则有明确作用范围，技能引用以技能目录解析。"""
    rules = [
        "以下是项目指令和技能。用户当前要求优先。",
        "AGENTS.md 只约束其 scope 目录及子目录，同目录链由浅到深，深层规则优先。",
        "不同子目录的规则互不覆盖；命令涉及哪些路径，就核对对应路径的规则。",
    ]
    for item in bundle.instructions:
        rules.append(f"项目指令 path={item.path} scope={item.scope}：\n{item.content}")
    if bundle.skills:
        rules.append(
            "可用技能目录；需要正文时用 read 读取 path，勿仅凭描述假定技能步骤："
        )
        rules.append(
            json.dumps(
                [item.model_dump() for item in bundle.skills], ensure_ascii=False
            )
        )
    for item in bundle.active:
        rules.append(
            f"已启用技能 path={item.path}；相对引用按 {item.scope} 解析：\n{item.content}"
        )
    return "\n\n".join(rules)
```

</details>

## 一条请求需要哪些观测证据

| 操作 | 已有/新增采集点 | 要核对的证据 |
| --- | --- | --- |
| Run | deta.run 与 Session Run | session_id、run_id、终态与原因 |
| Context 准备 | deta.context.build 与请求快照 | 资源来源、条目来源、最终指令、预算 |
| 逻辑请求与实际尝试 | deta.model.request / input / attempt | 重试次数、实际请求次数、完整响应与 usage |
| 工具处理 | 工具 Span、tool-call / raw / result | 提出、拒绝、实际开始、失败分开；原始与 Hook 后结果对应 |
| 持久化 | deta.session.commit 与耐久 events 表 | 成功提交的 Entry ID；保存失败不报告成功 |
| Compaction | deta.compaction / summary / commit | 输入范围、摘要开销、前后估算、提交 ID |
| 事件 | event artifact 中的 run_id / trace_id / span_id | 身份在事件产生边界获取，不依赖后续后台的当前 Span |

Context、摘要和提交 Span 都关闭 OTel 自动记录异常正文，只记录异常类型；正文采集关闭时，异常消息也不能成为绕过开关的通道。

Span 是调用关系，Session 是事实来源，artifact 是诊断正文。三个存储的可用性不能互相替代。请求 Hook 可以改变指令，因此资源指纹说明“本次加载过什么”，最终系统指令仍以 request artifact 为准；关闭正文采集时不能证明每一段文字都发送成功。

本页复用 Day 2 的 BatchSpanProcessor：队列 256、每批 64、退出收尾最多等待 1 秒。这个配置说明导出有界，不证明崩溃时所有 Span 都能交付；capture-status 明确记录 trace_delivery 不保证完整，结合导出警告和缺失父节点检查。Artifacts 的计数只描述正文采集，不能拿它充当 OTel 丢失 Span 的精确计数。

基础实现通过 artifact_listener 把事件保存成独立 JSON artifact，便于直接引用；没有固定名字的 events.jsonl。CLI 同时订阅记录和显示函数，Day 14 runner 只订阅记录函数；直接使用 Python API 时也需显式安装该 Listener。capture_body=False 时不保存事件正文，Session 和 Span 仍按各自规则记录。

正文仍是同步本地写入。本版补齐的是运行清单、事件来源与正文采集状态；精确 Span 丢失计数、异步正文导出、通用正文尺寸预算和标准 GenAI 字段映射均留在后续范围，与 Day 2 的承诺一致。高流量场景需要重新评估，不在 Listener 中无限缓存正文。

## 正常 API 使用示例

使用已有工作区内真实存在的技能名。下面函数只在空闲时启用一个技能，然后调用原来的任务入口；没有新建技能文件，也不会自动执行技能中的脚本。

```python
from deta.runtime import AgentSession
from deta.types import RunResult


async def run_with_skill(
    runtime: AgentSession, skill_name: str, prompt: str
) -> RunResult:
    runtime.use_skill(skill_name)
    return await runtime.prompt(prompt)
```

CLI 继续使用 Day 8 的参数，本日没有新增 --skill 标志。技能显式选择通过上述 Python API 完成。模型凭据不写入 manifest；项目正文的脱敏规则仍由调用方明确提供，当前密钥替换不能被解释为完整敏感信息识别。

## 怎样核对

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
uv run deta --help
```

完成实际源码后再运行这些命令。本文参考答案的静态检查与真实模型运行、存储恢复、回放和评测分别记录；没有相应产物时填写“未验证”。

选取工作区已存在、可从实际产物判断的项目规则，先检查 request artifact 中的路径和 scope，再看产物是否遵守。随后启用一个真实技能，对比启用前后的最终系统指令；未启用的技能只有目录项，已启用的技能正文在 Day 11 压缩后仍存在。通过请求快照核对同一 Run 的资源指纹保持一致；项目规则确实发生更新时，核对下一次 Run 才安装新版本，未发生更新的路径保留未验证。

对两个互不相关的真实任务分别组装 Runtime、SQLiteStore、工作区和 artifact 目录，再用调用方的并发入口运行。不要把两个 Session 接到 Day 8 明确禁止共享的同一 Store。核对两个 run_id 与 trace_id 的关系，取消其中一个并等待完整收尾，另一实例应能继续；同一实例仍拒绝同时直接启动两个 Run。

关闭 capture_body 后，manifest 与采集状态仍应可读，但请求正文不可用；此时记录限制。对实际观察到的导出失败检查 RunResult 和 Session 提交，诊断失败不应改写业务结果。数据库失败属于另一个生命周期边界，仍然必须使运行失败。

Day 14 接入后，在相同正文采集设置下分别检查 CLI 和评测的事件 artifact，确认两者都带事件产生位置的身份。直接 API 调用若没有订阅 artifact_listener，应注明缺少这类事件记录。

## Pi 对照与下一天

| Pi 位置或语义 | Deta 对应及差异 |
| --- | --- |
| `harness/skills.ts` 的技能发现与调用正文 | 目录先入请求，正文按需；Deta 仅实现工作区单目录、有限 frontmatter |
| 技能引用相对技能目录解析 | render_resources 显式给出技能目录，工具调用需换算工作区路径 |
| `harness/system-prompt.ts` | 在每次请求构造系统资源，不把它们当作聊天历史反复提交 |
| `harness/telemetry.ts` 与 `docs/telemetry.md` | 借鉴调用上下文传播；Deta 用 OTel 当前上下文和事件发生时的身份快照 |

固定基线的 telemetry.md 明确将自身标为设计输入，说明上下文传播已有基础，而许多运行 Span 和跨进程传播仍待实现。不能因 Pi 有 schema 就把设计文档当成已完成的遥测证明。

下一天进入 [Day 13：Badcase 定位与回放](day13.md)：从这些证据下钻到具体错误，并在资料完整时复用正式 Loop 做离线回放。
