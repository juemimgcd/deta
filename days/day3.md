# Day 3：工具契约与现成 read 接入

[总览](summary.md) · [前一天](day2.md) · [下一天](day4.md) · [项目目标](../target.md)

## 核心问题

模型返回 `read({"path":"target.md"})` 时，Python 怎样知道该调用哪个函数、参数是否合法、结果应该回复给哪个调用？今天用现成 read 工具学习这条调度链。

本文是学习与实现指南，依赖 Day 1、Day 2 的类型与观测边界。实际源码完成后再运行手工验收；文中的参数与输出形状不是已经发生的模型调用记录。

read、write、edit、bash 四个工具的业务实现均直接提供。你需要练习的是 Agent 怎样声明、校验、调用工具，以及怎样把配对结果交回模型。本章只手写 tools.py 的三个调度函数；read_file 的路径与分页实现选读。

## 今天新增什么

| 文件 | 本日内容 |
| --- | --- |
| `src/deta/builtin_tools/__init__.py` | 包声明，不做自动注册 |
| `src/deta/builtin_tools/read.py` | 直接提供 ReadArgs、ReadError 和完整 read_file |
| `src/deta/tools.py` | ToolSpec、显式 TOOLS、schema、参数解析与执行结果 |

本日复用 Day 2 的 Artifacts 保存工具调用和结果，不再建一个日志存储。Day 1 的生命周期事件类型已留好工具通知名称，但 `tool_start/tool_end` 的 Agent 事件由 Day 4 Loop 在实际调度处发布；本日只建立工具 Span。

工具函数可以由 Python 调用方直接使用，也可以经 execute_tool 使用。没有自动请求第二次模型，没有 Session 持久化，也没有 write/edit/bash。

## 先看函数怎样连接

```text
TOOLS["read"] = ToolSpec(
    name="read",
    args_model=ReadArgs,     ← 类对象：负责 schema 与参数校验
    handler=read_file        ← 函数对象：真正读取文件
)
           ├─ tool_schemas() → SDK tools → 模型看到工具声明
           └─ resolve_tool_call(call)
                ├─ 按 call["name"] 找到同一个 ToolSpec
                ├─ ReadArgs.model_validate(call["args"])
                └─ 返回 ToolSpec 和 ReadArgs 实例
                         ↓ execute_tool 调用
                  spec.handler(args, workspace) → 正文
                         ↓
                  ToolMessage(tool_call_id=call["id"], ...)
```

`ReadArgs`、`read_file` 在工具表里都不加括号：这里只保存对象。定义 schema 不会执行文件读取；模型提出调用也不会直接执行函数，只有运行时调度器才会调用 handler。

## 先认识本日的类与函数

| 对象 | 属性是什么意思 |
| --- | --- |
| `ReadArgs` | `path` 为路径；`offset` 是从 1 开始的行号，默认 1；`limit` 是最大完整行数，默认 200、最多 2000；禁止额外字段及字符串到整数的宽松转换 |
| `ReadError` | 可预期的读取失败，例如目录外路径、二进制内容或无法输出完整的一行 |
| `ToolSpec[Args]` | 将 `name`、`description`、`args_model`、`handler` 放在一起；Args 表示这个工具自己的参数类型，约束参数模型与 handler 配对 |
| `TOOLS` | 显式字典；每个键必须与对应 spec.name 一致。这里直接写配置，不用注册器、装饰器或目录扫描 |
| `ToolMessage` | 沿用 Day 1；`tool_call_id` 来自 call["id"]；`status` 标识成败，`artifact["error_code"]` 区分失败类别 |

工具表的 `Any` 只用于容纳不同参数模型的边界，不表示跳过运行时校验。真正调用 handler 前仍由同一份 args_model 校验。

| 函数 | 输入 → 行为 → 返回给谁 |
| --- | --- |
| `tool_schemas()` | 遍历 TOOLS → args_model.model_json_schema() → 返回 SDK 的 tools 参数；调用方传给 Day 2 stream_once |
| `resolve_tool_call(call)` | ToolCall → 查表、解析 JSON、字段校验 → 返回 `(spec, args)` 给执行器；今天执行 spec.handler |
| `read_file(args, workspace)` | 已验证参数与工作目录 → 解析真实路径、读取完整行、限制输出 → 返回字符串给 execute_tool |
| `execute_tool(call, workspace, ...)` | 未校验 ToolCall → 记录调度、校验、实际执行、构造结果 → 返回 ToolMessage 给调用方，Day 4 由 Loop 提交历史 |

`strict=False` 是提供方工具 schema 的选项，本例不要求服务端严格结构化生成，以保留可选参数默认值；本地 ReadArgs 的 `strict=True` 是另一层参数验证，两者不冲突。模型输出始终按不可信输入校验。

## read 的具体约定

