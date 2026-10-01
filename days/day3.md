# Day 3：一次接入四个现成工具

[总览](summary.md) · [前一天](day2.md) · [下一天](day4.md)

## 核心问题

Agent 最终需要 read、write、edit、bash 四个工具。先把完整工具包准备好，再围绕固定接口写调度与 Loop，就不必为了增加一个工具而回头修改已经写好的循环。

本章一次提供四个工具、共享文件操作和 ToolContext/ToolOutput。直接复制全部六个文件；文件编辑、输出截断和进程清理内部算法按需阅读。本章没有工具实现 TODO。

## 今天新增什么

| 文件 | 直接提供的内容 |
| --- | --- |
| `builtin_tools/__init__.py` | ToolContext 与 ToolOutput，包括输出回调和 terminate 提示 |
| `builtin_tools/read.py` | ReadArgs、分页读取、路径检查与字节限制 |
| `builtin_tools/_files.py` | 文件表示、diff、路径校验与原子写入 |
| `builtin_tools/write.py` | 创建或覆盖完整文本 |
| `builtin_tools/edit.py` | 基于同一原文的唯一匹配、批量替换 |
| `builtin_tools/bash.py` | 命令执行、stdout/stderr 日志、超时与进程组清理 |

Day 4 再提供完整工具表与执行器，统一注册这四个入口。Day 5–7 直接使用已经固定的工具接口。

## 先记住四个入口

| 工具 | 参数 | 业务入口与返回值 |
| --- | --- | --- |
| read | path、offset、limit | `read_file(args, workspace) → str` |
| write | path、content | `write_file(args, workspace) → ToolOutput` |
| edit | path、edits 中的 old_text/new_text | `edit_file(args, workspace) → ToolOutput` |
| bash | command、timeout_seconds | `await run_bash(args, context) → ToolOutput` |

前三个是同步文件函数，bash 是异步函数。Day 4 在注册时用同一个 async_file_handler 包装同步函数，调度器统一调用 `await handler(args, context)`，统一得到 ToolOutput。业务函数保留各自适合的实现形式，Loop 无需判断同步还是异步。

ToolContext 显式携带 workspace、output_dir、shell、environment 和 on_output。ToolOutput 携带 content、error_code、details 与 terminate；`tool_call_id` 由 Day 4 执行器从原调用补入 ToolMessage。工具不请求模型、不提交对话历史，也不决定下一轮何时开始。

## 使用前需要理解的边界

### read


- 相对路径以显式 workspace 为基准；绝对路径必须仍在该目录内。先 resolve 再判断范围，静态符号链接不能指向目录外。
- 仅支持普通 UTF-8 文本文件，拒绝 NUL 和无法解码的内容；空文件返回清晰说明。
- offset 从 1 开始，limit 表示最大返回行数；输出加行号，默认最多 200 行，总输出预算 32 KiB。
- 读取用有上限的 readline，不把整个大文件一次读进内存。为头部与分页提示预留 1024 字节。
- 按完整行截断，返回下一段 offset。单行过长不能用“继续同一行”假装前进，明确返回 read_failed；单行分段和更多编码不属于今天范围。
- 超过文件末尾返回失败；正常分页用最后一个实际输出行号加一继续。只增加 offset，不改变文件。

路径范围校验是工具行为规则，不是进程安全沙箱；本日不承诺抵抗其他进程在检查和打开之间替换路径。读取从文件头顺序扫描到 offset，且跳过区间遇到超长行也会失败，这是首版的明确限制。

### write 与 edit

write 用完整正文创建或覆盖文件；edit 对现有文件做精确替换。一次 edit 的全部 old_text 都匹配调用开始时的同一份原文，必须唯一且范围不重叠；全部校验通过后才写回。替换 A 产生的新内容不能充当替换 B 的匹配依据。


已有文件保留统一的 LF、CRLF 或 CR 换行风格和 UTF-8 BOM，权限位在替换时恢复。混合换行、非 UTF-8、含 NUL 或超过 1 MiB 的文件在修改前拒绝；这是首版有意限定的文本范围。新文件按 LF 写入，BOM 是否存在由传入正文决定，权限沿用临时文件默认的私有权限。

diff 最多 24 KiB。超过时拒绝本次修改，让模型缩小任务，而不是已经写完才给一个不完整的 diff。工具结果包含修改前后 SHA-256；文本完全相同时可能没有 diff，是否有字节变化应同时看 details。

