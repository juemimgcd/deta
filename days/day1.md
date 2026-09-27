# Day 1：工程入口与数据契约

[总览](summary.md) · [下一天](day2.md) · [项目目标](../target.md)

## 核心问题

用户说“读取 target.md 并概括项目目标”之后，程序里先后会出现哪些对象？这些对象由谁创建，交给谁，哪些最终进入历史？今天先建立这些契约和可启动的包入口。

本文是学习与实现指南。代码块中的 `src/deta/` 路径是你接下来要创建的文件，不代表当前项目已经实现。参考 Zeta 的练习方式：先理解对象，复制骨架填写 TODO，再展开完整答案核对。

## 今天新增什么

| 文件 | 今天负责什么 |
| --- | --- |
| `pyproject.toml` | 在已有依赖上补包构建、命令入口和检查配置 |
| `src/deta/__init__.py`、`__main__.py` | 公共导出、版本与模块启动 |
| `src/deta/types.py` | 消息、工具调用、结果、usage 和运行契约 |
| `src/deta/events.py` | 模型流事件、Agent 事件和简单通知 |
| `src/deta/cli.py` | help/version；今天不调用模型 |
| `.env.example`、`.gitignore` | 配置名称与运行产物忽略规则 |
| `docs/pi-alignment.md` | 开始记录 Pi 对照与阶段缺口 |

当天没有 `Agent`、`run_loop` 或 `Session` 实现。`RunOptions`、`RunResult` 先定义数据形状，到 Day 4、Day 7 才接入运行控制，今天创建它们不会自动执行预算。

当前 `pyproject.toml` 已声明 OpenAI SDK、Pydantic、OpenTelemetry，以及 Ruff/mypy；沿用这些依赖。`target.md` 中“尚未声明项目依赖”是较早的状态描述，实施时以磁盘配置为准。下面只说明应补的配置，不要求覆盖已有内容。

## 先从具体对象看一次读取

下面是数据形状推演，不是真实运行记录。

```text
用户输入：读取 target.md 并概括项目目标
    ↓ 入口创建
UserMessage(content="读取 target.md 并概括项目目标")
    ↓ Day 2 将内部消息转换后发给模型
AssistantMessage(
    content="我先读取文件。",
    tool_calls=(ToolCall(id="call_1", name="read",
                        arguments_json='{"path":"target.md"}'),),
    stop_reason="tool_calls"
)
    ↓ Day 3 调度器按名称查表、校验参数、读取文件
ToolResult(tool_call_id="call_1", name="read", content="1: # Deta 项目目标…")
    ↓ Day 4 的 Loop 才会把结果送回模型并再次请求
AssistantMessage(content="Deta 是一个本地 Coding Agent…", stop_reason="stop")
```

`call_1` 是工具调用编号。同名 `read` 可以被调用多次，结果必须靠 `tool_call_id` 配对，不能只靠工具名。工具参数暂存为 JSON 字符串，是为了保留 SDK 返回的原文；参数对象解析和校验属于 Day 3。字符串完整也不保证 JSON 合法。

## 先认识本日的类与函数

| 类或别名 | 它是什么；字段是什么意思 |
| --- | --- |
| `Data` | 公共 Pydantic 基类；`extra="forbid"` 拒绝额外字段，`frozen=True` 禁止对象字段重新赋值 |
| `UserMessage` | `role` 固定为 user，`content` 是用户输入 |
| `ToolCall` | `id` 是调用编号；`name` 是查表键；`arguments_json` 是尚未验证的参数正文 |
| `AssistantMessage` | 一次最终响应；`content` 为文本，`refusal` 为拒绝内容，`tool_calls` 为完整调用集合；`stop_reason` 描述提供方为什么停止，`usage` 保存用量，`provider_response_id` 关联响应 |
| `ToolResult` | 配对的工具结果；`content` 是要回传的内容；`error_code=None` 为成功，其他值区分失败；`is_error` 是根据错误码计算的属性 |
| `Usage` | 输入、输出、总 token 数；`None` 表示提供方没给，`0` 表示明确报告零，两者不同 |
| `AgentMessage` | 三种消息的联合类型别名，不是新的容器类；系统指令作为请求配置单独传入 |
| `RunOptions` | 请求次数、工具次数和总时限；限制由后续 Loop 消费 |
| `RunResult` | 一次 Run 的编号、状态、原因、回答和消息；`completed/cancelled/limited/failed` 是 Deta 运行状态，不是模型 stop_reason |
| `TextDelta` | 模型新到的一小段文字，`text` 只含本次增量 |
| `ToolCallDelta` | `index` 标识同一响应内的调用槽位；`arguments_delta` 是参数碎片，不能交给工具 |
| `ModelDone` | 包装完整 `AssistantMessage`，供观察者查看；正式结果仍由请求函数返回 |
| `ModelEvent`、`Event`、`Listener` | 联合类型和函数类型别名；`Listener` 是接收一个事件、返回 None 的普通函数 |
| `AgentEvent` | Run/Turn/消息/工具的通知；`run_id`、`turn`、`tool_call_id` 关联执行；`model_event` 可携带流式更新；并非每个字段在每种事件中都有值 |