- 相对路径以显式 workspace 为基准；绝对路径必须仍在该目录内。先 resolve 再判断范围，静态符号链接不能指向目录外。
- 仅支持普通 UTF-8 文本文件，拒绝 NUL 和无法解码的内容；空文件返回清晰说明。
- offset 从 1 开始，limit 表示最大返回行数；输出加行号，默认最多 200 行，总输出预算 32 KiB。
- 读取用有上限的 readline，不把整个大文件一次读进内存。为头部与分页提示预留 1024 字节。
- 按完整行截断，返回下一段 offset。单行过长不能用“继续同一行”假装前进，明确返回 read_failed；单行分段和更多编码不属于今天范围。
- 超过文件末尾返回失败；正常分页用最后一个实际输出行号加一继续。只增加 offset，不改变文件。

路径范围校验是工具行为规则，不是进程安全沙箱；本日不承诺抵抗其他进程在检查和打开之间替换路径。读取从文件头顺序扫描到 offset，且跳过区间遇到超长行也会失败，这是首版的明确限制。

## 直接提供的 read 工具

先放入下面两个完整文件，再完成调度层的三个函数。read.py 无需手写。

### src/deta/builtin_tools/__init__.py

直接提供的完整文件。

```python
"""Built-in tools are registered explicitly in deta.tools."""
```

### src/deta/builtin_tools/read.py

直接复制下面完整文件。参数模型和 read_file 都已提供；按行读取、路径检查与分页算法可在需要时选读，不需要填写工具实现。

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

## 调度层练习骨架

按 tool_schemas → resolve_tool_call → execute_tool 的顺序完成。现成 read_file 作为 handler 调用，练习重点是模型声明、参数校验、执行与结果配对。

### src/deta/tools.py

只填写：`tool_schemas`、`resolve_tool_call`、`execute_tool`。保留导入、字段和其他已给实现。

```python
# ruff: noqa: F401  # 为练习体预留的导入。
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import ToolCall, ToolMessage
from opentelemetry.trace import Status, StatusCode, Tracer
from pydantic import BaseModel, ValidationError

from deta.builtin_tools.read import ReadArgs, ReadError, read_file
from deta.observability.artifacts import Artifacts
from deta.types import ToolSchema


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
    # 真实执行函数，接收已验证的 Args 与工作目录，返回工具输出字符串。
    handler: Callable[[Args, Path], str]


TOOLS: dict[str, ToolSpec[Any]] = {
    "read": ToolSpec(
        name="read",
        description="读取 UTF-8 文本，返回行号和分页提示。",
        args_model=ReadArgs,
        handler=read_file,
    ),
}


def tool_schemas() -> list[ToolSchema]:
    """遍历显式工具表，为每个 ToolSpec 生成提供方需要的函数工具声明。
    参数 schema 来自该工具自己的参数模型，返回列表供调用方传给 stream_once。

    TODO：从同一份 TOOLS 和参数模型生成 SDK 工具 schema，不执行工具。
    """
    raise NotImplementedError("请完成 tool_schemas")


def resolve_tool_call(call: ToolCall) -> tuple[ToolSpec[Any], BaseModel]:
    """接收模型提出的 ToolCall，按名称查找工具并验证结构化参数字典。
    返回工具定义和参数对象给 execute_tool；未知名称抛 KeyError，参数错误抛 ValidationError。

    TODO：按名称查表，通过 model_validate 校验 args 字典，返回 spec 与参数对象，不执行 handler。
    """
    raise NotImplementedError("请完成 resolve_tool_call")


async def execute_tool(
    call: ToolCall,
    workspace: Path,
    *,
    tracer: Tracer,
    artifacts: Artifacts,
) -> ToolMessage:
    """接收工具调用、工作目录和观测依赖，完成查表、参数校验、实际执行及结果记录。
    将成功输出或可预期错误封装成配对的 ToolMessage 返回给调用者；内部错误与取消继续传播。

    TODO：分开记录调度与实际执行；校验失败不进入 handler；只将可预期工具错误转换成配对结果；保存诊断引用并返回。
    """
    raise NotImplementedError("请完成 execute_tool")
```

## 调度层参考答案



<details>
<summary>参考答案：src/deta/tools.py（完整文件）</summary>