同目录 os.replace 防止把半截文件暴露为目标内容，但本页不承诺抵抗其他进程同时改写、保留所有扩展属性、硬链接关系或提供断电恢复。父目录按需创建，文件变更仍使用当前进程权限。

### bash


每个通道的内存尾部最多 12 KiB，完整采集内容写到单独文件。stdout/stderr 分别保存，事件中带通道名；两个通道之间没有可靠的全局字节顺序保证。

单次命令的总落盘预算为 16 MiB，超过预算时终止，并写明 capture_complete=False。成功读取至 EOF 且没有输出预算/超时中断时才标记采集完整；非零退出可以有完整输出，但仍是 bash_failed。

实时显示采用 UTF-8 增量解码，避免一个汉字跨 chunk 时被拆坏；尾部按字节截取，显示时允许用替换字符表示截断边缘。原始字节文件才是该次采集内容的依据。

这些输出文件是命令工具的运行产物，即使 `--capture-body` 关闭也会创建；该开关只控制请求/工具 JSON 诊断正文。输出文件可能包含命令输出的原文，不应把它们误称为经过正文脱敏的 Trace 快照。

shell 和 cwd 分别传入，不把工作目录拼到命令正文里。本机默认使用 macOS 与 `/bin/zsh`；调用方显式传环境变量。路径限制和进程组清理不构成操作系统沙箱。

## 直接提供的完整工具包

按下面顺序放入文件。ToolContext 和 ToolOutput 先于使用它们的工具出现；本章结束时四个业务工具都已齐备。

### src/deta/builtin_tools/__init__.py

<details>
<summary>直接提供：src/deta/builtin_tools/__init__.py（完整文件）</summary>

```python
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue


@dataclass(frozen=True)
class ToolOutput:
    """保存工具函数的业务输出，由调度器补上调用 ID 形成 ToolMessage。

    文件工具可以提供修改摘要和结构化细节，后续 bash 也复用这份输出契约。
    """

    # 要回传模型的有界文本，包含操作摘要和必要的 diff。
    content: str
    # 可预期的工具失败码；None 表示成功。
    error_code: str | None = None
    # 用于诊断的 JSON 数据，例如内容摘要、文件字节数或输出文件引用。
    details: dict[str, JsonValue] = field(default_factory=dict)
    # 本工具请求停止自然续轮的提示；批次聚合规则由 Loop 执行。
    terminate: bool = False


@dataclass(frozen=True)
class ToolContext:
    """给所有工具传递执行环境与增量输出回调，避免工具自行读取全局配置。"""

    # shell 的当前工作目录，与文件工具的路径基准一致。
    workspace: Path
    # stdout/stderr 原始字节文件的存放目录。
    output_dir: Path
    # 要启动的 shell 可执行文件，本机示例显式使用 /bin/zsh。
    shell: str
    # 显式传递的环境变量快照，不自动记录到 Trace。
    environment: Mapping[str, str]
    # 接收带通道标记的输出增量，交给 Loop 发布工具更新事件。
    on_output: Callable[[str], None]
```

</details>

### src/deta/builtin_tools/read.py

<details>
<summary>直接提供：src/deta/builtin_tools/read.py（完整文件）</summary>