`Field(default_factory=Usage)` 表示每条响应各自创建一个 Usage。消息集合用 tuple，避免无意修改消息内部的集合；外层历史仍由后续 Agent/Session 按提交顺序管理。`Literal` 约束允许的字段值，`type X = ...` 定义类型别名。

| 函数 | 谁调用；输入、工作、输出 |
| --- | --- |
| `emit(event, listeners)` | 请求边界或后续 Loop 调用；逐个通知监听器；返回 None，不决定是否继续执行 |
| `main()` | 安装后的 `deta` 命令或 `python -m deta` 调用；解析参数，显示帮助或版本，返回进程退出码 |

监听器普通异常只记录异常类型，下一位监听器仍会收到通知。取消、KeyboardInterrupt 等不被普通 `Exception` 分支吞掉。监听器必须快速返回；这里没有后台队列和执行决策 Hook，后续需要决策时另设有限回调。

## 工程准备：直接提供的配置与类型

在已有 `pyproject.toml` 中补充以下配置。保留 `[project]`、依赖和 dev 依赖原值；同名表已经存在时合并，不能重复声明。

```toml
[build-system]
requires = ["uv_build>=0.8,<1"]
build-backend = "uv_build"

[project.scripts]
deta = "deta.cli:main"

[tool.ruff]
target-version = "py314"

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I"]

[tool.mypy]
python_version = "3.14"
strict = true
files = ["src/deta"]
```

完成文件后使用项目已有的 uv 工作流同步依赖并更新锁文件。`uv_build` 是构建后端，不是在业务模块里 import 的库。

`.env.example` 记录配置名称即可；先保持为空，在自己本地选择实际可用的模型和密钥。Day 2 明确以 OpenAI 官方 Chat Completions 为单一提供方，模型需支持文本流与函数工具调用。

```dotenv
OPENAI_API_KEY=
OPENAI_MODEL=
```

`.env.example` 不会自动加载；本日没有加入 dotenv。手工配置 shell 环境变量，或实施时明确选择加载方案。已有 `.gitignore` 只追加缺失项：

```gitignore
.venv/
__pycache__/
*.py[cod]
.mypy_cache/
.ruff_cache/
.env
.env.*
!.env.example
.deta/
dist/
```

### src/deta/types.py

直接提供的完整文件。

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Data(BaseModel):
    """所有 Deta 数据对象的公共基类，集中设置字段校验与冻结规则。
    子类负责声明具体业务字段，创建对象时由 Pydantic 校验这些字段。
    """

    # Pydantic 的类级配置：拒绝未声明字段，并禁止创建后重新赋值对象字段。
    model_config = ConfigDict(extra="forbid", frozen=True)


class Usage(Data):
    """保存一次模型响应报告的 token 用量，供终端显示、Trace 和后续预算使用。
    未获得的用量保留为 None，以便与提供方明确报告的零区分。
    """

    # 提供方报告的输入 token 数；None 表示未提供，数值必须非负。
    input_tokens: int | None = Field(default=None, ge=0)
    # 提供方报告的输出 token 数；None 表示未知，不自动补成零。
    output_tokens: int | None = Field(default=None, ge=0)
    # 提供方报告的总 token 数；未报告时保留 None，不用猜测值填充。
    total_tokens: int | None = Field(default=None, ge=0)


