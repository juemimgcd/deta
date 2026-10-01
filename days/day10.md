# Day 10：Compaction 的切点与准备算法

[总览](summary.md) · [前一天](day9.md) · [下一天](day11.md)

## 核心问题

输入接近窗口上限时，不能简单保留最后 N 条消息：尾部可能从一条 ToolMessage 开始，也可能仍处于同一个很长的用户任务中。今天先把要总结的范围与要保留的范围选对，再考虑让模型写摘要。

本页只新增 `compaction.py`。直接复用 Day 9 的 ContextItem、ContextView、预算估算和最新摘要展开，不再写第二个上下文构建器。准备函数没有模型调用、数据库写入或文件修改。

## 今天新增什么

| 对象或函数 | 本日作用 |
| --- | --- |
| `CutPoint` | 保存首个保留项的位置，以及被切开用户任务的起点 |
| `FileOperations` | 整理摘要范围提及的读取/修改目标与待核对文件 |
| `CompactionPreparation` | 已有摘要、较早历史、任务前缀、连续尾部、估算与快照身份 |
| `PreparationResult` | 区分可准备与空记录、没有新增内容、工具未结清、没有范围 |
| `find_cut_point / find_turn_start_index` | 找合法切点及其所属用户任务 |
| `collect_file_operations` | 合并旧摘要文件信息和本次新摘要范围中的工具目标 |
| `prepare_compaction` | 校验当前视图/估算后，返回纯准备结果 |

Day 9 已完成“有多大、是否接近上限”，Day 10 回答“准备总结哪一段”，Day 11 才完成“请求摘要、保存、重新构建并继续”。本日不把自动触发塞进 Loop。

## 先区分用户任务和模型 Turn

```text
一次用户任务
  HumanMessage：修复某个问题
  AIMessage：提出 read
  ToolMessage：读取结果
  AIMessage：提出 edit
  ToolMessage：修改结果
  AIMessage：提出 bash
  ToolMessage：检查结果
  AIMessage：最终说明
```

这个任务包含多次模型 Turn。切点可以位于任务内部，但必须保留工具批次的完整关系。代码中保留 Pi 风格的 turn_prefix 命名，本页把它解释为“被切开用户任务的前缀”，不能误读成一次 SDK 响应的前半段。

用户任务起点只认来源为 message 的 HumanMessage；Context 中的历史摘要或 context_note 虽然也转换成用户角色，却不能冒充用户刚发起的新任务。

## 合法切点怎样选

1. 从尾部向前逐项累计 Day 9 的消息 token 估算。
2. 达到 keep_recent_tokens 后，从该位置起选择最近的合法消息起点。
3. 合法起点不能是 ToolMessage；若预算位置落进工具结果批次，要对齐到后面的完整消息边界。
4. 若没有更晚合法起点，则退回最早合法起点；因此可能保留全部，返回 no_range。

keep_recent_tokens 是尾部保留目标，不是精确 token 上限。合法边界可能使尾部小于目标；遇到无法拆开的长工具批次，也可能保留过多而没有压缩范围。算法不切工具正文，不构造孤立结果来强凑额度。

```text
普通切点恰好位于新用户任务：
  [较早任务们] | [User B → Assistant → ToolMessage → ...]
    history               retained_tail
    turn_prefix 为空

切点位于 User B 的长任务内部：
  [较早任务们] [User B → ... 已完成的前半段] | [Assistant → ToolMessage → ...]
    history             turn_prefix                    retained_tail

只有一个很长的用户任务：
  [User A → ... 已完成的前半段] | [Assistant → ToolMessage → ...]
          turn_prefix                    retained_tail
  history 可以为空；任务前缀仍是可总结内容
```

在消息配对已经有效的前提下，切点不落在 ToolMessage，保证调用助手与它的结果不被拆到两边。prepare_compaction 先复用 pending_calls 检查未结清调用；未完成工具要先结清或按 Day 8 恢复，不能用摘要掩盖状态空洞。

## 已有摘要时的输入

```text
Day 9 ContextView
  [最近摘要占位项] [按 retained_entry_ids 还原的上次尾部] [压缩后新增消息]
        │                     │
        │                     └─ 本次候选消息，重新选切点
        └─ previous_summary，单独传给 Day 11 的摘要更新步骤

本次候选消息
  history + turn_prefix + retained_tail
```

