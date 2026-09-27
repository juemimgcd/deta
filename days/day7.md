# Day 7：Loop 的完整控制语义

[总览](summary.md) · [前一天](day6.md) · [下一天](day8.md)

## 核心问题

模型调用工具、用户追加指令、结束 Hook 要求继续、请求失败触发重试，这些条件同时出现时，究竟只发一次还是多发几次请求？今天把它们放回同一个 Loop，明确每个消费与退出边界。

本页是逐步实施指南；所有代码仍是文档参考实现。Day 4 建立的文件职责不搬家，增强同名 agent.py、loop.py、hooks.py、runtime.py 和工具边界。前六天已完成的业务能力继续复用，不生成另一套核心。

## 今天新增什么

| 文件 | 本日变化 |
| --- | --- |
| `agent.py` | Steering/Follow-up、消费模式、合法 continue_ 与活动状态收尾 |
| `loop.py` | 内外两层调度、prepare_next_turn、结束优先级、批次终止聚合 |
| `hooks.py` | 六个有限 Hook、前置工具决策与增强后的 LoopBindings |
| `runtime.py` | Hook 接线、逻辑请求重试、工具变化声明与上下文副本 |
| `model.py` | 继续只做一次 SDK 请求；实际 Attempt 之前消费预算并记录编号 |
| `tools.py` | 参数之后的前置 Hook、所有普通结果的后置 Hook、raw/final 产物 |
| `types.py`、`builtin_tools/__init__.py` | RunBudget、重试配置与 terminate 提示 |

本日仍没有 SQLite Session、Context 压缩算法或跨进程输入队列。prepare_next_turn 为后续准备工作提供接入点，不表示压缩已经实现。

## 分三个步骤实施

本章允许跨多个工作日完成。先按“公共数据与单次模型边界”的补丁对齐类型和调用签名，再按下表填写同一套文件。完整答案是三个步骤都完成后的累计状态，不要求一次重写所有函数。

| 步骤 | 当前要做什么 | 可运行的检查点 |
| --- | --- | --- |
| 7A：队列与续轮 | InputQueue、start/continue_、内外循环及结束优先级；可选 Hooks 保持为空 | 默认 one 模式下追加一条 Steering 和一条 Follow-up，观察它们只在对应边界入历史；再核对 all 与助手末尾 continue_ |
| 7B：重试与预算 | 接通 RunBudget，先令 max_retries=0 跑通请求与工具计数，再加入有限退避 | 原来的 read/bash 链仍可运行，额度不足有明确终态；真实重试证据不足时保留未验证 |
| 7C：可选 Hooks | 按下表逐个引入一个控制需求，处理返回值、异常与副本 | 每次只配置一个 Hook，记录它改变了哪一步；最后再核对组合时的优先级 |

为保证 7A 可运行，先使用本章直接提供的 RunBudget 和参考答案中的请求/工具接线，配置 max_retries=0、Hooks()；此时练习集中在队列和 Loop。7B 再展开请求边界的计数与重试实现，7C 再逐个配置自定义 Hook。不要把尚未核对的 7B/7C 标为通过，也不保留多套 Loop。进入 Day 8 前完成本章全部核心语义。

### 先说明用途，再启用 Hook

下面是调用方有对应需求时的使用场景，默认运行不必启用全部 Hook。每个场景都复用正式 Agent 和真实任务，不要求新增测试或模拟响应。

| Hook | 一个具体用途 | 实施时要看清的边界 |
| --- | --- | --- |
| prepare_request | 本次任务只允许读取，调用方把工具表限定为 read | 发给模型的声明和实际执行表必须一致 |
| prepare_next_turn | 上一轮检查完成后，调用方补充一条已确认的新任务约束 | 只在后续轮次运行，不能重复提交输入 |
| transform_context | 从本次输入中移除调用方已确认无关的一段历史 | 原始历史保留，工具调用与结果不能拆开；Day 9 再记录来源变化 |
| finish_turn | 调用方已确认任务阶段结束，显式返回 end | 先完成本轮结果提交，再结束；未消费队列保留 |
| before_tool | 拒绝修改调用方指定的受保护文件 | 参数校验后、handler 执行前做决定；这里不提供 bash 沙箱 |
| after_tool | 为普通工具结果补充面向模型的解释 | 保留调用身份；raw/final 分开，异常不能伪装成工具未执行 |

deep copy、结果校验和来源重映射服务于这些允许改写的边界。没有自定义需求时沿用默认路径；未来扩展也先找到具体调用方，再增加新 Hook。

## 先把优先级写清楚

```text
请求、工具执行与消息提交
  ├─ 取消 / 内部错误 / 硬额度不足：退出并收尾
  └─ 本轮完整响应和工具结果提交成功
       → finish_turn → turn_end
       → decision=end：立即结束，尚未消费的队列保留
       → 工具批次允许自然续轮，或有 Steering：选择一个下一轮
       → 否则检查 Follow-up：有则选择一个下一轮
       → 否则 decision=continue：只补一个上下文请求
       → 否则结束
```

这里的错误指向外抛出的异常；可回传的普通工具失败仍作为 ToolResult 进入正常决策。finish_turn 自身异常或期间收到取消也直接退出。显式 continue 与自然续轮合并；若 Hook 每轮都返回 continue，每轮的新决定仍受预算限制。

## 先认识本日的类与属性

| 对象 | 属性与职责 |
| --- | --- |
| `InputQueue` | items 保存尚未提交的 UserMessage；mode 为 one 或 all；take 按模式消费，drain_all 用于显式助手末尾继续 |
| `Agent.steering / followups` | 两个独立队列；前者影响后续轮次，后者在自然结束前消费 |
| `RunBudget` | options 是配置；request_attempts 计实际 SDK 尝试；tool_calls 计已预占的调度次数 |
| `RunOptions` | max_requests 从本日明确包含重试；max_retries 是单个逻辑请求额外尝试上限；retry_delay_seconds 是退避基数 |
| `BeforeToolDecision` | allow 决定是否进入 handler；reason 用于拒绝结果；terminate 附带批次终止提示 |
| `Hooks` | 保存准备请求、准备下一轮、转换上下文、轮次结束、工具前置与工具后置六个可选回调 |
| `ToolResult.terminate / ToolOutput.terminate` | 一个结果的停止提示；只有非空整批全部为 True，才停止工具导致的自然续轮 |
| `AgentSession._last_tools` | 最近一次成功响应使用的 schema，用于记录新增、移除与参数更新 |

## 先认识本日的函数

| 函数 | 调用与返回关系 |
| --- | --- |
| `Agent.start` | 接受新 prompt，追加新的用户消息并开始 Run |
| `Agent.continue_` | 检查已有历史，复用末尾用户/工具结果；助手末尾必须取到排队输入 |
| `steer / follow_up` | 只入队，不中断当前工具，不立即追加历史 |
| `run_loop` | 从队列边界选择下一轮；对工具结果做 all(terminate) 聚合 |
| `_prepare_next_turn` | 有上一轮报告时才调用，可准备资源并返回要先提交的用户消息 |
| `_prepare_request / _transform_context` | 每次都执行准备，再在内部消息层转换；Loop 最后检查有效配对 |
| `_request` | 建立一个逻辑请求 Span，决定有限重试；每次复用同一计划并调用 stream_once |
| `RunBudget.take_request` | 输入准备成功之后、SDK 调用之前计数，返回真实尝试序号 |
| `RunBudget.reserve_tools` | 本批任何工具调度开始前检查并预占整批额度 |
| `retryable` | 只分类连接/超时、408、429、5xx，鉴权和协议错误不自动重试 |
| `execute_tool` | 名称/参数 → before_tool → 实际执行 → raw → after_tool → final |
| `copy_report` | 给 Hook 复制嵌套消息字段，防止观察/决策时直接改坏历史对象 |

