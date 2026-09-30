# Day 5：接入现成 write 与 edit 工具

[总览](summary.md) · [前一天](day4.md) · [下一天](day6.md)

## 核心问题

模型已经能读文件，怎样让它创建文件、精确修改文件，并给出与实际改动一致的 diff？尤其是一批替换里第二项不合法时，如何保证第一项没有先被写入？

本页沿用 Day 4 的同一个 Agent 和 Loop，直接提供 _files.py、write.py、edit.py 及配套接入补丁。无需手写文件写入、文本替换、diff 或原子落盘算法；学习重点是工具参数、成功/失败结果和模型根据结果继续工作的过程。代码仍需按文档放入源码后使用。

## 本阶段只做三件事

1. **接入工具。** 应用补丁、复制三个完整文件，确认 write/edit 的声明与执行使用同一张工具表。
2. **理解选择与参数。** write 用完整正文创建或覆盖；edit 用 old_text/new_text 精确替换。先用一次单处替换理解匹配规则，批量替换内部算法选读。
3. **走通结果。** 查看 ToolOutput 如何包装成带原调用 ID 的 ToolMessage；核对实际文件、diff 和错误说明，再观察模型如何继续修正。

## 今天新增什么

| 文件 | 本日职责 |
| --- | --- |
| `builtin_tools/_files.py` | 直接提供公共文件操作，内部实现选读 |
| `builtin_tools/write.py` | 直接提供完整创建/覆盖工具 |
| `builtin_tools/edit.py` | 直接提供完整精确替换工具 |
| `builtin_tools/__init__.py` | ToolOutput，供调度器构造带调用 ID 的结果 |
| `tools.py`、`types.py` | 显式注册新工具，保存结构化修改信息与错误分类 |

`_files.py` 是两个工具真正共用的文件操作职责，不增加通用存储接口或插件注册框架。Loop、Agent 和 CLI 的运行顺序保持沿用 Day 4 的实现。

## 先看一批修改的真实含义

```text
原文：A 区域 …… B 区域
请求：edits=[替换 A, 替换 B]
  → 两项都在原文定位
  → 检查每项唯一出现、范围互不重叠
  → 内存中由后向前替换
  → 校验最终文件大小并生成完整 diff
  → 同目录临时文件写完后 os.replace
  → ToolOutput → ToolMessage(call_id) → Loop 提交 → 模型继续
```

如果 A 的新内容刚好生成 B.old_text，那也不能给 B 作为匹配依据；B 只匹配调用开始时的原文件。两处相邻范围可以，交叠或包含关系必须拒绝。

## 先认识本日的类与函数

| 类 | 属性与作用 |
| --- | --- |
| `ToolOutput` | content 是有界回传文本；error_code 是业务错误；details 放摘要信息；调用 ID 由调度器补入 ToolMessage |
| `TextDocument` | raw 是原始字节；text 是 LF 规范文本；newline、bom、mode 用于恢复文件原有表示 |
| `WriteArgs` | path 是目标路径；content 是写入后的完整正文，允许空字符串 |
| `Replacement` | old_text 必须非空且唯一，new_text 可以为空以表示删除 |
| `EditArgs` | path 指向现有文件；edits 至少一项，同次全部基于原文 |
| `MutationError` | 可向模型解释的修改前拒绝，例如重复匹配、重叠或 diff 过大 |

| 函数 | 输入、职责与输出 |
| --- | --- |
| `write_file` | 校验参数和文件表示后创建/覆盖 → ToolOutput |
| `edit_file` | 串起读取、替换、编码、diff 与落盘 → ToolOutput |

这两个入口及其参数模型是接入重点。apply_replacements 和 _files.py 中的路径、编码、diff、原子写入函数属于现成工具内部；排查相应问题时再展开阅读。

## 文件表示与输出边界

已有文件保留统一的 LF、CRLF 或 CR 换行风格和 UTF-8 BOM，权限位在替换时恢复。混合换行、非 UTF-8、含 NUL 或超过 1 MiB 的文件在修改前拒绝；这是首版有意限定的文本范围。新文件按 LF 写入，BOM 是否存在由传入正文决定，权限沿用临时文件默认的私有权限。

diff 最多 24 KiB。超过时拒绝本次修改，让模型缩小任务，而不是已经写完才给一个不完整的 diff。工具结果包含修改前后 SHA-256；文本完全相同时可能没有 diff，是否有字节变化应同时看 details。

同目录 os.replace 防止把半截文件暴露为目标内容，但本页不承诺抵抗其他进程同时改写、保留所有扩展属性、硬链接关系或提供断电恢复。父目录按需创建，文件变更仍使用当前进程权限。

