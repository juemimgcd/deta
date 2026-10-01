import difflib
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from deta.builtin_tools import ToolOutput

# 文件修改首版限定为 1 MiB 的 UTF-8 文本，避免无界读入和生成巨大 diff。
MAX_FILE_BYTES = 1024 * 1024
# 工具回传中的 diff 最多 24 KiB；超限在落盘前拒绝本次修改。
MAX_DIFF_BYTES = 24 * 1024


class MutationError(Exception):
    """表示可以向模型解释的修改前校验失败，由调度器转成工具错误结果。"""


@dataclass(frozen=True)
class TextDocument:
    """保存原始字节与用于匹配的规范文本，让写回时恢复原文件表示。"""

    # 原始文件字节，用于内容摘要和变更比较。
    raw: bytes
    # 去除 UTF-8 BOM 并将换行统一为 LF 后的文本。
    text: str
    # 原文件换行风格：LF、CRLF 或 CR；无换行时使用 LF。
    newline: str
    # 原文件是否带 UTF-8 BOM，写回时保留。
    bom: bool
    # 原文件权限；新文件为 None，保留临时文件创建时的私有权限。
    mode: int | None


def normalize(text: str) -> str:
    """把字符串中的 CRLF 和 CR 统一成 LF，供原文件匹配和新内容组装。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def resolve_target(workspace: Path, name: str) -> Path:
    """解析将要修改的真实路径，允许目标尚不存在，但要求处于工作目录内。"""
    root = workspace.resolve(strict=True)
    target = (root / name).resolve(strict=False)
    if not target.is_relative_to(root) or target == root:
        raise MutationError("目标路径必须是工作目录内的文件")
    return target


def load_document(path: Path, *, allow_missing: bool = False) -> TextDocument:
    """读取有大小上限的原始文本，识别 BOM、换行和权限，返回修改前快照。"""
    if not path.exists() and allow_missing:
        return TextDocument(b"", "", "\n", False, None)
    if not path.is_file():
        raise MutationError("目标不存在或不是普通文件")
    with path.open("rb") as source:
        raw = source.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise MutationError("文件超过首版 1 MiB 限制")
    if b"\x00" in raw:
        raise MutationError("只支持不含 NUL 的 UTF-8 文本")
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    endings = set(re.findall(r"\r\n|\r|\n", text))
    if len(endings) > 1:
        raise MutationError("原文件混用多种换行，请先明确如何处理")
    newline = next(iter(endings), "\n")
    return TextDocument(
        raw, normalize(text), newline, bom, stat.S_IMODE(path.stat().st_mode)
    )


def encode_document(text: str, document: TextDocument) -> bytes:
    """把规范文本恢复为原换行与 BOM，校验写回字节数后交给原子写入函数。"""
    if "\x00" in text:
        raise MutationError("新内容不能包含 NUL")
    restored = text.replace("\n", document.newline)
    raw = ("\ufeff" if document.bom else "").encode("utf-8") + restored.encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise MutationError("修改后的文件超过首版 1 MiB 限制")
    return raw


def make_diff(name: str, before: str, after: str) -> str:
    """生成统一 diff，并在文件末行无换行时显式标记；超限时拒绝修改。"""
    parts: list[str] = []
    for line in difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
    ):
        parts.append(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        )
    diff = "".join(parts)
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        raise MutationError("diff 超过 24 KiB，请把任务拆成较小修改")
    return diff


def atomic_write(path: Path, content: bytes, mode: int | None) -> None:
    """在同一目录写临时文件，完整刷盘后替换目标，异常时清理临时文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def change_output(
    before: bytes, after: bytes, diff: str, description: str
) -> ToolOutput:
    """把已经完成的修改包装成有界摘要与哈希细节，交给工具调度器记录。"""
    return ToolOutput(
        content=f"{description}\n{diff or '[没有文本差异]'}",
        details={
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "bytes_written": len(after),
            "changed": before != after,
        },
    )