准备与提交仍由运行时绑定，Loop 不导入 SQLite、Session 或 Compaction。工具前后 Hook 能改变执行决定和结果；普通 Listener 的返回值不控制运行。

## 公共数据与单次模型边界的接入补丁

max_requests 现在限制所有实际 SDK 尝试，包括重试；工具额度计“进入调度”，参数被拒绝也占用，以限制反复错误调用。Trace 的 execution_started 另行统计实际进入 handler 的次数，不能混为一谈。

Day 2 的 stream_once 仍然只做一次请求。它原来的外层 Span 改名为 model.input，用来关联该次尝试的实际输入；真正的逻辑请求 Span 放到 runtime._request。新增 before_attempt 回调在输入转换和快照准备之后调用，额度不足时不会生成一个虚假的 attempt Span。

RunLimitError 从 loop.py 移到 types.py，与本日新增的 RunBudget 放在一起，供 Loop 与请求边界共同使用。工具调度器从本日开始使用 Hooks，因此 hooks.py 对 ToolSpec 改为只在类型检查时导入，避免循环导入。

下面是对前一天累计实现的完整接入补丁。`-` 行移除，`+` 行加入，其余行是定位上下文；不用把 diff 标记复制进 Python。补丁中的新类、属性与函数也带中文注释。先按补丁更新依赖，再填写本日骨架；文件其余内容继续保留。

<details>
<summary>接入补丁：src/deta/types.py（相对 Day 6 完成状态）</summary>

```diff
--- a/src/deta/types.py
+++ b/src/deta/types.py
@@ -1,3 +1,4 @@
+from dataclasses import dataclass
 from typing import Literal
 
 from pydantic import BaseModel, ConfigDict, Field, JsonValue
@@ -87,6 +88,8 @@
     error_code: str | None = None
     # 工具的结构化诊断信息，不直接转换成提供方的消息字段。
     details: dict[str, JsonValue] = Field(default_factory=dict)
+    # 工具或 Hook 的批次停止提示；只有本批全部结果都为 True 才停止自然工具续轮。
+    terminate: bool = False
 
     @property
     def is_error(self) -> bool:
@@ -105,12 +108,16 @@
     本类只定义限制数据，实际计数、超时和停止处理由运行控制代码完成。
     """
 
-    # 一次 Run 的模型请求额度，后续由 Loop 计数和执行限制。
+    # 一次 Run 的实际 SDK 尝试总额度，包括重试；在调用 SDK 前扣减。
     max_requests: int = Field(default=10, ge=1)
     # 一次 Run 允许的工具调用总数；设为零表示不给工具执行额度。
     max_tool_calls: int = Field(default=20, ge=0)
     # 整次 Run 的总时间额度，单位为秒，与单次模型请求超时分别管理。
     timeout_seconds: float = Field(default=120, gt=0)
+    # 单个逻辑请求最多额外重试几次，同时受 max_requests 总尝试额度约束。
+    max_retries: int = Field(default=2, ge=0, le=5)
+    # 指数退避的初始等待秒数，等待也计入总运行时限。
+    retry_delay_seconds: float = Field(default=0.25, ge=0)
 
 
 class RunResult(Data):
@@ -128,3 +135,32 @@
     answer: str = ""
     # 随运行结果返回的消息元组，供调用方查看本次产生或使用的对话内容。
     messages: tuple[AgentMessage, ...] = ()
+
+
+class RunLimitError(Exception):
+    """表示 Run 额度耗尽，保留停止原因交给 Agent 构造 limited 结果。"""
+
+
+@dataclass
+class RunBudget:
+    """保存当前 Run 的可变计数，由 Loop 创建并交给请求与工具边界共同使用。"""
+
+    # 当前 Run 的不可变额度配置。
+    options: RunOptions
+    # 已进入 SDK 调用边界的尝试次数，重试也计数。
+    request_attempts: int = 0
+    # 已放行进入调度的工具调用数量，参数被拒绝也占用调度额度。
+    tool_calls: int = 0
+
+    def take_request(self) -> int:
+        """在真正开始 SDK 尝试前检查额度并计数，返回该 Run 内的尝试编号。"""
+        if self.request_attempts >= self.options.max_requests:
+            raise RunLimitError("实际模型请求尝试额度耗尽")
+        self.request_attempts += 1
+        return self.request_attempts
+
+    def reserve_tools(self, count: int) -> None:
+        """在开始本批工具调度前一次性预占全部额度，避免执行半批后才发现不够。"""
+        if self.tool_calls + count > self.options.max_tool_calls:
+            raise RunLimitError("工具调度额度耗尽")
+        self.tool_calls += count
```

</details>

<details>
<summary>接入补丁：src/deta/builtin_tools/__init__.py（相对 Day 6 完成状态）</summary>

```diff
--- a/src/deta/builtin_tools/__init__.py
+++ b/src/deta/builtin_tools/__init__.py
@@ -18,6 +18,8 @@
     error_code: str | None = None
     # 用于诊断的 JSON 数据，例如内容摘要、文件字节数或输出文件引用。
     details: dict[str, JsonValue] = field(default_factory=dict)
+    # 本工具请求停止自然续轮的提示；批次聚合规则由 Loop 执行。
+    terminate: bool = False
 
 
 @dataclass(frozen=True)
```

</details>

<details>
<summary>接入补丁：src/deta/model.py（相对 Day 6 完成状态）</summary>

```diff
--- a/src/deta/model.py
+++ b/src/deta/model.py
@@ -1,5 +1,5 @@
 import asyncio
-from collections.abc import Sequence
+from collections.abc import Callable, Sequence
 from dataclasses import dataclass
 from typing import Literal, cast
 
@@ -114,6 +114,7 @@
     tracer: Tracer,
     artifacts: Artifacts,
     listeners: Sequence[Listener] = (),
+    before_attempt: Callable[[], int] | None = None,
 ) -> AssistantMessage:
     """接收 SDK 客户端、请求配置、指令、历史消息、工具声明及观测依赖，完成一次流式请求。
     函数向监听器发送增量，将完整 AssistantMessage 返回给 CLI 或后续 Loop；错误与取消向外传播。
@@ -130,15 +131,15 @@
             "stream": True,
             "stream_options": {"include_usage": True},
             "max_completion_tokens": config.max_completion_tokens,
-            "config_version": "day2-v1",
+            "config_version": "day7-v1",
             "sdk_retries": 0,
         }
     )
     with tracer.start_as_current_span(
-        "deta.model.request", record_exception=False, set_status_on_exception=False
+        "deta.model.input", record_exception=False, set_status_on_exception=False
     ) as request:
         request.set_attribute("deta.model", config.model)
-        request.set_attribute("deta.config_version", "day2-v1")
+        request.set_attribute("deta.config_version", "day7-v1")
         ref = artifacts.save("request", snapshot)
         request.set_attribute(
             "deta.request_body", "captured_redacted" if ref else "unavailable"
@@ -147,12 +148,14 @@
             request.set_attribute("deta.request_artifact", ref)
         try:
             async with asyncio.timeout(config.timeout_seconds):
+                # 输入准备完成后才申请实际尝试额度；额度失败不会创建 attempt Span。
+                attempt_number = before_attempt() if before_attempt is not None else 1
                 with tracer.start_as_current_span(
                     "deta.model.attempt",
                     record_exception=False,
                     set_status_on_exception=False,
                 ) as attempt:
-                    attempt.set_attribute("deta.attempt", 1)
+                    attempt.set_attribute("deta.attempt", attempt_number)
                     try:
                         stream = await client.chat.completions.create(
                             model=config.model,
```