不重新读取已经被摘要覆盖的全部旧 Entry，也不把摘要占位项混进普通历史再总结一遍。上次尾部中的一部分可能在这次进入 history/prefix，剩余部分与新增消息共同组成新的连续尾部。

## 先认识本日的属性

| 属性 | 含义与后续消费者 |
| --- | --- |
| `CutPoint.first_kept` | candidates 中首个保留项下标，切片时由此划分尾部 |
| `CutPoint.task_start` | 切入某个用户任务时，该任务起点；切在新任务起点时为 None |
| `snapshot_tip_id` | 准备所用快照末尾，Day 11 提交前核对是否过期 |
| `previous_compaction_id / previous_summary` | 上次压缩身份与正文，增量更新时单独使用 |
| `history / turn_prefix / retained_tail` | 准备期间三段不重叠的 ContextItem 元组，各自保存来源；Day 11 落库时尾部只保存 ID |
| `tokens_before` | Day 9 对同一消息视图得到的输入规模估算 |
| `retained_tokens / keep_recent_tokens` | 实际选中尾部的启发式估算与原保留目标 |
| `file_operations` | 已有摘要元数据与新摘要范围涉及文件的合并信息 |
| `is_split_turn` | 是否存在非空任务前缀，供 Day 11 选择摘要步骤 |

FileOperations 保存的是工具请求提及的路径：read 归入读取目标，write/edit 归入修改目标；失败或缺失最终结果的路径另列为待核对。after_tool 能改写最终结果，因此这些集合不能单独证明磁盘效果；bash 的任意命令也不会被正则猜成文件操作。

## 先认识本日的函数

| 函数 | 输入、输出与调用关系 |
| --- | --- |
| `find_turn_start_index` | 候选项与切点下标 → 最近的真实 HumanMessage 下标或 None |
| `find_cut_point` | 已配对的候选消息与保留目标 → CutPoint |
| `collect_file_operations` | 新摘要范围与旧 CompactionRecord → 合并后的 FileOperations |
| `prepare_compaction` | ContextView、保留目标、同视图的 ContextEstimate → PreparationResult |

prepare_compaction 不自行判断“现在是否必须压缩”。调用方先根据 estimate.needs_compaction 或明确的手动请求决定是否准备；之后仍可能得到 no_range。输入快照和估算的 messages_hash 不一致时直接抛错，避免用另一段历史的数字描述当前范围。

## 准备结果有哪些

| status | 条件 | preparation |
| --- | --- | --- |
| `ready` | 找到可总结前缀，尾部关系完整 | 完整准备对象 |
| `empty` | 没有任何投影消息 | None |
| `unchanged` | 已有摘要且其后没有新增可投影消息 | None |
| `pending_tools` | 仍有已声明但未结清的工具 | None |
| `no_range` | 只有摘要，或合法边界要求保留全部 | None |

无效预算、估算与消息不匹配、错误的消息配对属于调用错误，抛出异常；上表是正常的“这次是否有范围可准备”的结果。一个极长、不可再拆的单条消息可能没有范围，不能写空摘要并宣称压缩成功。

## Compaction 骨架

类、属性和寻找用户任务起点的辅助函数直接提供。填写三个函数：

1. `find_cut_point`：计算合法起点，从后向前累计，再对齐起点并找所属用户任务。
2. `collect_file_operations`：先合并上次文件信息，再解析本次 history/prefix 的已声明 read/write/edit 目标；不扫描 retained_tail。
3. `prepare_compaction`：处理正常边界、核对估算与配对、去掉摘要占位项、切三段、复制消息与来源并返回。

### src/deta/compaction.py

只填写：`find_cut_point`、`collect_file_operations`、`prepare_compaction`。导入、类型、属性与其他辅助实现直接提供。