class ToolCall(Data):
    """保存模型提出的一次工具调用，由模型接入层在流读取完成后创建。
    执行器根据名称选择工具，并在执行前验证这里保存的原始参数。
    """

    # 模型给出的调用编号；后续 ToolResult.tool_call_id 必须使用同一个值。
    id: str = Field(min_length=1)
    # 工具名称，也是显式 TOOLS 字典中用于选择执行器的键。
    name: str = Field(min_length=1)
    # 模型返回的原始参数 JSON 字符串；此处保留原文，执行前再解析和校验。
    arguments_json: str


class UserMessage(Data):
    """表示一条用户输入，由入口或后续会话层创建。
    它进入消息历史后，会在请求边界转换成提供方的 user 消息。
    """

    # 固定的消息角色，用于将这条消息识别为用户输入。
    role: Literal["user"] = "user"
    # 用户输入的正文；字段要求至少一个字符，入口另行拒绝纯空白输入。
    content: str = Field(min_length=1)


class AssistantMessage(Data):
    """表示一次模型请求的完整响应，由 model.py 拼接流式内容后创建。
    调用者据此提交消息、判断停止原因，并决定是否进入后续工具调度。
    """

    # 固定的助手角色，在转换请求和恢复消息时标识消息来源。
    role: Literal["assistant"] = "assistant"
    # 所有正文分片拼接后的完整文本；仅调用工具时可以为空。
    content: str = ""
    # 提供方返回的拒绝内容；没有拒绝信息时为 None。
    refusal: str | None = None
    # 本次响应提出的工具调用元组；没有调用时为空，不代表这些工具已经执行。
    tool_calls: tuple[ToolCall, ...] = ()
    # 提供方结束响应的原因：自然停止、工具调用、输出额度耗尽或内容过滤。
    stop_reason: Literal["stop", "tool_calls", "length", "content_filter"]
    # 本次响应的用量对象；default_factory 为每条响应创建独立的 Usage。
    usage: Usage = Field(default_factory=Usage)
    # 提供方给出的响应编号，用于关联诊断记录；它与工具调用 ID 不同。
    provider_response_id: str | None = None


class ToolResult(Data):
    """保存一次工具处理的成功内容或可预期错误，由工具执行器构造。
    调用者通过调用 ID 将它与助手提出的 ToolCall 配对，再把内容送回模型。
    """

    # 固定的工具角色，转换请求时生成提供方的 tool 消息。
    role: Literal["tool"] = "tool"
    # 对应 ToolCall.id，保证模型能确定这份结果属于哪一次调用。
    tool_call_id: str = Field(min_length=1)
    # 对应的工具名称，用于内部记录和诊断。
    name: str = Field(min_length=1)
    # 送回模型的成功输出或错误说明，不能仅依靠内部错误码表达失败。
    content: str
    # None 表示成功；字符串标识失败类别，具体错误码由产生结果的模块定义。
    error_code: str | None = None

    @property
    def is_error(self) -> bool:
        """根据 error_code 计算当前结果是否失败，返回布尔值给调用者。
        这是只读属性，使用 result.is_error 访问；每次读取都根据当前错误码计算。
        """
        return self.error_code is not None


# 消息联合类型：用户输入、助手响应或工具结果，用于历史与请求边界的类型标注。
type AgentMessage = UserMessage | AssistantMessage | ToolResult


class RunOptions(Data):
    """保存一次 Agent 运行的资源限制，由入口组装后交给后续 Loop 使用。
    本类只定义限制数据，实际计数、超时和停止处理由运行控制代码完成。
    """

    # 一次 Run 的模型请求额度，后续由 Loop 计数和执行限制。
    max_requests: int = Field(default=10, ge=1)
    # 一次 Run 允许的工具调用总数；设为零表示不给工具执行额度。
    max_tool_calls: int = Field(default=20, ge=0)
    # 整次 Run 的总时间额度，单位为秒，与单次模型请求超时分别管理。
    timeout_seconds: float = Field(default=120, gt=0)


