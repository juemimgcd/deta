from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from deta.builtin_tools import ToolOutput
from deta.builtin_tools._files import (
    TextDocument,
    atomic_write,
    change_output,
    encode_document,
    load_document,
    make_diff,
    normalize,
    resolve_target,
)


class WriteArgs(BaseModel):
    """声明 write 的路径与完整内容；本工具明确执行创建或覆盖。"""

    # 拒绝未声明字段与宽松类型转换。
    model_config = ConfigDict(extra="forbid", strict=True)
    # 目标文件路径，相对工作目录解析。
    path: str = Field(min_length=1, description="工作目录内的目标文件")
    # 写入后的完整正文，空字符串表示写入空文件。
    content: str = Field(description="完整文件正文")


def write_file(args: WriteArgs, workspace: Path) -> ToolOutput:
    """校验目标、输出大小和 diff 后一次性创建或覆盖文件，返回修改摘要。"""
    target = resolve_target(workspace, args.path)
    document = load_document(target, allow_missing=True)
    text = args.content.removeprefix("\ufeff")
    if document.mode is None:
        # 新文件以 LF 为规范换行，是否含 BOM 由传入内容显式决定。
        document = TextDocument(b"", "", "\n", args.content.startswith("\ufeff"), None)
    text = normalize(text)
    raw = encode_document(text, document)
    diff = make_diff(args.path, document.text, text)
    atomic_write(target, raw, document.mode)
    return change_output(document.raw, raw, diff, "write 已完成创建或覆盖")