```python
# ruff: noqa: F401  # 为 TODO 预留的导入。
import json
from collections.abc import Sequence
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deta.context import (
    CompactionRecord,
    ContextEstimate,
    ContextItem,
    ContextView,
    estimate_message,
    messages_hash,
)
from deta.loop import pending_calls
from deta.types import Data


class CutPoint(Data):
    """说明从哪一条保留，以及是否切进了一次用户任务内部。"""

    # 第一个保留项在候选消息中的下标，范围为 [0, len(items))。
    first_kept: int
    # 被切开的用户任务起点；恰好切在新任务起点或找不到用户输入时为 None。
    task_start: int | None


class FileOperations(Data):
    """整理摘要范围内工具请求涉及的文件，不能单凭这些路径认定磁盘效果。"""

    # read 请求提及、且未在修改目标集合中的路径。
    read_files: tuple[str, ...]
    # write/edit 请求提及的路径，不等于已经发生的实际 diff。
    modified_files: tuple[str, ...]
    # 最终结果失败或缺失的调用涉及的路径，摘要应保留待核对说明。
    uncertain_files: tuple[str, ...]


class CompactionPreparation(Data):
    """保存一次纯准备的结果；Day 11 使用它生成摘要并核对快照后提交。"""

    # 准备时的会话末尾 ID；生成期间历史变化时不能直接提交旧准备结果。
    snapshot_tip_id: str
    # 上次压缩条目，用于记录本次增量摘要的来源。
    previous_compaction_id: str | None
    # 已有摘要单独交给摘要更新步骤，不混进新历史重复总结。
    previous_summary: str | None
    # 切点所在用户任务之前的较早历史。
    history: tuple[ContextItem, ...]
    # 若切入一个任务，保存该任务中早于保留尾部的前缀。
    turn_prefix: tuple[ContextItem, ...]
    # 按原顺序保留的连续尾部，包含完整工具批次和来源。
    retained_tail: tuple[ContextItem, ...]
    # Day 9 对本次视图得到的完整输入估算，包含系统指令和工具声明的影响。
    tokens_before: int
    # 保留尾部的逐消息启发式估算，不是假称已经测得的新请求 usage。
    retained_tokens: int
    # 本次范围选择使用的保留目标；合法边界可能使实际保留量偏离目标。
    keep_recent_tokens: int
    # 已有摘要文件信息与本次新摘要范围提及路径的合并结果。
    file_operations: FileOperations

    @property
    def is_split_turn(self) -> bool:
        """有单独的任务前缀时返回 True；这里的任务不是一次模型 Turn。"""
        return bool(self.turn_prefix)


class PreparationResult(Data):
    """区分可准备与不能准备的正常边界，避免把没有范围误当作空摘要写入。"""

    # ready 为可继续生成；其余值说明为什么本次没有压缩准备结果。
    status: Literal["ready", "empty", "unchanged", "pending_tools", "no_range"]
    # ready 时提供完整准备对象，其他状态为 None。
    preparation: CompactionPreparation | None = None
    # 给调用方的具体原因，不暗示已经完成摘要或节省了 token。
    reason: str = ""


def find_turn_start_index(items: Sequence[ContextItem], index: int) -> int | None:
    """从切点向前找真正的用户输入；摘要和自定义备注不冒充新任务起点。"""
    for position in range(index, -1, -1):
        item = items[position]
        if item.source == "message" and isinstance(item.message, HumanMessage):
            return position
    return None


def find_cut_point(items: Sequence[ContextItem], keep_recent_tokens: int) -> CutPoint:
    """从后向前累计估算，再把切点对齐到更晚的合法消息起点。

    调用方已确认消息配对；找不到更晚合法起点时保留全部，交给上层判为无范围。

    TODO：从末尾累计 estimate_message；达到保留目标后对齐到不为 ToolMessage 的合法起点，再判断是否切入用户任务。
    """
    raise NotImplementedError("请完成 find_cut_point")


def collect_file_operations(
    items: Sequence[ContextItem],
    previous: CompactionRecord | None,
) -> FileOperations:
    """合并既有文件信息与新摘要范围中的工具目标，解析失败不猜路径，bash 不猜副作用。

    TODO：合并既有元数据与本次待摘要项的工具目标；失败或缺失结果进入 uncertain_files，bash 不推测路径。
    """
    raise NotImplementedError("请完成 collect_file_operations")


def prepare_compaction(
    view: ContextView,
    keep_recent_tokens: int,
    estimate: ContextEstimate,
) -> PreparationResult:
    """复用 Day 9 的视图，拆出较早历史、任务前缀与尾部；不请求模型、不写数据库。

    TODO：处理空记录与未新增、核对估算和未结清工具、拆分三段；返回快照身份与准备数据，不请求模型、不写库。
    """
    raise NotImplementedError("请完成 prepare_compaction")
```