</details>

<details>
<summary>接入补丁：src/deta/hooks.py（相对 Day 6 完成状态）</summary>

```diff
--- a/src/deta/hooks.py
+++ b/src/deta/hooks.py
@@ -1,10 +1,23 @@
+from __future__ import annotations
+
 from collections.abc import Awaitable, Callable, Mapping
 from dataclasses import dataclass
-from typing import Any, Literal
+from typing import TYPE_CHECKING, Any, Literal
+
+from pydantic import BaseModel
 
 from deta.events import Listener
-from deta.tools import ToolSpec
-from deta.types import AgentMessage, AssistantMessage, ToolCall, ToolResult
+from deta.types import (
+    AgentMessage,
+    AssistantMessage,
+    RunBudget,
+    ToolCall,
+    ToolResult,
+    UserMessage,
+)
+
+if TYPE_CHECKING:
+    from deta.tools import ToolSpec
 
 
 @dataclass(frozen=True)
@@ -44,6 +57,42 @@
 
 
 @dataclass(frozen=True)
+class BeforeToolDecision:
+    """表示工具前置 Hook 的执行决策，不通过普通事件监听器隐式改变行为。"""
+
+    # 是否允许进入工具 handler。
+    allow: bool = True
+    # 拒绝时写入工具结果的原因。
+    reason: str = ""
+    # 拒绝时附带的批次停止提示，仍需所有结果都要求终止才生效。
+    terminate: bool = False
+
+
+@dataclass(frozen=True)
+class Hooks:
+    """保存六个可选控制回调；未配置的位置使用 AgentSession 的默认行为。"""
+
+    # 首次与后续每次请求都调用，可以替换本次请求计划。
+    prepare_request: Callable[[RequestPlan], Awaitable[RequestPlan]] | None = None
+    # 仅已完成一轮后调用，允许准备上下文并返回要先提交的用户消息。
+    prepare_next_turn: (
+        Callable[[TurnReport], Awaitable[tuple[UserMessage, ...]]] | None
+    ) = None
+    # 在请求计划确定后调整消息副本，结果只用于本次请求。
+    transform_context: (
+        Callable[[tuple[AgentMessage, ...]], Awaitable[tuple[AgentMessage, ...]]] | None
+    ) = None
+    # 根据完整轮次报告显式继续或结束；异常会使 Run 失败。
+    finish_turn: Callable[[TurnReport], Awaitable[TurnDecision]] | None = None
+    # 已解析且有效的参数才到这里；回调读取参数副本，不能偷偷修改执行参数。
+    before_tool: (
+        Callable[[ToolCall, BaseModel], Awaitable[BeforeToolDecision]] | None
+    ) = None
+    # 每个普通结果都经过此处，包括拒绝和参数失败；允许修改正文与结果状态。
+    after_tool: Callable[[ToolCall, ToolResult], Awaitable[ToolResult]] | None = None
+
+
+@dataclass(frozen=True)
 class LoopBindings:
     """把运行时提供的有限操作显式交给 Loop，避免 Loop 依赖存储和配置加载。
 
@@ -54,8 +103,8 @@
     prepare_request: Callable[[tuple[AgentMessage, ...]], Awaitable[RequestPlan]]
     # 在准备后转换请求视图，例如插入本次需要的上下文资料。
     transform_context: Callable[[RequestPlan], Awaitable[RequestPlan]]
-    # 发起一次模型请求；Listener 接收流式通知，返回值是完整响应。
-    request: Callable[[RequestPlan, Listener], Awaitable[AssistantMessage]]
+    # 发起逻辑请求并管理重试；预算在实际 SDK 尝试边界消费。
+    request: Callable[[RequestPlan, Listener, RunBudget], Awaitable[AssistantMessage]]
     # 执行一个完整调用；两个回调分别通知实际开始与输出增量。
     execute_tool: Callable[
         [ToolCall, RequestPlan, Callable[[], None], Callable[[str], None]],
@@ -65,3 +114,5 @@
     commit: Callable[[AgentMessage], Awaitable[None]]
     # 完整轮次结束后返回决策；它是控制 Hook，不是普通事件监听器。
     finish_turn: Callable[[TurnReport], Awaitable[TurnDecision]]
+    # 后续轮次开始前的准备，首次请求不调用。
+    prepare_next_turn: Callable[[TurnReport], Awaitable[tuple[UserMessage, ...]]]
```

</details>

## 本日练习骨架

按 InputQueue 和 continue_ → Loop 调度 → runtime 接线与重试 → 工具前后 Hook 的顺序填写。本节只列今天需要手写的定义，省略已学过的导入和实现；片段不能当作完整文件覆盖。每节先说明配套修改，后面的完整参考答案包含全部导入、属性与接线，可逐项核对。

### src/deta/agent.py

新增 InputQueue，并给 Agent.__init__ 增加 steering、followups 两个队列。将 start 原有的运行检查与任务安排分别提取到 _ensure_idle、_schedule，供 start 和 continue_ 共用；_drive 改为接收初始消息元组，并把队列消费与取消检查函数交给 Loop。running、subscribe、publish、abort、wait 沿用前面的职责。

新增类的完整骨架；补充 `from collections import deque` 与 `from typing import Literal`：

```python
class InputQueue:
    """保存尚未提交历史的用户输入，按单条或全部模式在明确边界取出。"""

    def __init__(self, mode: Literal["one", "all"] = "one") -> None:
        """初始化消费模式与独立队列；入队不会立刻影响正在发送的请求。"""
        if mode not in {"one", "all"}:
            raise ValueError("队列模式必须是 one 或 all")
        # 每个正常轮询消费一条还是全部待处理消息。
        self.mode = mode
        # 队列拥有的用户消息，直到 take/drain_all 才从队列移出。
        self.items: deque[UserMessage] = deque()

    def push(self, text: str) -> None:
        """校验非空输入并排队，留给下一次合适的调度边界消费。"""
        if not text.strip():
            raise ValueError("排队输入不能为空")
        self.items.append(UserMessage(content=text))

    def take(self) -> tuple[UserMessage, ...]:
        """按当前模式取出一批消息；队列为空时返回空元组。

        TODO：按 one/all 模式只取本次应消费的批次，空队列返回空元组。
        """
        raise NotImplementedError("请完成 take")

    def drain_all(self) -> tuple[UserMessage, ...]:
        """一次性取出全部消息，用于显式 continue 的助手末尾分支或 all 模式。

        TODO：复制当前队列为元组并清空，返回原先全部待处理输入。
        """
        raise NotImplementedError("请完成 drain_all")
```

在已有 Agent 类中增加这个方法（放回类内时缩进四个空格）：