```python
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

MAX_OUTPUT_BYTES = 32 * 1024
MAX_LINE_BYTES = MAX_OUTPUT_BYTES + 1


class ReadArgs(BaseModel):
    """定义 read 工具接受的参数，同时提供模型可见的 schema 和本地校验规则。
    调度器将结构化参数字典 校验为该对象后，才传给 read_file。
    """

    # 拒绝多余参数并使用严格类型校验，避免把字符串行号自动转换为整数。
    model_config = ConfigDict(extra="forbid", strict=True)
    # 要读取的路径；相对路径按 workspace 解析，绝对路径也必须位于工作目录内。
    path: str = Field(min_length=1, description="相对工作目录或目录内的绝对文件路径")
    # 读取的起始行号，从 1 开始；默认从文件第一行读取。
    offset: int = Field(default=1, ge=1, description="起始行，从 1 开始")
    # 本次最多返回的完整行数；默认 200 行，允许范围为 1 到 2000。
    limit: int = Field(default=200, ge=1, le=2000, description="最多返回的完整行数")


class ReadError(Exception):
    """表示 read 工具能够解释给调用方的文件读取失败。
    执行器将它转换成 read_failed 结果；本类没有额外属性，父类保存错误说明。
    """


def read_file(args: ReadArgs, workspace: Path) -> str:
    """接收已验证的 ReadArgs 和工作目录，检查路径后按行与字节预算读取 UTF-8 文本。
    把带行号、结束或续读提示的字符串返回给执行器；无法读取时抛出相应异常。
    """
    root = workspace.resolve(strict=True)
    path = (root / args.path).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ReadError("路径超出工作目录")
    if not path.is_file():
        raise ReadError("目标不是普通文件")
    lines: list[str] = []
    size = 0
    line_number = 0
    next_offset: int | None = None
    with path.open("rb") as source:
        # 限制每次 readline 的字节数，避免单独一行过大而占满内存。
        while True:
            raw = source.readline(MAX_LINE_BYTES)
            if not raw:
                break
            line_number += 1
            if len(raw) >= MAX_LINE_BYTES:
                raise ReadError(
                    f"第 {line_number} 行超过字节限制；本工具不支持单行分段"
                )
            if line_number < args.offset:
                continue
            if len(lines) >= args.limit:
                next_offset = line_number
                break
            if b"\x00" in raw:
                raise ReadError("检测到 NUL 字节，只支持 UTF-8 文本")
            text = raw.decode("utf-8")
            rendered = f"{line_number}: {text.rstrip(chr(10)).rstrip(chr(13))}\n"
            # 为行范围标题和下一段读取提示保留输出空间。
            if size + len(rendered.encode("utf-8")) > MAX_OUTPUT_BYTES - 1024:
                if not lines:
                    raise ReadError(
                        f"第 {line_number} 行无法完整放入输出；不支持单行分段"
                    )
                next_offset = line_number
                break
            lines.append(rendered)
            size += len(rendered.encode("utf-8"))
    if not lines:
        if line_number == 0 and args.offset == 1:
            return "[空文件]"
        raise ReadError(f"offset={args.offset} 超出文件末尾（共 {line_number} 行）")
    last = args.offset + len(lines) - 1
    tail = (
        f"[输出已截断；继续读取 offset={next_offset}]"
        if next_offset is not None
        else "[已到文件末尾]"
    )
    return f"[行 {args.offset}–{last}]\n{''.join(lines)}{tail}"
```

</details>

### src/deta/builtin_tools/_files.py

<details>
<summary>直接提供：src/deta/builtin_tools/_files.py（完整文件）</summary>

```python
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
```

</details>

### src/deta/builtin_tools/write.py

<details>
<summary>直接提供：src/deta/builtin_tools/write.py（完整文件）</summary>

```python
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
```

</details>

### src/deta/builtin_tools/edit.py

<details>
<summary>直接提供：src/deta/builtin_tools/edit.py（完整文件）</summary>

```python
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
```

</details>

### src/deta/builtin_tools/bash.py

<details>
<summary>直接提供：src/deta/builtin_tools/bash.py（完整文件）</summary>