## 参考答案

<details>
<summary>参考答案：src/deta/compaction.py（完整文件）</summary>

```python
from collections.abc import Sequence
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deta.context import (
    CompactionRecord,
    ContextEstimate,
    ContextItem,
    ContextView,
    estimate_message,
    messages_hash,
)
from deta.loop import pending_calls
from deta.types import Data


class CutPoint(Data):
    """说明从哪一条保留，以及是否切进了一次用户任务内部。"""

    # 第一个保留项在候选消息中的下标，范围为 [0, len(items))。
    first_kept: int
    # 被切开的用户任务起点；恰好切在新任务起点或找不到用户输入时为 None。
    task_start: int | None


class FileOperations(Data):
    """整理摘要范围内工具请求涉及的文件，不能单凭这些路径认定磁盘效果。"""

    # read 请求提及、且未在修改目标集合中的路径。
    read_files: tuple[str, ...]
    # write/edit 请求提及的路径，不等于已经发生的实际 diff。
    modified_files: tuple[str, ...]
    # 最终结果失败或缺失的调用涉及的路径，摘要应保留待核对说明。
    uncertain_files: tuple[str, ...]


class CompactionPreparation(Data):
    """保存一次纯准备的结果；Day 11 使用它生成摘要并核对快照后提交。"""

    # 准备时的会话末尾 ID；生成期间历史变化时不能直接提交旧准备结果。
    snapshot_tip_id: str
    # 上次压缩条目，用于记录本次增量摘要的来源。
    previous_compaction_id: str | None
    # 已有摘要单独交给摘要更新步骤，不混进新历史重复总结。
    previous_summary: str | None
    # 切点所在用户任务之前的较早历史。
    history: tuple[ContextItem, ...]
    # 若切入一个任务，保存该任务中早于保留尾部的前缀。
    turn_prefix: tuple[ContextItem, ...]
    # 按原顺序保留的连续尾部，包含完整工具批次和来源。
    retained_tail: tuple[ContextItem, ...]
    # Day 9 对本次视图得到的完整输入估算，包含系统指令和工具声明的影响。
    tokens_before: int
    # 保留尾部的逐消息启发式估算，不是假称已经测得的新请求 usage。
    retained_tokens: int
    # 本次范围选择使用的保留目标；合法边界可能使实际保留量偏离目标。
    keep_recent_tokens: int
    # 已有摘要文件信息与本次新摘要范围提及路径的合并结果。
    file_operations: FileOperations

    @property
    def is_split_turn(self) -> bool:
        """有单独的任务前缀时返回 True；这里的任务不是一次模型 Turn。"""
        return bool(self.turn_prefix)


class PreparationResult(Data):
    """区分可准备与不能准备的正常边界，避免把没有范围误当作空摘要写入。"""

    # ready 为可继续生成；其余值说明为什么本次没有压缩准备结果。
    status: Literal["ready", "empty", "unchanged", "pending_tools", "no_range"]
    # ready 时提供完整准备对象，其他状态为 None。
    preparation: CompactionPreparation | None = None
    # 给调用方的具体原因，不暗示已经完成摘要或节省了 token。
    reason: str = ""


def find_turn_start_index(items: Sequence[ContextItem], index: int) -> int | None:
    """从切点向前找真正的用户输入；摘要和自定义备注不冒充新任务起点。"""
    for position in range(index, -1, -1):
        item = items[position]
        if item.source == "message" and isinstance(item.message, HumanMessage):
            return position
    return None


def find_cut_point(items: Sequence[ContextItem], keep_recent_tokens: int) -> CutPoint:
    """从后向前累计估算，再把切点对齐到更晚的合法消息起点。

    调用方已确认消息配对；找不到更晚合法起点时保留全部，交给上层判为无范围。
    """
    if not items or keep_recent_tokens <= 0:
        raise ValueError("候选消息非空且保留目标必须为正数")
    points = [
        i for i, item in enumerate(items) if not isinstance(item.message, ToolMessage)
    ]
    if not points:
        raise ValueError("候选消息没有合法起点")
    cut = points[0]
    accumulated = 0
    for index in range(len(items) - 1, -1, -1):
        accumulated += estimate_message(items[index].message)
        if accumulated >= keep_recent_tokens:
            cut = next((point for point in points if point >= index), points[0])
            break
    item = items[cut]
    starts_task = item.source == "message" and isinstance(item.message, HumanMessage)
    return CutPoint(
        first_kept=cut,
        task_start=None if starts_task else find_turn_start_index(items, cut),
    )


def collect_file_operations(
    items: Sequence[ContextItem],
    previous: CompactionRecord | None,
) -> FileOperations:
    """合并既有文件信息与新摘要范围中的工具目标，解析失败不猜路径，bash 不猜副作用。"""
    reads = set(previous.read_files if previous else ())
    modified = set(previous.modified_files if previous else ())
    uncertain = set(previous.uncertain_files if previous else ())
    outcomes = {
        item.message.tool_call_id: item.message
        for item in items
        if isinstance(item.message, ToolMessage)
    }
    for item in items:
        if not isinstance(item.message, AIMessage):
            continue
        for call in item.message.tool_calls:
            if call["name"] not in {"read", "write", "edit"}:
                continue
            arguments = call["args"]
            path = arguments.get("path") if isinstance(arguments, dict) else None
            if not isinstance(path, str) or not path:
                continue
            (reads if call["name"] == "read" else modified).add(path)
            result = outcomes.get((call["id"] or ""))
            if result is None or (result.status == "error"):
                uncertain.add(path)
    return FileOperations(
        read_files=tuple(sorted(reads - modified)),
        modified_files=tuple(sorted(modified)),
        uncertain_files=tuple(sorted(uncertain)),
    )


def prepare_compaction(
    view: ContextView,
    keep_recent_tokens: int,
    estimate: ContextEstimate,
) -> PreparationResult:
    """复用 Day 9 的视图，拆出较早历史、任务前缀与尾部；不请求模型、不写数据库。"""
    if keep_recent_tokens <= 0:
        raise ValueError("保留目标必须为正数")
    if not view.items:
        return PreparationResult(status="empty", reason="没有可压缩消息")
    if view.compaction is not None and view.new_messages == 0:
        return PreparationResult(
            status="unchanged", reason="上次压缩后没有新增可投影消息"
        )
    if estimate.messages_hash != messages_hash(view.messages):
        raise ValueError("输入估算不属于当前消息视图")
    if pending_calls(view.messages):
        return PreparationResult(
            status="pending_tools", reason="先结清或恢复未完成工具，再准备摘要"
        )
    candidates = tuple(item for item in view.items if item.source != "summary")
    if not candidates or view.tip_id is None:
        return PreparationResult(
            status="no_range", reason="仅有摘要，没有新的原始消息范围"
        )
    cut = find_cut_point(candidates, keep_recent_tokens)
    if cut.first_kept == 0:
        return PreparationResult(
            status="no_range", reason="保留目标与合法边界要求保留全部消息"
        )
    history_end = cut.task_start if cut.task_start is not None else cut.first_kept
    history = candidates[:history_end]
    prefix = candidates[history_end : cut.first_kept]
    tail = candidates[cut.first_kept :]
    preparation = CompactionPreparation(
        snapshot_tip_id=view.tip_id,
        previous_compaction_id=view.compaction_id,
        previous_summary=view.compaction.summary if view.compaction else None,
        history=tuple(item.model_copy(deep=True) for item in history),
        turn_prefix=tuple(item.model_copy(deep=True) for item in prefix),
        retained_tail=tuple(item.model_copy(deep=True) for item in tail),
        tokens_before=estimate.tokens,
        retained_tokens=sum(estimate_message(item.message) for item in tail),
        keep_recent_tokens=keep_recent_tokens,
        file_operations=collect_file_operations((*history, *prefix), view.compaction),
    )
    return PreparationResult(status="ready", preparation=preparation)
```