```python
def continue_(self, *, run_id: str | None = None) -> None:
    """从已有合法历史继续；助手末尾必须先取排队输入，空历史直接拒绝。

    TODO：先拒绝活动运行、空历史与 pending；助手末尾优先排空 Steering，其次 Follow-up；其他合法末尾复用原输入。
    """
    raise NotImplementedError("请完成 continue_")
```

### src/deta/loop.py

保留 Day 4 的 pending_calls、failed_result，只改同一个 run_loop。它从 types.py 导入 RunBudget、RunLimitError；新增参数接收 Agent 的队列消费和取消检查函数。

用下面的定义替换同名函数：

```python
async def run_loop(
    messages: list[AgentMessage],
    bindings: LoopBindings,
    options: RunOptions,
    *,
    run_id: str,
    publish: Callable[[AgentEvent], None],
    tracer: Tracer,
    take_steering: Callable[[], tuple[UserMessage, ...]],
    take_followups: Callable[[], tuple[UserMessage, ...]],
    check_cancel: Callable[[], None],
    skip_initial_steering: bool = False,
) -> tuple[str, str]:
    """以同一套内外循环处理工具续轮、Steering、Follow-up 和显式继续。

    内层处理工具与 Steering，外层只在自然停止时取 Follow-up 或履行一次显式继续。
    所有消息提交仍走 bindings.commit，错误与取消不会被 finish_turn 的返回值覆盖。

    TODO：后续轮次先 prepare_next_turn；只在此前没选到消息时补取 Steering；完整提交后聚合结果；end 先于队列；自然请求与 continue 合并；硬错误和取消直接传播。
    """
    raise NotImplementedError("请完成 run_loop")
```

### src/deta/runtime.py

AgentSession 新增 hooks 与 _last_tools；绑定 prepare_next_turn，提供 continue_ 入口，并把本轮 Hooks 传给工具执行器。copy_report、retryable 是直接提供的辅助函数；_commit 仍只负责追加消息。下面只练习请求准备、转换、重试和结束决策。

以下方法分别替换 AgentSession 内的同名方法，并恢复类内缩进：

```python
async def _prepare_request(self, messages: tuple[AgentMessage, ...]) -> RequestPlan:
    """每次请求前准备视图，应用准备 Hook 后冻结实际工具表。

    TODO：复制消息，应用请求准备 Hook，检查工具键与名称一致，冻结本轮工具表。
    """
    raise NotImplementedError("请完成 _prepare_request")

async def _transform_context(self, plan: RequestPlan) -> RequestPlan:
    """在准备之后转换消息副本；完整协议配对由 Loop 在请求边界检查。

    TODO：把消息副本交给转换 Hook，检查返回内部消息元组并放回计划。
    """
    raise NotImplementedError("请完成 _transform_context")

async def _prepare_next_turn(self, report: TurnReport) -> tuple[UserMessage, ...]:
    """后续轮次才调用准备 Hook，返回先于本批队列消息提交的用户输入。

    TODO：首次不由 Loop 调用；后续使用报告副本，返回经过形状检查的准备消息。
    """
    raise NotImplementedError("请完成 _prepare_next_turn")

async def _request(
    self,
    plan: RequestPlan,
    listener: Listener,
    budget: RunBudget,
) -> AssistantMessage:
    """为一个逻辑请求管理有限重试，所有尝试复用相同输入且由 SDK 边界计数。

    TODO：记录实际工具变化，在一个逻辑请求 Span 内重试；已发布增量后不自动重试；每次 SDK 尝试经预算回调计数。
    """
    raise NotImplementedError("请完成 _request")

async def _finish_turn(self, report: TurnReport) -> TurnDecision:
    """把完整报告副本交给结束 Hook，未配置时返回 auto 自然决策。

    TODO：默认 auto；有 Hook 时传入报告副本并把决策交回 Loop。
    """
    raise NotImplementedError("请完成 _finish_turn")
```

### src/deta/tools.py

保留现有 ToolSpec、工具表、schema、参数解析和 invoke_handler。仅给 execute_tool 增加 hooks 参数，围绕既有执行步骤接入 before_tool / after_tool；四个内置工具的实现均不改。

用下面的定义替换同名函数：

```python
async def execute_tool(
    call: ToolCall,
    workspace: Path,
    *,
    tracer: Tracer,
    artifacts: Artifacts,
    registry: Mapping[str, ToolSpec[Any]] | None = None,
    on_start: Callable[[], None] | None = None,
    context: ToolContext | None = None,
    hooks: Hooks | None = None,
) -> ToolResult:
    """统一校验、执行决策、工具执行与结果后处理，保存原始和最终结果的对应关系。

    业务错误返回配对结果；Hook、产物之外的内部错误和取消向外传播。

    TODO：校验通过才调用 before_tool；拒绝不进入 handler；所有普通结果先保存 raw 再过 after_tool；固定调用身份，保存 final；Hook/内部错误和取消向外传播。
    """
    raise NotImplementedError("请完成 execute_tool")
```

## 完整参考答案

以下是 Day 7 累计到当前的完整文件。核对本日改动时展开对应文件即可；运行时继续使用同一个 Agent 和 Loop。

<details>
<summary>参考答案：src/deta/agent.py（完整文件）</summary>

```python
import asyncio
from collections import deque
from collections.abc import Sequence
from typing import Literal
from uuid import uuid4

from opentelemetry.trace import Status, StatusCode, Tracer

from deta.events import AgentEvent, Listener, ModelEvent, emit
from deta.hooks import LoopBindings
from deta.loop import pending_calls, run_loop
from deta.types import (
    AgentMessage,
    AssistantMessage,
    RunLimitError,
    RunOptions,
    RunResult,
    UserMessage,
)


class InputQueue:
    """保存尚未提交历史的用户输入，按单条或全部模式在明确边界取出。"""

    def __init__(self, mode: Literal["one", "all"] = "one") -> None:
        """初始化消费模式与独立队列；入队不会立刻影响正在发送的请求。"""
        if mode not in {"one", "all"}:
            raise ValueError("队列模式必须是 one 或 all")
        # 每个正常轮询消费一条还是全部待处理消息。
        self.mode = mode
        # 队列拥有的用户消息，直到 take/drain_all 才从队列移出。
        self.items: deque[UserMessage] = deque()

    def push(self, text: str) -> None:
        """校验非空输入并排队，留给下一次合适的调度边界消费。"""
        if not text.strip():
            raise ValueError("排队输入不能为空")
        self.items.append(UserMessage(content=text))

    def take(self) -> tuple[UserMessage, ...]:
        """按当前模式取出一批消息；队列为空时返回空元组。"""
        if not self.items:
            return ()
        if self.mode == "all":
            return self.drain_all()
        return (self.items.popleft(),)

    def drain_all(self) -> tuple[UserMessage, ...]:
        """一次性取出全部消息，用于显式 continue 的助手末尾分支或 all 模式。"""
        messages = tuple(self.items)
        self.items.clear()
        return messages


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
        initial: tuple[UserMessage, ...],
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
        self._schedule((UserMessage(content=prompt),), run_id)

    def continue_(self, *, run_id: str | None = None) -> None:
        """从已有合法历史继续；助手末尾必须先取排队输入，空历史直接拒绝。"""
        self._ensure_idle()
        if not self.messages:
            raise ValueError("空历史不能继续；系统指令本身也不构成任务输入")
        if isinstance(self.messages[-1], AssistantMessage):
            selected = self.steering.drain_all()
            if selected:
                self._schedule(selected, run_id, skip_initial_steering=True)
                return
            selected = self.followups.drain_all()
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

    async def _drive(
        self,
        initial: tuple[UserMessage, ...],
        run_id: str,
        skip_initial_steering: bool,
    ) -> RunResult:
        """包住同一个 Loop，提交初始输入并在总时限与清理完成后产生 RunResult。"""
        self._entered = True
        result = RunResult(run_id=run_id, status="failed")
        with self.tracer.start_as_current_span(
            "deta.run", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("deta.run_id", run_id)
            self.publish(AgentEvent(kind="run_start", run_id=run_id))
            try:
                self._check_cancel()
                async with asyncio.timeout(self.options.timeout_seconds):
                    for message in initial:
                        await self.bindings.commit(message)
                    answer, reason = await run_loop(
                        self.messages,
                        self.bindings,
                        self.options,
                        run_id=run_id,
                        publish=self.publish,
                        tracer=self.tracer,
                        take_steering=self.steering.take,
                        take_followups=self.followups.take,
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
                span.set_attribute("deta.outcome", result.status)
                if result.status != "completed":
                    span.set_status(Status(StatusCode.ERROR, result.reason))
                self.publish(
                    AgentEvent(kind="run_end", run_id=run_id, status=result.status)
                )
        return result.model_copy(update={"messages": tuple(self.messages)})
```