```python
import asyncio
import codecs
import logging
import os
import signal
from typing import BinaryIO
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from deta.builtin_tools import ToolContext, ToolOutput

logger = logging.getLogger(__name__)
# 每个通道只在内存和工具回复中保留末尾 12 KiB。
TAIL_BYTES = 12 * 1024
# 一次命令最多落盘 16 MiB；超过就终止进程组，并明确标记输出未完整采集。
CAPTURE_BYTES = 16 * 1024 * 1024


class BashArgs(BaseModel):
    """定义交给指定 shell 的命令与单次执行超时，工作目录来自 ToolContext。"""

    # 命令和时间参数使用严格校验。
    model_config = ConfigDict(extra="forbid", strict=True)
    # 原样交给 shell -c 的命令字符串，不再拼接路径或引号。
    command: str = Field(min_length=1, description="要执行的 shell 命令")
    # 命令等待上限；不包含必须完成的进程清理时间。
    timeout_seconds: float = Field(default=30, gt=0, le=120)


class CaptureLimitError(Exception):
    """表示命令输出达到本次磁盘采集预算，转为 output_limit 工具结果。"""


class OutputCaptureError(Exception):
    """表示输出文件或进程清理的内部故障，必须让 Run 失败而非伪装成命令失败。"""


async def terminate_group(process: asyncio.subprocess.Process) -> None:
    """终止独立进程组，再等待直接子进程退出；即使 shell 已退出也处理组内子进程。"""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        await asyncio.sleep(0.25)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError as exc:
        raise OutputCaptureError("无法在清理期限内确认子进程退出") from exc


async def run_bash(args: BashArgs, context: ToolContext) -> ToolOutput:
    """执行命令、并发读取两个输出通道、保存有界产物，并在所有出口清理进程组。

    普通非零退出和工具超时返回 ToolOutput；外部取消完成清理后继续向上传播。
    """
    token = uuid4().hex
    stdout_path = context.output_dir / f"{token}.stdout.log"
    stderr_path = context.output_dir / f"{token}.stderr.log"
    try:
        context.output_dir.mkdir(parents=True, exist_ok=True)
        stdout_file = stdout_path.open("xb")
        try:
            stderr_file = stderr_path.open("xb")
        except BaseException:
            stdout_file.close()
            raise
    except OSError as exc:
        raise OutputCaptureError("无法创建命令输出文件") from exc
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    captured = 0

    async def pump(
        reader: asyncio.StreamReader, output: BinaryIO, channel: str
    ) -> None:
        """读取一个通道，保存原始字节并发布解码增量；内存中仅保留有界尾部。"""
        nonlocal captured
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                rest = decoder.decode(b"", final=True)
                if rest:
                    context.on_output(f"[{channel}] {rest}")
                return
            exceeded = captured + len(chunk) > CAPTURE_BYTES
            chunk = chunk[: max(0, CAPTURE_BYTES - captured)]
            try:
                output.write(chunk)
            except OSError as exc:
                raise OutputCaptureError("写入命令输出失败") from exc
            captured += len(chunk)
            tails[channel].extend(chunk)
            del tails[channel][:-TAIL_BYTES]
            delta = decoder.decode(chunk)
            if delta:
                context.on_output(f"[{channel}] {delta}")
            if exceeded:
                raise CaptureLimitError

    process: asyncio.subprocess.Process | None = None
    jobs: list[asyncio.Task[None]] = []
    failure: str | None = None
    primary: BaseException | None = None
    try:
        try:
            spawning = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    context.shell,
                    "-c",
                    args.command,
                    cwd=context.workspace,
                    env=dict(context.environment),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            )
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                # 取得已经启动的进程句柄后再传播取消，确保 finally 能清理它。
                try:
                    process = await spawning
                except OSError:
                    pass
                raise
        except OSError as exc:
            return ToolOutput(f"启动命令失败：{type(exc).__name__}", "bash_failed")
        if process.stdout is None or process.stderr is None:
            raise OutputCaptureError("子进程未提供预期管道")
        jobs = [
            asyncio.create_task(pump(process.stdout, stdout_file, "stdout")),
            asyncio.create_task(pump(process.stderr, stderr_file, "stderr")),
        ]
        try:
            async with asyncio.timeout(args.timeout_seconds):
                await asyncio.gather(*jobs)
                await process.wait()
        except TimeoutError:
            failure = "bash_timeout"
        except CaptureLimitError:
            failure = "output_limit"
    except BaseException as exc:
        primary = exc
        raise
    finally:

        async def cleanup() -> None:
            """在后台清理进程组与输出任务，最后关闭文件，避免留下仍在执行的命令。"""
            try:
                if process is not None:
                    await terminate_group(process)
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                try:
                    stdout_file.close()
                finally:
                    stderr_file.close()

        cleaning = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleaning)
        except asyncio.CancelledError:
            await cleaning
            raise
        except Exception as exc:
            if primary is None:
                raise OutputCaptureError("命令清理或文件关闭失败") from exc
            logger.warning("cleanup failed while unwinding: %s", type(exc).__name__)
    if process is None:
        raise OutputCaptureError("进程未创建")
    code = failure or ("bash_failed" if process.returncode != 0 else None)
    stdout = tails["stdout"].decode("utf-8", errors="replace")
    stderr = tails["stderr"].decode("utf-8", errors="replace")
    complete = failure is None
    return ToolOutput(
        content=(
            f"exit_code={process.returncode}; outcome={code or 'success'}\n"
            f"stdout（末尾最多 {TAIL_BYTES} 字节）:\n{stdout}\n"
            f"stderr（末尾最多 {TAIL_BYTES} 字节）:\n{stderr}\n"
            f"输出文件：{stdout_path}；{stderr_path}\n"
            f"capture_complete={complete}"
        ),
        error_code=code,
        details={
            "exit_code": process.returncode,
            "pid": process.pid,
            "pgid": process.pid,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "captured_bytes": captured,
            "capture_complete": complete,
        },
    )
```

</details>

## edit 内部算法（选读）

<details>
<summary>展开查看唯一匹配、范围检查与写回顺序</summary>