</details>

## 正常 API 使用示例

下面函数接收已经打开的真实 Session 和明确的请求配置。它复用同一个 view 进行估算与准备，返回对象给调用方检查；不存在摘要 API 请求或 Compaction Entry 写入。

```python
from collections.abc import Sequence

from deta.compaction import PreparationResult, prepare_compaction
from deta.context import build_context, estimate_context
from deta.session import Session
from deta.types import ToolSchema


def prepare_session_compaction(
    session: Session,
    *,
    model: str,
    instructions: str,
    schemas: Sequence[ToolSchema],
    context_window: int,
    output_tokens: int,
    keep_recent_tokens: int,
    safety_tokens: int = 1024,
) -> PreparationResult:
    """读取真实会话快照并返回压缩准备结果，生成与提交由后续调用方完成。"""
    view = build_context(session.entries())
    estimate = estimate_context(
        view.messages,
        model=model,
        instructions=instructions,
        tools=schemas,
        window_tokens=context_window,
        output_tokens=output_tokens,
        safety_tokens=safety_tokens,
    )
    return prepare_compaction(view, keep_recent_tokens, estimate)
```

instructions 和 schemas 由调用方提供，需对应被评估的实际配置；消息若又经过 Hook 改写，就不能把另一消息视图的估算直接传回来。本例是显式准备入口，是否因阈值触发、在哪里生成和提交，Day 11 再接回 AgentSession。