class RunResult(Data):
    """汇总一次 Agent 运行结束后的状态、原因、回答和消息。
    由后续运行层创建并返回给 Python 调用方或命令入口。
    """

    # Deta 为本次任务运行生成的业务编号，用于关联事件和诊断产物。
    run_id: str
    # 运行终态：完成、取消、达到限制或失败；与模型 stop_reason 分开。
    status: Literal["completed", "cancelled", "limited", "failed"]
    # 说明为什么以当前状态结束，正常完成时可以为空。
    reason: str = ""
    # 返回给用户的最终回答文本；未得到回答时可以为空。
    answer: str = ""
    # 随运行结果返回的消息元组，供调用方查看本次产生或使用的对话内容。
    messages: tuple[AgentMessage, ...] = ()
```

### src/deta/__init__.py

直接提供的完整文件。

```python
from deta.types import AgentMessage, RunOptions, RunResult

__version__ = "0.1.0"
__all__ = ["AgentMessage", "RunOptions", "RunResult", "__version__"]
```

### src/deta/__main__.py

直接提供的完整文件。

```python
from deta.cli import main

raise SystemExit(main())
```

## 完整练习骨架

先写 `emit`：遍历监听器快照，逐个调用，隔离普通观察异常。再写 `main`：建立 parser，注册 version，解析参数并显示帮助。骨架的 NotImplementedError 必须替换后才能验收。

### src/deta/events.py

只填写：`emit`。保留导入、字段和其他已给实现。

```python
# ruff: noqa: F401  # 为练习体预留的导入。
import logging
from collections.abc import Callable, Sequence
from typing import Literal

from deta.types import AssistantMessage, Data

logger = logging.getLogger(__name__)


class TextDelta(Data):
    """表示模型流中新到达的一段正文，由 model.py 创建并通知监听器。
    界面可以立即显示增量，最终消息则由请求函数单独汇总。
    """

    # 事件类型标识，监听器用它区分正文增量与其他通知。
    kind: Literal["text_delta"] = "text_delta"
    # 本次新到达的正文片段，不是累计全文。
    text: str


class ToolCallDelta(Data):
    """表示某个工具调用新到达的参数片段，供观察者显示或记录进度。
    片段按本次响应内的 index 归组，完整拼接并校验之前不能用于执行工具。
    """

    # 事件类型标识，表示这是工具参数的流式增量。
    kind: Literal["tool_call_delta"] = "tool_call_delta"
    # 调用在当前模型响应中的槽位，同一 index 的参数片段需要拼到一起。
    index: int
    # 本次新到达的参数字符串片段，可能只是 JSON 的一部分。
    arguments_delta: str


class ModelDone(Data):
    """通知观察者本次模型响应已经完整收集，携带最终助手消息。
    请求函数还会将同一个最终结果返回给调用者，由调用者负责后续提交。
    """

    # 事件类型标识，表示本次模型流已收集为最终消息。
    kind: Literal["model_done"] = "model_done"
    # 完整的 AssistantMessage，供监听器读取正文、调用、用量和结束原因。
    message: AssistantMessage


# 模型流事件联合类型，区分正文增量、参数增量与完整响应通知。
type ModelEvent = TextDelta | ToolCallDelta | ModelDone


class AgentEvent(Data):
    """描述 Agent 运行、轮次、消息和工具处理过程中的一个通知。
    后续 Loop 在对应边界创建事件，监听器利用关联字段显示或记录过程。
    """

    # 发生的生命周期动作，例如运行开始、轮次结束或工具执行通知。
    kind: Literal[
        "run_start",
        "run_end",
        "turn_start",
        "turn_end",
        "message_update",
        "message_end",
        "tool_start",
        "tool_end",
    ]
    # 事件所属 Run 的业务编号，将同一次运行的通知关联起来。
    run_id: str
    # 事件所属的模型轮次；没有轮次语义的通知可以为 None。
    turn: int | None = None
    # 事件关联的工具调用编号；非工具事件通常为 None。
    tool_call_id: str | None = None
    # 当前动作的状态说明，是否填写及具体值由事件产生位置决定。
    status: str | None = None
    # 可选的模型流事件，用于把增量或最终响应包装进 Agent 通知。
    model_event: ModelEvent | None = None