```python
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import ToolCall, ToolMessage
from opentelemetry.trace import Status, StatusCode, Tracer
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from deta.builtin_tools.read import ReadArgs, ReadError, read_file
from deta.observability.artifacts import Artifacts
from deta.types import ToolSchema


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
    # 真实执行函数，接收已验证的 Args 与工作目录，返回工具输出字符串。
    handler: Callable[[Args, Path], str]


TOOLS: dict[str, ToolSpec[Any]] = {
    "read": ToolSpec(
        name="read",
        description="读取 UTF-8 文本，返回行号和分页提示。",
        args_model=ReadArgs,
        handler=read_file,
    ),
}


def tool_schemas() -> list[ToolSchema]:
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
        for spec in TOOLS.values()
    ]


def resolve_tool_call(call: ToolCall) -> tuple[ToolSpec[Any], BaseModel]:
    """接收模型提出的 ToolCall，按名称查找工具并验证结构化参数字典。
    返回工具定义和参数对象给 execute_tool；未知名称抛 KeyError，参数错误抛 ValidationError。
    """
    spec = TOOLS[call["name"]]
    args = spec.args_model.model_validate(call["args"])
    return spec, args


async def execute_tool(
    call: ToolCall,
    workspace: Path,
    *,
    tracer: Tracer,
    artifacts: Artifacts,
) -> ToolMessage:
    """接收工具调用、工作目录和观测依赖，完成查表、参数校验、实际执行及结果记录。
    将成功输出或可预期错误封装成配对的 ToolMessage 返回给调用者；内部错误与取消继续传播。
    """
    with tracer.start_as_current_span(
        "deta.tool.dispatch",
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("deta.tool_call_id", (call["id"] or ""))
        span.set_attribute("deta.tool_name", call["name"])
        span.set_attribute("deta.execution_started", False)
        call_ref = artifacts.save(
            "tool-call", TypeAdapter(JsonValue).validate_python(call)
        )
        if call_ref is not None:
            span.set_attribute("deta.call_artifact", call_ref)
        code: Literal["unknown_tool", "invalid_arguments", "read_failed"] | None = None
        try:
            spec, args = resolve_tool_call(call)
        except KeyError:
            code, content = "unknown_tool", "工具不存在"
        except ValidationError:
            code, content = (
                "invalid_arguments",
                "参数错误：请按工具 schema 提供 JSON 对象和正确字段类型",
            )
        else:
            span.set_attribute("deta.execution_started", True)
            try:
                with tracer.start_as_current_span(
                    "deta.tool.execute",
                    record_exception=False,
                    set_status_on_exception=False,
                ) as execution:
                    try:
                        content = await asyncio.to_thread(spec.handler, args, workspace)
                    except BaseException as exc:
                        execution.set_status(
                            Status(StatusCode.ERROR, type(exc).__name__)
                        )
                        raise
            except ReadError as exc:
                code, content = "read_failed", str(exc)
            except (OSError, UnicodeError) as exc:
                code, content = "read_failed", f"读取失败：{type(exc).__name__}"
        result = ToolMessage(
            tool_call_id=call["id"] or "",
            name=call["name"],
            content=content,
            status="error" if code else "success",
            artifact={"error_code": code},
        )
        span.set_attribute(
            "deta.outcome", (result.artifact or {}).get("error_code", None) or "success"
        )
        if result.status == "error":
            span.set_status(
                Status(
                    StatusCode.ERROR, (result.artifact or {}).get("error_code", None)
                )
            )
        ref = artifacts.save("tool-result", result.model_dump(mode="json"))
        if ref is not None:
            span.set_attribute("deta.result_artifact", ref)
        return result
```

</details>

## 从调用到结果，逐步看一次 read

以下为对象推演。假设 call["id"] 是 `call_1`，参数是 `{"path":"target.md","offset":1,"limit":20}`，workspace 是项目根目录。

1. `execute_tool` 创建 `deta.tool.dispatch` Span，标记 `execution_started=False`，可选保存调用正文。
2. `resolve_tool_call` 在 TOOLS 取出 read 的 ToolSpec。`model_validate` 返回 `ReadArgs(path="target.md", offset=1, limit=20)`，不是字典，也没有读取文件。
3. 它把 `(spec, args)` 返回给 execute_tool，其中 `spec.handler` 就是 `read_file`。执行器标记 `execution_started=True`，新建 `deta.tool.execute`，通过 `asyncio.to_thread` 调用同步读取函数。
4. 现成 `read_file` 使用 workspace 和已校验参数读取文件，返回带行号的正文及分页提示；不需要在 execute_tool 中再实现读取算法。
5. 返回行 1–20 后，有后续内容时提示 `offset=21`；没有后续内容时返回“已到文件末尾”。调用方按这个结果决定是否继续读取。
6. 执行器构造 `ToolMessage(tool_call_id="call_1", name="read", content=..., status="success", artifact={"error_code": None})`；保存结果引用后返回。
7. 目前结果交给 Python 调用者。Day 4 Loop 才会把助手消息及配对工具结果放入历史，再请求模型。

## 错误结果与内部失败分开