</details>

<details>
<summary>参考答案：src/deta/loop.py（完整文件）</summary>

```python
import asyncio
from collections.abc import Callable, Sequence

from opentelemetry.trace import Status, StatusCode, Tracer

from deta.events import AgentEvent, Event, ModelDone
from deta.hooks import LoopBindings, TurnReport
from deta.types import (
    AgentMessage,
    AssistantMessage,
    RunBudget,
    RunLimitError,
    RunOptions,
    ToolCall,
    ToolResult,
    UserMessage,
)


def pending_calls(messages: Sequence[AgentMessage]) -> dict[str, str]:
    """检查历史顺序与工具配对，返回尚欠结果的调用 ID 到工具名的映射。

    Agent 在接收新输入时使用返回值；Loop 在请求前要求映射为空。
    错配结果、重复调用编号或在未配对时插入普通消息会直接失败。
    """
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        if isinstance(message, ToolResult):
            if pending.get(message.tool_call_id) != message.name:
                raise ValueError("历史中的工具结果无法配对")
            del pending[message.tool_call_id]
            continue
        if pending:
            raise ValueError("工具结果尚未配齐就插入了普通消息")
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                if not call.id.strip() or not call.name.strip() or call.id in seen:
                    raise ValueError("历史中存在空白或重复的工具调用编号")
                seen.add(call.id)
                pending[call.id] = call.name
    return pending


def failed_result(call: ToolCall, code: str, reason: str) -> ToolResult:
    """为不能执行的调用构造失败消息，把原调用 ID 和名称完整交回 Loop。

    用于截断响应或整批额度不足等分支，此函数不会执行工具。
    """
    return ToolResult(
        tool_call_id=call.id, name=call.name, content=reason, error_code=code
    )


async def run_loop(
    messages: list[AgentMessage],
    bindings: LoopBindings,
    options: RunOptions,
    *,
    run_id: str,
    publish: Callable[[AgentEvent], None],
    tracer: Tracer,
    take_steering: Callable[[], tuple[UserMessage, ...]],
    take_followups: Callable[[], tuple[UserMessage, ...]],
    check_cancel: Callable[[], None],
    skip_initial_steering: bool = False,
) -> tuple[str, str]:
    """以同一套内外循环处理工具续轮、Steering、Follow-up 和显式继续。

    内层处理工具与 Steering，外层只在自然停止时取 Follow-up 或履行一次显式继续。
    所有消息提交仍走 bindings.commit，错误与取消不会被 finish_turn 的返回值覆盖。
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
            prepared: tuple[UserMessage, ...] = ()
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
                    plan = await bindings.prepare_request(tuple(messages))
                    plan = await bindings.transform_context(plan)
                    check_cancel()
                    if pending_calls(plan.messages):
                        raise ValueError("本次请求仍缺少工具结果")

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

                    response = await bindings.request(plan, on_model, budget)
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
                    calls = response.tool_calls
                    results: list[ToolResult] = []
                    blocked = ""
                    if calls and budget.request_attempts >= options.max_requests:
                        blocked = "没有剩余请求额度消费工具结果"
                    if calls and response.stop_reason == "tool_calls" and not blocked:
                        try:
                            budget.reserve_tools(len(calls))
                        except RunLimitError as exc:
                            blocked = str(exc)
                    for call in calls:
                        check_cancel()
                        if blocked:
                            result = failed_result(call, "budget_exhausted", blocked)
                        elif response.stop_reason != "tool_calls":
                            result = failed_result(
                                call,
                                "incomplete_response",
                                "助手响应被截断或过滤，未执行本次调用，请重新提供完整参数。",
                            )
                        else:

                            def on_start(call_id: str = call.id) -> None:
                                """在参数与前置 Hook 放行之后通知真正的 handler 开始。"""
                                publish(
                                    AgentEvent(
                                        kind="tool_start",
                                        run_id=run_id,
                                        turn=turn,
                                        tool_call_id=call_id,
                                    )
                                )

                            def on_output(text: str, call_id: str = call.id) -> None:
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

                            result = await bindings.execute_tool(
                                call, plan, on_start, on_output
                            )
                        await bindings.commit(result)
                        results.append(result)
                        publish(
                            AgentEvent(
                                kind="tool_end",
                                run_id=run_id,
                                turn=turn,
                                tool_call_id=call.id,
                                status=result.error_code or "success",
                            )
                        )
                    check_cancel()
                    if blocked:
                        raise RunLimitError(blocked)
                    if (
                        response.stop_reason in {"length", "content_filter"}
                        and not calls
                    ):
                        raise RunLimitError(f"模型未完整回答：{response.stop_reason}")
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
                return response.content, "hook_end"
            explicit = decision == "continue"
            has_tools = bool(results) and not all(
                result.terminate for result in results
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
        return response.content, "tool_terminated" if results else "answer"
```

</details>

<details>
<summary>参考答案：src/deta/runtime.py（完整文件）</summary>