## 对已有契约的接入补丁

ToolSpec.handler 开始允许返回 `str | ToolOutput`：read 保留原返回字符串，write/edit 返回结构化输出。执行器统一包装并保留 details；ToolMessage 转给模型时仍只有文本和调用 ID，诊断字段通过产物引用查看。

下面直接提供相对前一阶段累计实现的完整接入补丁。`-` 行移除，`+` 行加入，其余行是定位上下文；不用把 diff 标记复制进 Python。先按补丁更新工具表与返回值适配，再复制下方三个完整文件；文件其余内容继续保留。本章不新增手写 TODO，重点阅读 execute_tool 怎样统一处理字符串与 ToolOutput。

<details>
<summary>接入补丁：src/deta/builtin_tools/__init__.py（相对 Day 4 完成状态）</summary>

```diff
--- a/src/deta/builtin_tools/__init__.py
+++ b/src/deta/builtin_tools/__init__.py
@@ -1 +1,18 @@
-"""Built-in tools are registered explicitly in deta.tools."""
+from dataclasses import dataclass, field
+
+from pydantic import JsonValue
+
+
+@dataclass(frozen=True)
+class ToolOutput:
+    """保存工具函数的业务输出，由调度器补上调用 ID 形成 ToolMessage。
+
+    文件工具可以提供修改摘要和结构化细节，后续 bash 也复用这份输出契约。
+    """
+
+    # 要回传模型的有界文本，包含操作摘要和必要的 diff。
+    content: str
+    # 可预期的工具失败码；None 表示成功。
+    error_code: str | None = None
+    # 用于诊断的 JSON 数据，例如内容摘要、文件字节数或输出文件引用。
+    details: dict[str, JsonValue] = field(default_factory=dict)
```

</details>

<details>
<summary>接入补丁：src/deta/types.py（相对 Day 4 完成状态）</summary>

此处无需新增消息字段；使用 LangChain 消息已有的 metadata 或 artifact。

</details>

<details>
<summary>接入补丁：src/deta/tools.py（相对 Day 4 完成状态）</summary>

```diff
--- a/src/deta/tools.py
+++ b/src/deta/tools.py
@@ -2,13 +2,17 @@
 from collections.abc import Callable, Mapping
 from dataclasses import dataclass
 from pathlib import Path
-from typing import Any, Literal
+from typing import Any

 from langchain_core.messages import ToolCall, ToolMessage
 from opentelemetry.trace import Status, StatusCode, Tracer
 from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

+from deta.builtin_tools import ToolOutput
+from deta.builtin_tools._files import MutationError
+from deta.builtin_tools.edit import EditArgs, edit_file
 from deta.builtin_tools.read import ReadArgs, ReadError, read_file
+from deta.builtin_tools.write import WriteArgs, write_file
 from deta.observability.artifacts import Artifacts
 from deta.types import ToolSchema

@@ -25,8 +29,8 @@
     description: str
     # 参数模型类对象，用于生成 JSON Schema 并验证实际传入的参数。
     args_model: type[Args]
-    # 真实执行函数，接收已验证的 Args 与工作目录，返回工具输出字符串。
-    handler: Callable[[Args, Path], str]
+    # 同步执行函数，接收已验证的 Args 与工作目录，返回文本或 ToolOutput。
+    handler: Callable[[Args, Path], str | ToolOutput]


 TOOLS: dict[str, ToolSpec[Any]] = {
@@ -35,6 +39,18 @@
         description="读取 UTF-8 文本，返回行号和分页提示。",
         args_model=ReadArgs,
         handler=read_file,
+    ),
+    "write": ToolSpec(
+        name="write",
+        description="创建或覆盖文本文件，返回 diff。",
+        args_model=WriteArgs,
+        handler=write_file,
+    ),
+    "edit": ToolSpec(
+        name="edit",
+        description="按原文件唯一匹配批量替换，返回 diff。",
+        args_model=EditArgs,
+        handler=edit_file,
     ),
 }

@@ -96,7 +112,8 @@
         )
         if call_ref is not None:
             span.set_attribute("deta.call_artifact", call_ref)
-        code: Literal["unknown_tool", "invalid_arguments", "read_failed"] | None = None
+        code: str | None = None
+        output: ToolOutput | None = None
         try:
             spec, args = resolve_tool_call(call, registry)
         except KeyError:
@@ -117,7 +134,9 @@
                     set_status_on_exception=False,
                 ) as execution:
                     try:
-                        content = await asyncio.to_thread(spec.handler, args, workspace)
+                        raw = await asyncio.to_thread(spec.handler, args, workspace)
+                        output = ToolOutput(raw) if isinstance(raw, str) else raw
+                        code, content = output.error_code, output.content
                     except BaseException as exc:
                         execution.set_status(
                             Status(StatusCode.ERROR, type(exc).__name__)
@@ -125,14 +144,22 @@
                         raise
             except ReadError as exc:
                 code, content = "read_failed", str(exc)
+            except MutationError as exc:
+                code, content = f"{call['name']}_failed", str(exc)
             except (OSError, UnicodeError) as exc:
-                code, content = "read_failed", f"读取失败：{type(exc).__name__}"
+                code, content = (
+                    f"{call['name']}_failed",
+                    f"文件操作失败：{type(exc).__name__}",
+                )
         result = ToolMessage(
             tool_call_id=call["id"] or "",
             name=call["name"],
             content=content,
             status="error" if code else "success",
-            artifact={"error_code": code},
+            artifact={
+                "error_code": code,
+                "details": output.details if output is not None else {},
+            },
         )
         span.set_attribute(
             "deta.outcome", (result.artifact or {}).get("error_code", None) or "success"
```