# 观察者可接收的全部通知类型，既包含模型事件，也包含 Agent 生命周期事件。
type Event = ModelEvent | AgentEvent
# 监听函数的类型：接收一个 Event 并返回 None；传入的是函数对象。
type Listener = Callable[[Event], None]


def emit(event: Event, listeners: Sequence[Listener]) -> None:
    """把传入事件依次交给 listeners 中的观察函数，返回 None。
    模型边界或后续 Loop 调用它发送通知；普通监听器异常被隔离，通知不参与执行决策。

    TODO：逐个调用监听器；普通异常仅记录类型；不吞取消或系统退出。
    """
    raise NotImplementedError("请完成 emit")
```

### src/deta/cli.py

只填写：`main`。保留导入、字段和其他已给实现。

```python
# ruff: noqa: F401  # 为练习体预留的导入。
import argparse

from deta import __version__


def main() -> int:
    """作为命令入口解析参数，支持帮助和版本信息，并在无参数时显示用法。
    由 deta 命令或 python -m deta 调用，正常完成后返回退出码 0。

    TODO：支持 --help、--version；无其他参数时显示帮助并返回 0。
    """
    raise NotImplementedError("请完成 main")
```

## 完整参考答案

<details>
<summary>参考答案：src/deta/events.py（完整文件）</summary>

```python
import logging
from collections.abc import Callable, Sequence
from typing import Literal

from deta.types import AssistantMessage, Data

logger = logging.getLogger(__name__)


class TextDelta(Data):
    """表示模型流中新到达的一段正文，由 model.py 创建并通知监听器。
    界面可以立即显示增量，最终消息则由请求函数单独汇总。
    """

    # 事件类型标识，监听器用它区分正文增量与其他通知。
    kind: Literal["text_delta"] = "text_delta"
    # 本次新到达的正文片段，不是累计全文。
    text: str


class ToolCallDelta(Data):
    """表示某个工具调用新到达的参数片段，供观察者显示或记录进度。
    片段按本次响应内的 index 归组，完整拼接并校验之前不能用于执行工具。
    """

    # 事件类型标识，表示这是工具参数的流式增量。
    kind: Literal["tool_call_delta"] = "tool_call_delta"
    # 调用在当前模型响应中的槽位，同一 index 的参数片段需要拼到一起。
    index: int
    # 本次新到达的参数字符串片段，可能只是 JSON 的一部分。
    arguments_delta: str


class ModelDone(Data):
    """通知观察者本次模型响应已经完整收集，携带最终助手消息。
    请求函数还会将同一个最终结果返回给调用者，由调用者负责后续提交。
    """

    # 事件类型标识，表示本次模型流已收集为最终消息。
    kind: Literal["model_done"] = "model_done"
    # 完整的 AssistantMessage，供监听器读取正文、调用、用量和结束原因。
    message: AssistantMessage


# 模型流事件联合类型，区分正文增量、参数增量与完整响应通知。
type ModelEvent = TextDelta | ToolCallDelta | ModelDone


class AgentEvent(Data):
    """描述 Agent 运行、轮次、消息和工具处理过程中的一个通知。
    后续 Loop 在对应边界创建事件，监听器利用关联字段显示或记录过程。
    """

    # 发生的生命周期动作，例如运行开始、轮次结束或工具执行通知。
    kind: Literal[
        "run_start",
        "run_end",
        "turn_start",
        "turn_end",
        "message_update",
        "message_end",
        "tool_start",
        "tool_end",
    ]
    # 事件所属 Run 的业务编号，将同一次运行的通知关联起来。
    run_id: str
    # 事件所属的模型轮次；没有轮次语义的通知可以为 None。
    turn: int | None = None
    # 事件关联的工具调用编号；非工具事件通常为 None。
    tool_call_id: str | None = None
    # 当前动作的状态说明，是否填写及具体值由事件产生位置决定。
    status: str | None = None
    # 可选的模型流事件，用于把增量或最终响应包装进 Agent 通知。
    model_event: ModelEvent | None = None


# 观察者可接收的全部通知类型，既包含模型事件，也包含 Agent 生命周期事件。
type Event = ModelEvent | AgentEvent
# 监听函数的类型：接收一个 Event 并返回 None；传入的是函数对象。
type Listener = Callable[[Event], None]