```python
import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from opentelemetry.trace import Status, StatusCode, Tracer

from deta.agent import Agent
from deta.builtin_tools import ToolContext
from deta.events import Event, Listener, TextDelta, ToolCallDelta
from deta.hooks import Hooks, LoopBindings, RequestPlan, TurnDecision, TurnReport
from deta.model import ModelConfig, stream_once
from deta.observability.artifacts import Artifacts
from deta.tools import TOOLS, execute_tool, tool_schemas
from deta.types import (
    AgentMessage,
    AssistantMessage,
    RunBudget,
    RunLimitError,
    RunOptions,
    RunResult,
    ToolCall,
    ToolResult,
    UserMessage,
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


class AgentSession:
    """绑定模型、工具、Hooks 和消息提交，让评测与普通调用以后复用同一个 Agent。"""

    def __init__(
        self,
        client: AsyncOpenAI,
        config: ModelConfig,
        workspace: Path,
        tracer: Tracer,
        artifacts: Artifacts,
        *,
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
        # 唯一的活动运行、消息历史与输入队列所有者。
        self.agent = Agent(
            LoopBindings(
                self._prepare_request,
                self._transform_context,
                self._request,
                self._execute_tool,
                self._commit,
                self._finish_turn,
                self._prepare_next_turn,
            ),
            options or RunOptions(),
            tracer,
            listeners,
        )

    async def prompt(self, text: str, *, run_id: str | None = None) -> RunResult:
        """接受新的用户输入并等待完整 Run 结果。"""
        self.agent.start(text, run_id=run_id)
        try:
            return await self.agent.wait()
        except asyncio.CancelledError:
            # 公共任务入口被取消时先结束 Agent，保持客户端等依赖直到清理完成。
            self.agent.abort()
            await self.agent.wait()
            raise

    async def continue_(self, *, run_id: str | None = None) -> RunResult:
        """继续合法历史或助手末尾的排队输入，不把结果未知的工具自动重放。"""
        self.agent.continue_(run_id=run_id)
        try:
            return await self.agent.wait()
        except asyncio.CancelledError:
            # 公共任务入口被取消时先结束 Agent，保持客户端等依赖直到清理完成。
            self.agent.abort()
            await self.agent.wait()
            raise

    async def _prepare_request(self, messages: tuple[AgentMessage, ...]) -> RequestPlan:
        """每次请求前准备视图，应用准备 Hook 后冻结实际工具表。"""
        plan = RequestPlan(
            self.instructions,
            tuple(item.model_copy(deep=True) for item in messages),
            MappingProxyType(dict(self.tools)),
        )
        if self.hooks.prepare_request is not None:
            plan = await self.hooks.prepare_request(plan)
        if not isinstance(plan, RequestPlan):
            raise TypeError("prepare_request 必须返回 RequestPlan")
        if any(name != spec.name for name, spec in plan.tools.items()):
            raise ValueError("工具表键与 ToolSpec.name 不一致")
        return replace(plan, tools=MappingProxyType(dict(plan.tools)))

    async def _transform_context(self, plan: RequestPlan) -> RequestPlan:
        """在准备之后转换消息副本；完整协议配对由 Loop 在请求边界检查。"""
        if self.hooks.transform_context is None:
            return plan
        messages = await self.hooks.transform_context(
            tuple(item.model_copy(deep=True) for item in plan.messages)
        )
        if not isinstance(messages, tuple) or any(
            not isinstance(item, (UserMessage, AssistantMessage, ToolResult))
            for item in messages
        ):
            raise TypeError("transform_context 必须返回内部消息元组")
        return replace(plan, messages=messages)

    async def _prepare_next_turn(self, report: TurnReport) -> tuple[UserMessage, ...]:
        """后续轮次才调用准备 Hook，返回先于本批队列消息提交的用户输入。"""
        if self.hooks.prepare_next_turn is None:
            return ()
        messages = await self.hooks.prepare_next_turn(copy_report(report))
        if not isinstance(messages, tuple) or any(
            not isinstance(item, UserMessage) for item in messages
        ):
            raise TypeError("prepare_next_turn 必须返回 UserMessage 元组")
        return messages

    async def _request(
        self,
        plan: RequestPlan,
        listener: Listener,
        budget: RunBudget,
    ) -> AssistantMessage:
        """为一个逻辑请求管理有限重试，所有尝试复用相同输入且由 SDK 边界计数。"""
        schemas = tool_schemas(plan.tools)
        current = {
            name: json.dumps(schema, sort_keys=True, ensure_ascii=False)
            for name, schema in zip(plan.tools, schemas)
        }
        changes = {
            "added": sorted(current.keys() - self._last_tools.keys()),
            "removed": sorted(self._last_tools.keys() - current.keys()),
            "updated": sorted(
                name
                for name in current.keys() & self._last_tools.keys()
                if current[name] != self._last_tools[name]
            ),
        }
        instructions = plan.instructions
        if any(changes.values()):
            instructions += "\n本次可用工具变化：" + json.dumps(
                changes, ensure_ascii=False
            )
        with self.tracer.start_as_current_span(
            "deta.model.request", record_exception=False, set_status_on_exception=False
        ) as span:
            signature = hashlib.sha256(
                json.dumps(current, sort_keys=True).encode()
            ).hexdigest()
            span.set_attribute("deta.tools_hash", signature)
            for key, names in changes.items():
                span.set_attribute(f"deta.tools.{key}", names)
            for retry_index in range(budget.options.max_retries + 1):
                observed = False

                def observe(event: Event) -> None:
                    """记录是否已经发布文本或参数增量，并把同一个事件交给 Loop。"""
                    nonlocal observed
                    if isinstance(event, (TextDelta, ToolCallDelta)):
                        observed = True
                    listener(event)

                try:
                    message = await stream_once(
                        self.client,
                        self.config,
                        instructions,
                        plan.messages,
                        schemas,
                        tracer=self.tracer,
                        artifacts=self.artifacts,
                        listeners=[observe],
                        before_attempt=budget.take_request,
                    )
                    self._last_tools = current
                    span.set_attribute("deta.retry_count", retry_index)
                    return message
                except asyncio.CancelledError:
                    span.set_status(Status(StatusCode.ERROR, "CancelledError"))
                    raise
                except Exception as exc:
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
    ) -> ToolResult:
        """将当前工具快照、环境和前后 Hook 交给统一工具执行器。"""
        return await execute_tool(
            call,
            self.workspace,
            tracer=self.tracer,
            artifacts=self.artifacts,
            registry=plan.tools,
            on_start=on_start,
            context=ToolContext(
                self.workspace,
                self.artifacts.root.parent / "tool-output",
                self.shell,
                MappingProxyType(self.environment),
                on_output,
            ),
            hooks=self.hooks,
        )

    async def _commit(self, message: AgentMessage) -> None:
        """提交一条最终消息；Day 8 在该边界接事务，不在 Loop 再加一份保存逻辑。"""
        self.agent.messages.append(message)

    async def _finish_turn(self, report: TurnReport) -> TurnDecision:
        """把完整报告副本交给结束 Hook，未配置时返回 auto 自然决策。"""
        if self.hooks.finish_turn is None:
            return "auto"
        return await self.hooks.finish_turn(copy_report(report))
```

</details>

<details>
<summary>参考答案：src/deta/tools.py（完整文件）</summary>