| 情况 | 结果 | 是否实际调用 read_file |
| --- | --- | --- |
| 工具名不在 TOOLS | unknown_tool | 否 |
| 非法 JSON、缺 path、offset=0、limit 为字符串、额外字段 | invalid_arguments | 否 |
| 路径不存在、不是文件、越界、编码失败、超长行 | read_failed | 是 |
| 正常读取或空文件 | error_code=None | 是 |
| 程序错误、取消等非预期异常 | 向调用者传播，不伪装为 read_failed | 取决于发生位置 |

参数验证错误不直接输出 ValidationError 的完整正文，避免把未经处理的输入写入日志。ReadError 消息由工具自己构造；OSError/UnicodeError 仅给类型，保留清晰类别。

`asyncio.to_thread` 让读取不阻塞事件循环，但取消等待不等于立刻终止线程中的文件读取。本日没有可中断文件 I/O 保证；Day 6 的 bash 必须额外处理子进程，不能照抄线程等待取消当作命令已终止。

Trace 中“模型提出”由 Day 2 的响应快照和 `deta.tool_calls_proposed` 记录；“进入调度”由 dispatch 记录；只有实际进入 handler 才出现 execute Span。调用 ID 连接响应和工具产物，因此参数失败不能冒充一次成功文件读取。

## 怎样核对

先运行项目已有静态检查：

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
```

然后使用 Python API 实际读取当前已有的 target.md。下面是正常使用命令，不创建测试文件、断言、mock 或初始数据：

```bash
uv run python -c 'from pathlib import Path; from deta.builtin_tools.read import ReadArgs, read_file; print(read_file(ReadArgs(path="target.md", offset=1, limit=20), Path.cwd()))'
```

接着按返回提示替换 offset 继续读取，核对行号没有跳过或重复。验证调度层与 Trace 时，在 `uv run python` 的交互环境中执行下面的正常 API 调用：

```python
import asyncio
from pathlib import Path

from langchain_core.messages import ToolCall

from deta.observability.artifacts import Artifacts
from deta.observability.tracing import local_tracing
from deta.tools import execute_tool

call = ToolCall(
    id="manual_read_1",
    name="read",
    args={"path": "target.md", "offset": 1, "limit": 20},
)
with local_tracing(Path(".deta/manual-read/spans.jsonl")) as tracer:
    result = asyncio.run(
        execute_tool(
            call,
            Path.cwd(),
            tracer=tracer,
            artifacts=Artifacts(Path(".deta/manual-read/artifacts")),
        )
    )
print(result.model_dump_json(indent=2))
```

这是手工发起的真实工具调用，不是“模型已选择工具”的证据。正文采集默认关闭；如需诊断文件，按 Day 2 约定显式设置 capture_body 和合适的脱敏函数。

用同一个入口手工替换本次输入，检查一个不存在的文件路径、offset=0 和一个未知工具名；分别确认 read_failed、invalid_arguments、unknown_tool，以及原调用 ID 始终保留。选用工作区已有、超过单页限制的文本文件观察分页；没有合适的大文件时标记该项待验收，不额外生成 fixture。检查文件工具输出不超过字节预算，过长单行返回明确失败。

以上手工步骤是实施后的验收清单，本文生成时没有执行真实模型请求或故障实验。

## 与 Day 4 的接线

Day 4 中，Loop 通过请求回调调用 `AgentSession._request`；后者用本轮工具表生成 schema，再传给 Day 2 的 stream_once。完整 AIMessage 返回 Loop 后，先提交助手消息，再逐个执行工具、提交 ToolMessage，最后决定是否继续。

必须先检查完整响应、stop_reason、工具批次预算及调用 ID；本日 execute_tool 假定调用者已确认允许执行。不能把 TextDelta/ToolCallDelta 直接接到 execute_tool，也不能把一次手工工具调用说成“Agent Loop 已完成”。

## Pi 对照与验收记录

| Pi 位置与职责 | Deta 对应 | 差异与后续 |
| --- | --- | --- |
| `agent-loop.ts` 工具准备与 validateToolArguments | tools.resolve_tool_call | 使用 Pydantic；完整 Loop 的历史检查与批次控制在 Day 4 |
| `types.ts` 的工具定义/执行职责 | ToolSpec、TOOLS、execute_tool | 显式表、串行工具；Hook 和终止提示后续补齐 |
| `harness/tools/read.ts` 的 offset/limit、截断与继续位置 | builtin_tools/read.py | 保留 1-based 分页语义；仅 UTF-8 文本，默认 200 行、最大 2000 行、32 KiB，不实现图片 |
| `harness/utils/truncate.ts` 的完整行输出与长行处理 | read_file 的字节预算和 ReadError | Python 实现采用有界 readline；单行超限明确失败，不承诺与 Pi 文案或所有细节相同 |

把实际读取路径、输出片段、Span 引用和未验证项写入 `docs/pi-alignment.md`。通过 Day 3 表示工具可以独立工作；“模型自行读取并基于结果回答”仍是 Day 4 的验收目标。