## 像调试器一样核对范围

取得 ready 后，按 entry_ids 展开 history、turn_prefix 和 retained_tail：三段应按原顺序拼回 candidates；previous_summary 单独存在，摘要占位项不在这三段里。每条消息都能说明是较早历史、当前任务前缀还是保留尾部，不应依赖“看起来差不多长”。

普通多任务历史中，切点恰好落在 HumanMessage 时 turn_prefix 为空；长单任务中，history 可以为空而 turn_prefix 非空。检查的是用户任务关系，不能用模型请求次数代替。

有上一条 compaction 时，从 Day 9 展开的尾部与新增消息继续选择，核对旧摘要仅作为 previous_summary 传递。上次压缩后没有新增可投影消息时直接 unchanged，避免同一份摘要反复压缩。

文件信息只取 history/prefix 与旧摘要元数据。尾部仍保留原消息，后续模型可以直接读取其中的文件信息；不提前把尾部也当作已经总结过的范围。

## 怎样核对

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
uv run deta --help
```

完成实际源码后再运行这些命令。静态检查通过与真实运行、故障恢复、摘要质量的验证分别记录。

在实际累积的 Session 上调用上面的准备函数，记录 status、切点来源、三段条目 ID、tokens_before、retained_tokens 与 keep_recent_tokens。用同一快照做解释性检查，不新增持久化条目，也不创建模型调用 Span。

普通多任务、单个很长任务、已有摘要是三个需要最终覆盖的来源形态。前两类可使用已经完成的真实任务；第三类等待 Day 11 产生有效摘要后再核对。没有相应记录时标记尚未验证，不用伪造 payload 代替真实恢复与压缩链路。

对空记录、工具尚未结清、刚完成压缩及极长单项，核对返回状态或边界说明。若得到 no_range，保留原会话并交回调用方；不能通过删除工具结果或悄悄截正文获得 ready。

## Pi 对照与下一步

| Pi 位置或语义 | Deta 对应 |
| --- | --- |
| `packages/agent/src/harness/compaction/compaction.ts` 的 findValidCutPoints / findCutPoint | 从合法消息起点中选择保留尾部，不从 ToolMessage 开始 |
| findTurnStartIndex | 向前找真实用户任务起点，分开 history 与 turn_prefix |
| prepareCompaction | 展开当前上下文后构造纯准备对象，不直接改写会话 |
| `compaction/utils.ts` 文件操作信息 | 提取工具请求提及路径，保留结果不确定性 |

这里沿用 Pi 的关键范围语义，数据结构按 Deta 的单路径 Session 简化。预算是启发式估算，切点选择不等于已经降低真实模型用量。

下一步是 [Day 11：Compaction 生成、提交与续接](day11.md)：分别处理首次摘要、已有摘要更新和任务前缀摘要；成功后核对 snapshot_tip_id，再保存 Compaction Entry、重建 Context 并继续。生成失败、取消或提交失败时，今天的准备对象不能被误当作已完成压缩。