1. 参数 JSON 先经 EditArgs 校验，得到包含多个 Replacement 的对象；额外字段或空 old_text 此时就失败。
2. load_document 返回原始字节和规范文本。apply_replacements 只操作这个原文，不读写文件。
3. `find(old, start + 1)` 检查第二次出现，包含相互重叠的重复出现。例如原文 aaa 中的 aa 不是唯一匹配，不能用非重叠计数误判。
4. 每项形成 `(start, end, new_text)`。排序后若前一项 end 大于后一项 start，就是范围冲突；相等则只是相邻。
5. 从末尾向前拼接，较后位置长度的变化不会移动尚未处理的前面位置。
6. encode_document 与 make_diff 都成功后才写文件。任何一项缺失、重复或重叠都没有到达写入步骤。
7. 调度器保存最终结果，模型在下一轮看到修改摘要和 diff；终端报告和 details 必须与实际目标文件核对。

</details>

## 进程与取消的实现细节（选读）

这一节供排查工具实现问题时查阅，不作为本阶段手写练习。

<details>
<summary>展开查看进程创建、输出采集与取消清理</summary>

### 1. 创建阶段也需要取得句柄

子进程创建用独立 spawning 任务并以 shield 等待。若取消恰好发生在进程已创建、await 还未返回之间，仍先取得句柄，再把取消向外抛出，保证 finally 能找到进程组。

### 2. 两个通道同时读取

stdout 和 stderr 各有一个 pump 任务，防止其中一条管道写满让命令卡住。每个 read 最多 4096 字节；落盘计数在事件循环中同步更新，所以两个 pump 共用同一份预算。

工具回复只含尾部及日志路径，不能把整个长输出塞进下一次模型请求。模型若还需更早内容，可以再调用 read 按行查看日志；极长单行仍受 Day 3 的读取边界限制。

### 3. 超时与外部取消分开

命令自己的 timeout_seconds 到期时，记录 bash_timeout，清理后返回可配对结果，模型可以解释或修正。用户 abort 或 Run 总时限打断等待时，取消穿过工具层和 Loop，工具不会伪造一条普通成功/失败来继续运行；Agent 分别归一为 cancelled 或 limited。

asyncio.timeout 不能杀死进程，因此无论什么出口都调用 terminate_group。即使 shell 已先退出，也继续处理仍在同一进程组的子进程。自行脱离该进程组的后台进程不在这一保证内，本页不是操作系统沙箱。

### 4. 文件线程也必须收尾

Day 4 提供的 async_file_handler 会让 read/write/edit 的同步函数运行在线程中。取消时不能把线程当成已经停止；async_file_handler 的包装函数等待它结束后再传播取消。若文件修改已经发生而最终结果尚未提交，历史可能保留结果未知状态，下一次运行不能自动重放。

因此取消的响应时间可以超过超时值：超时值限制工作等待，之后还有必要清理。Day 7 的 Agent.running 会在任务、线程/进程收尾和 run_end 通知完成前保持 True。

### 5. 错误不能互相伪装

非零退出是 bash_failed；命令超时是 bash_timeout；输出预算是 output_limit。磁盘写入或文件关闭等采集故障是 OutputCaptureError，直接使 Run 失败；不能报告成“检查命令正常返回一个错误码”。清理又失败时保留已有的主要异常，并单独记录清理失败类型。

</details>

## 怎样核对

将本章代码写入实际源码后，再运行：

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
```

静态检查、真实工具调用和真实模型运行分别记录；本文中的代码与调用示例不代表已经完成运行验收。

先用真实已有文件确认工具包可独立使用：

```bash
uv run python -c 'from pathlib import Path; from deta.builtin_tools.read import ReadArgs, read_file; print(read_file(ReadArgs(path="target.md", offset=1, limit=20), Path.cwd()))'
```

按实际输出的 offset 继续读取，核对行号、内容和分页提示。write/edit 只在明确用于练习的工作区调用，并核对目标文件、diff 和 before/after 哈希；bash 使用项目已有检查命令，核对 cwd、退出码与两份日志。

这些属于真实工具的独立使用。由模型选择工具并根据结果继续回答，要等 Day 7 主 Loop 接通后验收。没有适合的长输出、特殊编码或取消场景时记录未验证，不额外创建 fixture 或模拟响应。

## Pi 对照与下一阶段

| Pi 职责 | Deta 对应 |
| --- | --- |
| `harness/tools/read.ts` | read.py 的行号、分页与输出限制 |
| `harness/tools/write.ts`、`edit.ts` | write.py、edit.py 与 _files.py |
| `harness/tools/bash.ts` | bash.py 的执行环境、输出和清理 |

下一阶段 [Day 4：固定工具调度与运行契约](day4.md) 给出完整 tools.py，以及后续运行层共用的类型和回调签名。