</details>

## 直接提供的文件操作基础

### src/deta/builtin_tools/_files.py

直接提供的完整文件。

<details>
<summary>直接提供：src/deta/builtin_tools/_files.py（完整文件，内部实现选读）</summary>

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

## 直接提供的 write 与 edit 工具

将下面两个完整文件放入对应路径，配合上面的 _files.py 和接入补丁使用。无需手写 write_file、apply_replacements 或 edit_file；先用单处精确替换走通真实任务，再按需要阅读批量替换算法。

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

## 错误如何回到模型

| 情况 | 结果与效果 |
| --- | --- |
| 参数 schema 不通过 | invalid_arguments，不进入文件工具 |
| 匹配不存在、重复或重叠 | edit_failed，目标文件不发生本次部分修改 |
| 路径范围、文本类型、输出预算失败 | 对应 write_failed/edit_failed，在落盘前拒绝 |
| 权限、读取或替换的普通文件错误 | 按工具名返回文件操作失败，供模型据此修正 |
| 内部状态或 Hook 错误 | 向 Run 传播，不能当作一次普通文件业务失败继续 |

今天文件操作仍经 Day 3 的线程入口执行。取消等待线程不会杀死线程，因此 Day 6 将补充“等待文件修改收尾后再结束 Run”的责任；本日不能把取消通知当作磁盘已经停止变化的证明。

## 怎样核对接入结果

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
uv run deta --help
```

在实际接入现成源码后运行这些检查，再完成下方使用验收。现成工具仍需核对集成结果；静态检查通过不代表真实模型或工具行为已经验证。

选择一个你明确用作练习的临时项目目录，从同一个 Agent 入口提出真实文件任务：

```bash
uv run deta -C "/绝对路径/临时项目" -p "创建 NOTES.md，写明本项目的用途，并展示实际 diff。"
uv run deta -C "/绝对路径/临时项目" -p "先读取 NOTES.md，再用 edit 精确替换其中一处已确认的文本，保留其他内容。"
```

检查最终文件、工具 diff、before/after 哈希和模型回答是否一致。对已有实际文本，手工提交一次含缺失匹配或重叠范围的请求，确认整个调用拒绝且文件字节没有改变；不要只看模型说“修改失败”。若没有 CRLF、BOM 或边界大小的实际文件，记录相应项目未验证，不创建测试 fixture 冒充覆盖。

发现真实失败时先在实施记录中写下：用户期望、实际结果、涉及路径、tool_call_id、Trace/产物引用和当前假设。今天只建立可追溯的初步 badcase，不创建自动归因服务或评测数据集。

## Pi 对照与下一天

| Pi 位置 | 本日吸收的语义 | Deta 差异 |
| --- | --- | --- |
| `harness/tools/write.ts` | 创建、覆盖、父目录处理 | 本版只支持有界 UTF-8 文本，返回 diff |
| `harness/tools/edit.ts` | 同批基于原文件、匹配唯一、范围不重叠 | 使用 old_text/new_text Python 字段，不兼容旧参数形态 |
| `edit-diff.ts` | 规范化匹配、恢复换行与 BOM、生成差异 | 混合换行明确拒绝；使用 Python difflib |
| 文件变更队列 | 避免同一运行中的并发修改 | 当前 Loop 串行；不承诺多个 Agent/进程对同文件互斥 |

继续把运行证据与差异写入 `docs/pi-alignment.md`。下一阶段[接入现成 bash 工具](day6.md)，直接使用提供的实现，让模型能根据已有项目检查结果继续修正。
