# Deta Python API

以下是正式调用方式，会请求真实模型并可能执行文件和 shell 操作。与 CLI 相同，需要显式配置模型、窗口与允许执行的工作目录。

## 组装并运行

```python
import asyncio
import os
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr

from deta.cli import show
from deta.model import DEFAULT_BASE_URL, ModelConfig, open_model
from deta.observability.artifacts import Artifacts
from deta.observability.tracing import artifact_listener, local_tracing
from deta.runtime import AgentSession
from deta.session import Session
from deta.storage import SQLiteStore
from deta.types import RunOptions


async def main() -> None:
    workspace = Path.cwd().resolve()
    key = os.environ["OPENAI_API_KEY"]
    config = ModelConfig(
        model=os.environ["OPENAI_MODEL"],
        api_key=SecretStr(key),
        base_url=os.environ.get("OPENAI_BASE_URL", "").strip() or DEFAULT_BASE_URL,
    )
    run_id = uuid4().hex
    root = workspace / ".deta" / "runs" / run_id
    artifacts = Artifacts(
        root / "artifacts",
        capture_body=True,
        redact=lambda text: text.replace(key, "[REDACTED_API_KEY]"),
    )
    with (
        closing(SQLiteStore(workspace / ".deta" / "sessions.sqlite3")) as store,
        local_tracing(root / "spans.jsonl") as tracer,
    ):
        # 重新打开时给 Session 第三个参数传既有 session_id。
        session = Session(store, workspace)
        async with open_model(config) as client:
            runtime = AgentSession(
                client,
                config,
                workspace,
                tracer,
                artifacts,
                session=session,
                context_window=int(os.environ["OPENAI_CONTEXT_WINDOW"]),
                instructions="你是 Deta。按用户指定的范围读取、修改文件并执行已有检查。",
                options=RunOptions(max_requests=10, max_tool_calls=20),
                environment={
                    name: os.environ[name]
                    for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
                    if name in os.environ
                },
                listeners=[artifact_listener(artifacts), show],
            )
            result = await runtime.prompt("读取 README.md，说明主要调用关系。", run_id=run_id)
            print(session.id, result.status, result.reason)
            print(result.answer)


asyncio.run(main())
```

调用方负责生命周期：等待运行与工具清理结束，再关闭客户端、Trace 和数据库。一个 `SQLiteStore` 只绑定一个 Session，并持有独占文件锁。

Python 调用方也可直接传 `ModelConfig(..., base_url="https://your-api-host/v1")`；省略时使用 OpenAI 官方地址。`open_model(config)` 使用该地址创建客户端，评测的 `run_batch(..., config=config)` 同样复用此配置。地址应为服务方要求的 API 基础路径，模型需支持当前使用的 OpenAI 兼容流式工具调用协议。

## 控制、维护与 Hooks

已有 `runtime` 时，空闲状态可调用 `runtime.use_skill(name)` 和 `await runtime.compact()`。同一实例的两次 `prompt` 不得重叠；运行期间用 `runtime.agent.steer(text)` 或 `follow_up(text)` 排队。`abort()` 请求取消，`await runtime.agent.wait()` 等待清理后的结果。

`runtime.agent.steering.mode` 和 `runtime.agent.followups.mode` 分别控制队列模式，默认 `"one"` 每次选择一条，`"all"` 选择当前全部。助手消息结尾调用 `continue_()` 时也遵守模式：优先选择 Steering，否则选择 Follow-up；已经选择 Steering 时跳过首次额外轮询，避免单条模式多取一条。选中的输入在提交成功后才逐条移出，取消或准备失败时未提交部分仍保留。队列仅保存在当前实例内，进程退出后不恢复。

`Hooks` 通过 `AgentSession(..., hooks=hooks)` 配置，默认都关闭。六个位置为 `prepare_request`、`transform_context`、`prepare_next_turn`、`before_tool`、`after_tool`、`finish_turn`。Hook 必须遵守 [hooks.py](../src/deta/hooks.py) 的返回类型；新增或改写请求内容时创建 `ContextItem(source="hook", entry_ids=())`。普通事件监听器仅用于观察。

## 录制与离线回放

在线运行前创建 `Recorder(artifacts)` 并调用 `recorder.attach(runtime)`。只使用空闲的新 Session，正文采集必须开启。Run 完整结束后调用 `index = recorder.finish(result, prompt)`，索引的 `complete` 才能说明是否满足回放条件。

离线回放需重新组装空 Session：给 `AgentSession` 传 `client=None`，保留原模型标识、窗口、指令、工具及起始资源，但 API key 可使用明确的离线占位值。然后依次调用：

```python
from pathlib import Path

from deta.observability.replay import Replay

tape = Replay(Path(index))
tape.attach(offline_runtime)
result = await offline_runtime.prompt(tape.index["prompt"])
tape.finish(result)
```

这段代码放在调用方已有的异步函数里。离线边界消费保存的模型及工具结果，不重新修改磁盘；`finish` 必须调用，它核对全部记录是否消费和最终回答是否一致。脱敏改写、缺失或参数不匹配会拒绝回放。

## 隔离评测与报告

从真实任务整理 `EvalTask`，验收用 `Acceptance` / `FileRule`，提示词版本用 `Variant`。每个任务的 `initial` 是 `initial_root` 下的干净项目目录，`allowed_changes` 写实际允许修改的路径，`checks` 只放人工确认的已有检查命令。

调用 `await run_batch(tasks, variants, repeats=..., initial_root=..., batch_root=..., config=..., context_window=..., environment=...)`。`batch_root` 与初始项目必须分离且尚未存在；每个 Trial 拥有独立工作区和数据库。该入口返回全部计划对应的 observation，保存 `protocol.json` 和 `observations.json`。

报告使用已经保存的计划，不能重新调用 `plan_trials` 生成另一组 ID：

```python
import json
from pathlib import Path

from deta.evaluation.report import render_report
from deta.evaluation.runner import Trial

batch = Path(batch_root)
protocol = json.loads((batch / "protocol.json").read_text())
trials = tuple(Trial.model_validate(value) for value in protocol["trials"])
rows = json.loads((batch / "observations.json").read_text())
(batch / "report.md").write_text(render_report(trials, rows), encoding="utf-8")
```

`summarize` 保留缺失与异常试验；`compare` 需要显式选择 control 和 candidate。报告默认按版本 ID 排序显示配对方向，ID 应体现希望比较的先后顺序。批内比较的是同一代码上的提示词版本；代码改动对照按 [Day14](../days/day14.md) 的两个独立环境流程处理。

`Badcase` / `EvidenceRef` 定义在 `observability.artifacts`。只把有实际 Run 和产物的失败写为案例；未知根因保持 `None`，原案例和回归通过前不标记 `verified`。
