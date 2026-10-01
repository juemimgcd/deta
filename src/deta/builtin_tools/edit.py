from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from deta.builtin_tools import ToolOutput
from deta.builtin_tools._files import (
    MutationError,
    atomic_write,
    change_output,
    encode_document,
    load_document,
    make_diff,
    normalize,
    resolve_target,
)


class Replacement(BaseModel):
    """表示一次精确替换，所有 old_text 都在同一份原文件中定位。"""

    # 参数字段和类型必须与声明一致。
    model_config = ConfigDict(extra="forbid", strict=True)
    # 必须非空且在原文件中仅出现一次的旧文本。
    old_text: str = Field(min_length=1, description="原文件中唯一匹配的文本")
    # 替换后的文本，空字符串表示删除匹配片段。
    new_text: str = Field(description="替换内容")


class EditArgs(BaseModel):
    """表示对一个现有文本文件的一批修改，验证全部通过后才写回。"""

    # 拒绝额外参数，数组元素由 Replacement 逐项校验。
    model_config = ConfigDict(extra="forbid", strict=True)
    # 已存在的目标文件路径。
    path: str = Field(min_length=1, description="要修改的文件")
    # 同次调用的替换列表，至少一项；各范围必须互不重叠。
    edits: list[Replacement] = Field(
        min_length=1, description="基于原文件的精确替换列表"
    )


def apply_replacements(original: str, edits: list[Replacement]) -> str:
    """先在原文定位全部唯一范围并检查重叠，再由后向前替换，返回新文本。"""
    ranges: list[tuple[int, int, str]] = []
    for edit in edits:
        old = normalize(edit.old_text)
        new = normalize(edit.new_text)
        start = original.find(old)
        if start < 0:
            raise MutationError("某项 old_text 在原文件中不存在")
        # 从 start + 1 继续查找，连相互重叠的重复出现也算不唯一。
        if original.find(old, start + 1) >= 0:
            raise MutationError("某项 old_text 在原文件中出现多次")
        ranges.append((start, start + len(old), new))
    ranges.sort()
    for left, right in zip(ranges, ranges[1:]):
        if left[1] > right[0]:
            raise MutationError("同次 edit 的修改范围重叠")
    updated = original
    for start, end, replacement in reversed(ranges):
        updated = updated[:start] + replacement + updated[end:]
    return updated


def edit_file(args: EditArgs, workspace: Path) -> ToolOutput:
    """读取原文件，完成全部匹配和输出检查后原子写回，并返回真实 diff。"""
    target = resolve_target(workspace, args.path)
    document = load_document(target)
    updated = apply_replacements(document.text, args.edits)
    raw = encode_document(updated, document)
    diff = make_diff(args.path, document.text, updated)
    if raw != document.raw:
        atomic_write(target, raw, document.mode)
    return change_output(
        document.raw, raw, diff, f"edit 已处理 {len(args.edits)} 处替换"
    )
