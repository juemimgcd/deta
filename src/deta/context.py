import hashlib
import json
from collections.abc import Sequence
from typing import Literal

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    convert_to_openai_messages,
)
from pydantic import Field

from deta.session import MESSAGE, Entry
from deta.types import AgentMessage, Data, ToolSchema


class ContextItem(Data):
    """把一条内部消息与它的来源放在一起，供请求解释和保留尾部使用。"""

    # 即将交给模型转换层的内部消息，不是数据库中的可变历史容器。
    message: AgentMessage
    # 来源条目 ID；Hook 新增或改写时为空，保留原项时携带原始 ID。
    entry_ids: tuple[str, ...]
    # 区分普通消息、自定义备注、摘要占位消息和 Hook 产生的内容。
    source: Literal["message", "custom", "summary", "hook"]
    # 解释来源不确定或发生转换的原因。
    note: str = ""


class CompactionRecord(Data):
    """保存摘要及原条目引用，供后续请求还原保留尾部。"""

    # 用于替代较早上下文的摘要正文。
    summary: str = Field(min_length=1)
    # 摘要后仍需保留的原始条目 ID，按会话顺序保存；消息正文只存于 Entry。
    retained_entry_ids: tuple[str, ...]
    # 上次压缩前记录的输入规模估算，不是摘要请求的实际用量。
    tokens_before: int = Field(ge=0)
    # read 请求涉及的文件，不单凭调用声明认定读取已经成功。
    read_files: tuple[str, ...] = ()
    # write/edit 请求涉及的文件，是否实际修改仍需结合结果和磁盘核对。
    modified_files: tuple[str, ...] = ()
    # 已知调用失败或结果未知、需要继续核对的文件。
    uncertain_files: tuple[str, ...] = ()


class ContextView(Data):
    """保存从一个会话快照投影的模型上下文；构建它不改变原始 Entry。"""

    # 有顺序的消息及来源，摘要至多出现一次。
    items: tuple[ContextItem, ...]
    # 本次构建所读的最后一个条目 ID，后续提交摘要时用于检查快照是否过期。
    tip_id: str | None
    # 最近一次压缩的条目 ID，没有历史压缩时为 None。
    compaction_id: str | None
    # 最近压缩的结构化内容，准备增量摘要时复用其中的摘要与文件信息。
    compaction: CompactionRecord | None
    # 未进入视图的条目及原因，记录仍留在 Session。
    excluded: tuple[tuple[str, str], ...]
    # 最近压缩之后新增的可投影消息数量，用于识别没有新内容的重复准备。
    new_messages: int

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        """按视图顺序返回消息，交给请求准备层；来源仍由 items 保留。"""
        return tuple(item.message for item in self.items)


class ContextEstimate(Data):
    """区分提供方已报告用量与启发式补估，用于决定输入是否接近预算。"""

    # 当前完整输入的估算 token 数。
    tokens: int
    # 可复用的历史 usage；None 表示没有匹配当前输入前缀的报告值。
    reported_tokens: int | None
    # usage 之后新增内容的估算；没有有效 usage 时为完整输入估算。
    estimated_tokens: int
    # 提供有效 usage 的助手消息位置；None 表示完全依靠估算。
    anchor_index: int | None
    # 模型窗口扣除本次输出预留与误差余量后的输入上限。
    input_limit: int
    # 当前消息视图的摘要，防止压缩准备误用另一个视图的估算。
    messages_hash: str

    @property
    def needs_compaction(self) -> bool:
        """比较输入估算与可用额度；这里只给出判断，不调用摘要或删历史。"""
        return self.tokens > self.input_limit


def project_entry(entry: Entry) -> ContextItem | None:
    """投影普通消息或明确支持的 context_note；其他条目保留在记录中但不发给模型。"""
    if entry.kind == "message":
        return ContextItem(
            message=MESSAGE.validate_python(entry.payload).model_copy(deep=True),
            entry_ids=(entry.id,),
            source="message",
        )
    if entry.kind == "custom" and entry.payload.get("type") == "context_note":
        text = entry.payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("context_note 必须包含有效 text")
        return ContextItem(
            message=HumanMessage(content=f"会话备注（历史参考）：\n{text}"),
            entry_ids=(entry.id,),
            source="custom",
        )
    return None


def resolve_retained_tail(
    record: CompactionRecord, entries: Sequence[Entry]
) -> tuple[ContextItem, ...]:
    """按保存的 ID 从原始条目还原尾部，拒绝缺失、重复、乱序和不支持的条目。"""
    earlier = {entry.id: entry for entry in entries}
    result: list[ContextItem] = []
    last_sequence = 0
    for entry_id in record.retained_entry_ids:
        entry = earlier.get(entry_id)
        if entry is None or entry.seq <= last_sequence:
            raise ValueError("保留尾部引用缺失、重复或乱序")
        item = project_entry(entry)
        if item is None:
            raise ValueError("保留尾部只能引用消息或支持的备注")
        result.append(item)
        last_sequence = entry.seq
    return tuple(result)