```python
import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai.types.chat import ChatCompletionToolParam
from opentelemetry.trace import Status, StatusCode, Tracer
from pydantic import BaseModel, ValidationError

from deta.builtin_tools import ToolContext, ToolOutput
from deta.builtin_tools._files import MutationError
from deta.builtin_tools.bash import BashArgs, run_bash
from deta.builtin_tools.edit import EditArgs, edit_file
from deta.builtin_tools.read import ReadArgs, ReadError, read_file
from deta.builtin_tools.write import WriteArgs, write_file
from deta.hooks import BeforeToolDecision, Hooks
from deta.observability.artifacts import Artifacts
from deta.types import ToolCall, ToolResult


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
    # 同步执行函数，接收已验证的 Args 与工作目录，返回文本或 ToolOutput。
    handler: Callable[[Args, Path], str | ToolOutput] | None = None
    # 异步工具接收 ToolContext；与同步 handler 必须恰好提供一个。
    async_handler: Callable[[Args, ToolContext], Awaitable[ToolOutput]] | None = None

    def __post_init__(self) -> None:
        """在组装工具定义时拒绝缺失或重复的执行入口，避免运行时再猜测调用方式。"""
        if (self.handler is None) == (self.async_handler is None):
            raise ValueError("ToolSpec 必须恰好提供一个执行函数")


TOOLS: dict[str, ToolSpec[Any]] = {
    "read": ToolSpec(
        name="read",
        description="读取 UTF-8 文本，返回行号和分页提示。",
        args_model=ReadArgs,
        handler=read_file,
    ),
    "bash": ToolSpec(
        name="bash",
        description="在工作目录运行 shell 命令，保留输出并返回退出码。",
        args_model=BashArgs,
        async_handler=run_bash,
    ),
    "write": ToolSpec(
        name="write",
        description="创建或覆盖文本文件，返回 diff。",
        args_model=WriteArgs,
        handler=write_file,
    ),
    "edit": ToolSpec(
        name="edit",
        description="按原文件唯一匹配批量替换，返回 diff。",
        args_model=EditArgs,
        handler=edit_file,
    ),
}


def tool_schemas(
    registry: Mapping[str, ToolSpec[Any]] | None = None,
) -> list[ChatCompletionToolParam]:
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
    """接收模型提出的 ToolCall，按名称查找工具并验证原始参数 JSON。
    返回工具定义和参数对象给 execute_tool；未知名称抛 KeyError，参数错误抛 ValidationError。
    """
    spec = (TOOLS if registry is None else registry)[call.name]
    args = spec.args_model.model_validate_json(call.arguments_json)
    return spec, args


async def invoke_handler(
    spec: ToolSpec[Any], args: BaseModel, context: ToolContext
) -> ToolOutput:
    """按定义调用同步或异步工具，并保证同步文件修改在取消后也完成收尾。

    shield 只保护等待中的线程任务；若已请求取消，等待线程结束后仍传播取消。
    """
    if spec.async_handler is not None:
        return await spec.async_handler(args, context)
    handler = spec.handler
    if handler is None:
        raise RuntimeError("同步工具缺少 handler")
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


async def execute_tool(
    call: ToolCall,
    workspace: Path,
    *,
    tracer: Tracer,
    artifacts: Artifacts,
    registry: Mapping[str, ToolSpec[Any]] | None = None,
    on_start: Callable[[], None] | None = None,
    context: ToolContext | None = None,
    hooks: Hooks | None = None,
) -> ToolResult:
    """统一校验、执行决策、工具执行与结果后处理，保存原始和最终结果的对应关系。

    业务错误返回配对结果；Hook、产物之外的内部错误和取消向外传播。
    """
    table = TOOLS if registry is None else registry
    policy = hooks or Hooks()
    with tracer.start_as_current_span(
        "deta.tool.dispatch", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("deta.tool_call_id", call.id)
        span.set_attribute("deta.tool_name", call.name)
        span.set_attribute("deta.execution_started", False)

        def result_from(output: ToolOutput) -> ToolResult:
            """把业务输出与当前调用身份配对，保留停止提示和诊断字段。"""
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=output.content,
                error_code=output.error_code,
                details=output.details,
                terminate=output.terminate,
            )

        try:
            call_ref = artifacts.save("tool-call", call.model_dump(mode="json"))
            if call_ref is not None:
                span.set_attribute("deta.call_artifact", call_ref)
            if call.name not in table:
                raw = result_from(ToolOutput("工具不存在", "unknown_tool"))
            else:
                try:
                    spec, args = resolve_tool_call(call, table)
                except ValidationError:
                    raw = result_from(
                        ToolOutput(
                            "参数错误，请按工具 schema 提供完整 JSON 对象。",
                            "invalid_arguments",
                        )
                    )
                else:
                    decision = BeforeToolDecision()
                    if policy.before_tool is not None:
                        decision = await policy.before_tool(
                            call, args.model_copy(deep=True)
                        )
                    if not isinstance(decision, BeforeToolDecision):
                        raise TypeError("before_tool 必须返回 BeforeToolDecision")
                    if not decision.allow:
                        raw = result_from(
                            ToolOutput(
                                decision.reason or "本次工具执行被 Hook 拒绝",
                                "denied",
                                terminate=decision.terminate,
                            )
                        )
                    else:
                        if context is None:
                            if spec.async_handler is not None:
                                raise RuntimeError("异步工具必须提供 ToolContext")
                            # 仅供旧的直接文件工具入口使用，忽略流式输出。
                            context = ToolContext(
                                workspace,
                                artifacts.root / "tool-output",
                                "/bin/zsh",
                                {},
                                lambda _text: None,
                            )
                        span.set_attribute("deta.execution_started", True)
                        if on_start is not None:
                            on_start()
                        try:
                            with tracer.start_as_current_span(
                                "deta.tool.execute",
                                record_exception=False,
                                set_status_on_exception=False,
                            ) as execution:
                                try:
                                    output = await invoke_handler(spec, args, context)
                                except BaseException as exc:
                                    execution.set_status(
                                        Status(StatusCode.ERROR, type(exc).__name__)
                                    )
                                    raise
                        except ReadError as exc:
                            output = ToolOutput(str(exc), "read_failed")
                        except MutationError as exc:
                            output = ToolOutput(str(exc), f"{call.name}_failed")
                        except (OSError, UnicodeError) as exc:
                            output = ToolOutput(
                                f"文件操作失败：{type(exc).__name__}",
                                f"{call.name}_failed",
                            )
                        raw = result_from(output)
            raw_ref = artifacts.save("tool-raw", raw.model_dump(mode="json"))
            if raw_ref is not None:
                span.set_attribute("deta.raw_artifact", raw_ref)
            result = raw
            if policy.after_tool is not None:
                result = await policy.after_tool(call, raw.model_copy(deep=True))
            if not isinstance(result, ToolResult) or (
                result.tool_call_id != call.id
                or result.name != call.name
                or result.role != "tool"
            ):
                raise ValueError("after_tool 改坏了工具结果的调用身份")
            result = ToolResult.model_validate(result.model_dump())
            ref = artifacts.save("tool-result", result.model_dump(mode="json"))
            if ref is not None:
                span.set_attribute("deta.result_artifact", ref)
            span.set_attribute("deta.outcome", result.error_code or "success")
            span.set_attribute("deta.terminate", result.terminate)
            if result.is_error:
                span.set_status(Status(StatusCode.ERROR, result.error_code))
            return result
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
```

</details>

## 像调试器一样看请求选择

### 1. 第一轮与后续准备

第一次请求也经过 prepare_request → transform_context → SDK 转换。第一轮没有上一轮报告，所以不调用 prepare_next_turn。

后续轮次先调用 prepare_next_turn，再提交“准备消息 + 本批已选中的队列输入”，然后进行 prepare_request。若准备开始前已经选到一条 Steering，准备结束后不再多取一条；若此前没选到，则准备结束后补取，以接住准备期间到达的输入。

这个补取规则针对 prepare_next_turn。新输入若在后面的 prepare_request 或模型请求期间到达，会留待之后的调度边界，不会改写一个已经在发送的请求。队列目前在内存中，取出与历史提交还没有持久化事务；进程退出或提交前中断时不能承诺恢复未提交输入，Day 8 要另外定义可靠保存边界。

### 2. 工具结果与显式继续只触发一个下一轮

假设本轮执行 read，finish_turn 又返回 continue。工具已经产生自然续轮条件，所以清除额外的 explicit 标记，只发下一次请求。Steering 或 Follow-up 选择了下一轮时也一样。