def emit(event: Event, listeners: Sequence[Listener]) -> None:
    """把传入事件依次交给 listeners 中的观察函数，返回 None。
    模型边界或后续 Loop 调用它发送通知；普通监听器异常被隔离，通知不参与执行决策。
    """
    for listener in tuple(listeners):
        try:
            listener(event)
        except Exception as exc:
            logger.warning("listener failed: %s", type(exc).__name__)
```

</details>

<details>
<summary>参考答案：src/deta/cli.py（完整文件）</summary>

```python
import argparse

from deta import __version__


def main() -> int:
    """作为命令入口解析参数，支持帮助和版本信息，并在无参数时显示用法。
    由 deta 命令或 python -m deta 调用，正常完成后返回退出码 0。
    """
    parser = argparse.ArgumentParser(prog="deta", description="Deta 本地 Coding Agent")
    parser.add_argument("--version", action="version", version=f"deta {__version__}")
    parser.parse_args()
    parser.print_help()
    return 0
```

</details>

## 谁产生状态，谁拥有历史

```text
CLI → UserMessage → 未来的 Agent/Loop
模型 SDK chunk → Day 2 model.py → ModelEvent → 终端/观察者
                              └→ AssistantMessage → 调用者
ToolCall → Day 3 execute_tool → ToolResult → 调用者
Day 4 Loop → 消息提交回调 → 内存历史
Day 8 AgentSession → Session → SQLite
```

`emit` 没有保存消息的职责。收到 20 个 TextDelta，只意味着收到 20 次更新；完整响应最终提交为一条 AssistantMessage。Day 8 接入后，Session 是持久化事实来源；Context 是为下一次模型请求生成的派生视图。

| 单位 | 具体例子 |
| --- | --- |
| Run | 一次“读取项目并解释目标”的任务运行 |
| Turn | 一次助手响应及其工具批次；读文件和读后总结通常是两个 Turn |
| 逻辑模型请求 | 为某次响应准备好输入并等待结果的操作 |
| Attempt | 一次 SDK 调用尝试；以后重试可令同一逻辑请求有多次 Attempt |
| Compaction 用户任务片段 | 一次用户输入到下一次用户输入之间的历史，可能包含很多模型 Turn |

模型返回 `stop_reason="tool_calls"` 表示这次响应停在工具调用处，不能据此把 Run 标为 completed。

## 怎样核对

在真正完成上述源码和工程配置后，从项目根目录运行：

```bash
uv sync
uv run deta --help
uv run deta --version
uv run python -m deta --version
uv run python -c 'from deta import AgentMessage, RunOptions, RunResult; print(RunOptions().model_dump())'
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
uv build
```

预期 help 显示用法，version 显示 `deta 0.1.0`，导入不要求密钥、不请求模型、不创建 `.deta/`。能说明 `ToolCall.id` 和 `ToolResult.tool_call_id` 的关系，并区分流式更新与最终消息。构建成功证明包配置可用，不证明 Agent 已能执行任务。

不添加测试文件、内联断言、mock 或 fixture；今天的记录由实际命令输出和手工解释组成。

## Pi 对照记录

在 `docs/pi-alignment.md` 开始记录下表；实施后补实际证据，不先勾选“通过”。本次阅读的本地 Pi HEAD 与目标基线一致：`1a584a7a56eb5e7b4ff8ccbd46430f1533282eed`。

| Pi 源码与语义 | Deta 落点 | 当前差异与待验收项 |
| --- | --- | --- |
| `packages/agent/src/types.ts` 的 AgentMessage、AgentToolResult、AgentEvent | `types.py`、`events.py` | 首版只支持文本与函数工具；多模态、工具终止提示 Day 7 再接入 |
| `agent-loop.ts` 的 streamAssistantResponse | Day 2 `model.py` 与 Day 4 Loop | 流事件和最终消息分开；本日没有流或 Loop 实现 |
| `agent.ts` 的 AgentState | Day 4 `agent.py` | 状态归属先确定；活动运行和队列未实现 |

进入 [Day 2](day2.md) 前，至少确保入口可启动并理解消息形状。后续按同一份类型逐步增强，不另建一套每天下复制的核心。