def build_context(entries: Sequence[Entry]) -> ContextView:
    """展开最近摘要、其保留尾部和之后新增的消息；每份内容只加入一次。"""
    ids = [entry.id for entry in entries]
    sequence = [entry.seq for entry in entries]
    if len(ids) != len(set(ids)) or any(a >= b for a, b in zip(sequence, sequence[1:])):
        raise ValueError("会话快照的条目 ID 或顺序无效")
    index = next(
        (i for i in range(len(entries) - 1, -1, -1) if entries[i].kind == "compaction"),
        -1,
    )
    items: list[ContextItem] = []
    excluded: list[tuple[str, str]] = []
    record: CompactionRecord | None = None
    compaction_id: str | None = None
    if index >= 0:
        entry = entries[index]
        compaction_id = entry.id
        record = CompactionRecord.model_validate(entry.payload)
        tail = resolve_retained_tail(record, entries[:index])
        retained_ids = set(record.retained_entry_ids)
        items.append(
            ContextItem(
                message=HumanMessage(
                    content=f"此前会话摘要（历史参考）：\n{record.summary}"
                ),
                entry_ids=(entry.id,),
                source="summary",
            )
        )
        items.extend(tail)
        excluded.extend(
            (item.id, f"位于压缩 {entry.id} 之前且不在保留尾部")
            for item in entries[:index]
            if item.id not in retained_ids
        )
    new_messages = 0
    for entry in entries[index + 1 :]:
        projected = project_entry(entry)
        if projected is None:
            excluded.append((entry.id, f"未配置投影：{entry.kind}"))
        else:
            items.append(projected)
            new_messages += 1
    return ContextView(
        items=tuple(items),
        tip_id=entries[-1].id if entries else None,
        compaction_id=compaction_id,
        compaction=record,
        excluded=tuple(excluded),
        new_messages=new_messages,
    )


def validate_context_items(
    before: tuple[ContextItem, ...],
    items: tuple[ContextItem, ...],
    stage: str,
) -> tuple[tuple[ContextItem, ...], tuple[tuple[str, str], ...]]:
    """按显式来源核对 Hook 输出；新增或改写项必须标记 hook，不能沿用原条目身份。"""
    if not isinstance(items, tuple) or any(
        not isinstance(item, ContextItem)
        or not isinstance(item.message, (HumanMessage, AIMessage, ToolMessage))
        for item in items
    ):
        raise TypeError("请求转换必须返回 ContextItem 元组")
    originals = {item.entry_ids: item for item in before if item.entry_ids}
    retained: set[str] = set()
    owned: list[ContextItem] = []
    for item in items:
        if item.source == "hook":
            if item.entry_ids:
                raise ValueError("Hook 内容不能声明原始条目来源")
        elif not item.entry_ids or originals.get(item.entry_ids) != item:
            raise ValueError("改写内容必须创建 source=hook、entry_ids=() 的上下文项")
        retained.update(item.entry_ids)
        owned.append(item.model_copy(deep=True))
    missing = tuple(
        (entry_id, f"{stage} 移除了此条目")
        for item in before
        for entry_id in item.entry_ids
        if entry_id not in retained
    )
    return tuple(owned), missing


def messages_hash(messages: Sequence[AgentMessage]) -> str:
    """只对实际提供方可见的消息字段取摘要，忽略内部 usage、来源与诊断字段。"""
    body = convert_to_openai_messages(messages)
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def input_fingerprint(
    model: str,
    instructions: str,
    messages: Sequence[AgentMessage],
    tools: Sequence[ToolSchema],
) -> str:
    """标识真实请求前缀与配置，供后续请求判断历史 usage 是否仍适用。"""
    body = [model, instructions, list(tools), messages_hash(messages)]
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def estimate_message(message: AgentMessage) -> int:
    """按提供方可见 JSON 的 UTF-8 字节数粗估一条消息，供范围选择使用。"""
    body = json.dumps(convert_to_openai_messages([message])[0], ensure_ascii=False)
    return (len(body.encode()) + 2) // 3 + 8


def estimate_context(
    messages: Sequence[AgentMessage],
    *,
    model: str,
    instructions: str,
    tools: Sequence[ToolSchema],
    window_tokens: int,
    output_tokens: int,
    safety_tokens: int = 1024,
) -> ContextEstimate:
    """优先复用匹配前缀的最近 usage，再补估新增内容；不匹配时估算完整输入。"""
    limit = window_tokens - output_tokens - safety_tokens
    if output_tokens < 0 or safety_tokens < 0 or limit <= 0:
        raise ValueError("模型窗口必须大于输出预留与估算余量之和")
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if (
            not isinstance(message, AIMessage)
            or message.response_metadata.get("deta_request_fingerprint") is None
        ):
            continue
        if message.response_metadata.get(
            "deta_request_fingerprint"
        ) != input_fingerprint(model, instructions, messages[:index], tools):
            continue
        usage = message.usage_metadata
        reported = usage["total_tokens"] if usage is not None else None
        if reported is not None:
            trailing = sum(estimate_message(item) for item in messages[index + 1 :])
            return ContextEstimate(
                tokens=reported + trailing,
                reported_tokens=reported,
                estimated_tokens=trailing,
                anchor_index=index,
                input_limit=limit,
                messages_hash=messages_hash(messages),
            )
    overhead = instructions + json.dumps(list(tools), ensure_ascii=False)
    estimated = (
        sum(estimate_message(item) for item in messages)
        + (len(overhead.encode()) + 2) // 3
        + 16
    )
    return ContextEstimate(
        tokens=estimated,
        reported_tokens=None,
        estimated_tokens=estimated,
        anchor_index=None,
        input_limit=limit,
        messages_hash=messages_hash(messages),
    )