只有没有工具自然续轮、没有可消费输入时，显式 continue 才独自触发一次上下文请求。它不创建假的用户消息，也不意味着跳过 prepare_request。

### 3. 显式结束与队列

finish_turn=end 时，在取下一批 Steering/Follow-up 之前结束，因此未取出的队列仍在 Agent 上。wait 返回之后，调用者可以检查队列，再明确调用 continue_ 或开始新 prompt。

如果结果中只有一个 terminate=True，其他结果为 False，整批仍允许自然续轮。只有非空整批都要求终止才停止工具自动续轮；这个提示不强制丢弃排队的新任务，显式 end 才是这里的强制结束决策。

### 4. prompt 与 continue_ 的合法边界

| 历史状态 | continue_ 行为 |
| --- | --- |
| 空历史 | 拒绝，即使配置了系统指令也不是一个可继续的任务 |
| 末尾是 UserMessage | 在已有输入上请求，不再重复追加同一问题 |
| 末尾是完整配对的 ToolResult | 可继续请求模型 |
| 有 pending 工具调用 | 拒绝，结果未知的工具不能自动重放 |
| 末尾是 AssistantMessage 且无排队输入 | 拒绝，需新 prompt 或先排队 |
| 末尾助手，Steering 非空 | 排空已存在的 Steering 作为本次初始输入，并跳过首次额外轮询 |
| 末尾助手，仅 Follow-up 非空 | 排空 Follow-up 作为初始输入，正常处理首次 Steering |

助手末尾显式 continue 的 drain_all 与普通 one/all 轮询分开，这是对 Pi 入口语义的保留。正常运行过程中仍按配置模式消费。

## 看懂一次重试的 Trace

```text
deta.run
  └─ deta.turn
       └─ deta.model.request             一个逻辑请求
            ├─ deta.model.input         第一次输入快照
            │    └─ deta.model.attempt   实际尝试编号 n
            ├─ retry_scheduled          退避与错误类别
            └─ deta.model.input         同一计划的下一次尝试
                 └─ deta.model.attempt  实际尝试编号 n+1
```

SDK max_retries 仍为零，重试只有 runtime 这一层。尚未发布文本/参数增量时，连接错误、超时、408/429/5xx 才可在额度内重试；一旦观察者已收到增量，就把失败交给 Run，避免把两次答案片段拼在一起。

重试不会再次执行已提交的工具。若是下一轮模型请求失败，重试的输入仍包含原来的工具结果；工具执行本身没有自动重试。取消、协议错误、Hook/内部错误与鉴权失败也不靠普通重试掩盖。退避时间计入 Run 总时限。

配置转换失败没有发出 Attempt；真正到达 SDK 调用边界才计数。远端是否接收或完成仍可能未知，Attempt 计数不能解释成远端一定成功执行的次数。

## 工具前后 Hook 的四条路径

| 路径 | before_tool | handler | after_tool |
| --- | --- | --- | --- |
| 未知名称 | 跳过 | 不执行 | 接收 unknown_tool |
| 参数无效 | 跳过 | 不执行 | 接收 invalid_arguments |
| 前置 Hook 拒绝 | 执行并返回拒绝 | 不执行 | 接收 denied |
| 校验并放行 | 执行 | 执行 | 接收成功或普通工具失败 |

before_tool 得到参数副本；改变这个副本不能偷偷改变实际执行参数。after_tool 得到 raw 的副本，可调整正文、错误状态和 terminate，但必须保留 role、name、tool_call_id。原始与最终结果分别保存，不能用脱敏后的 final 覆盖 raw 的事实。

输出长度截断、整批预算拒绝等 Loop 硬保护在调度前生成结果，不让工具 Hook 重新放行。Hook 本身异常直接使 Run 失败，取消也保持取消路径；后置 Hook 失败时工具可能已产生效果，因此不能自动重放。

## 正常 API 使用示例

下面函数接收已经按 Day 4/Day 6 创建好的 AgentSession，在同一运行中排入指令和后续任务；它使用真实 Agent，不替换模型响应。

```python
from deta.runtime import AgentSession
from deta.types import RunResult


async def run_with_queued_input(session: AgentSession) -> RunResult:
    """启动一次真实读取任务，并通过两类队列提交后续要求，最后返回完整运行结果。"""
    session.agent.steering.mode = "one"
    session.agent.followups.mode = "all"
    session.agent.start("读取 target.md 并说明项目范围")
    session.agent.steer("说明时区分已实现能力与规划")
    session.agent.follow_up("完成后给出接下来的一项实现任务")
    return await session.agent.wait()
```

Python 的 continue 是关键字，因此入口叫 continue_。设置模式与排队只改变当前实例，不写全局配置。

## 怎样核对

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
uv run deta --help
```

在实际写入源码后运行这些检查，再完成下方手工验收。静态检查通过不代表真实模型或工具行为已经验证。

首先重跑一次 Day 4 的真实读取主链路和 Day 6 的已有检查命令，确认接入队列与 Hooks 后仍然复用同一模型/工具边界。

再用普通 Python 调用与实际 Hook 配置逐项核对：

- 首次请求有 prepare_request，第二轮起才有 prepare_next_turn；Trace 和消息顺序能对应。
- one 模式在一次后续准备前已经选到输入时，准备结束后不会多取一条；all 模式消费当前批次全部输入。
- 显式 end 留下尚未消费的队列；显式 continue 与工具或队列同时成立时没有多余请求。
- 空历史继续、助手末尾无输入继续、pending 历史继续都明确拒绝，且不会改坏原历史。
- 已知工具参数错误成为配对失败结果，模型可以修正；后置 Hook 保留调用身份，raw/final 可对应。
- 全批 terminate 与仅单个 terminate 的调度结果不同；总请求和整批工具额度有清晰停止原因。
- 取消和等待结束之间仍保持 running，清理期间第二次启动被拒绝。

请求重试需要真实可观测的失败记录；没有遇到相应故障时标记未验证，不伪造 Attempt，不通过 mock 或新建故障测试文件补齐表面覆盖。每个记录都写输入、实际事件/Span、结果和未验证边界。

## Pi 对照与本阶段边界

| Pi 位置/语义 | Deta 对应 |
| --- | --- |
| `agent-loop.ts` 内层工具/Steering、外层 Follow-up | run_loop 的两层循环 |
| 首次 prepareRequest、后续 prepareNextTurn 与准备期间补取 | 绑定回调及 pending 是否为空的判断 |
| finishTurn end 优先、continue 与自然请求合并 | 决策后先结束，再选择下一轮来源 |
| shouldTerminateToolBatch | 非空结果批次的 all(result.terminate) |
| `agent.ts` continue 的助手末尾队列处理 | Agent.continue_ 的 Steering/Follow-up drain_all |
| beforeToolCall / afterToolCall | 参数之后的前置决策、raw/final 后置处理 |

Deta 不逐字段兼容 Pi 的 TypeScript API。本版仍只支持文本/函数工具、串行执行与内存队列。工具变化通过实际请求 schema、系统指令中的变更说明和 Trace 记录，没有新增一套独立持久化工具声明历史；Day 8 之后需把恢复所需配置可靠纳入 Session。

本页实现单请求边界的有限重试，不包含提供方上下文溢出后的压缩恢复，那属于 Day 10–11。错误和取消通过异常/终态事件退出，正常 finish_turn 决策不接管硬错误恢复。完成本日手工验收后，继续 [Day 8：Session、SQLite 与恢复边界](day8.md)。
