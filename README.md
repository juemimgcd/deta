# Deta

Deta 是参考 Pi 核心执行语义实现的本地 Python Coding Agent。`days/day1.md`–`day14.md` 的累计实现已经写入 `src/deta`；Day15 的交付记录见 [验收记录](docs/verification.md)和 [Pi 对照](docs/pi-alignment.md)。

当前实现包含流式 Agent Loop、四个工具、取消与输入队列、SQLite 会话恢复、Context 来源追踪、Compaction、项目指令与 Skills、本地观测、录制回放和隔离评测。真实模型端到端、压缩续接、回放和评测批次尚未验收。

## 安装与配置

本地环境为 macOS / zsh，要求 Python 3.14+，依赖使用现有 `uv.lock`。

```bash
uv sync --locked
uv run deta --help
uv run python -m deta --version
```

在本地 `.env` 或 shell 环境中填写：

| 变量 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API 密钥 |
| `OPENAI_MODEL` | 支持 Chat Completions 文本流及函数工具调用的模型标识 |
| `OPENAI_CONTEXT_WINDOW` | 所选模型的实际窗口 token 数，必须是整数 |

配置名称见 [.env.example](.env.example)。Deta 不自动读取 `.env`；下面使用 uv 的 `--env-file` 显式加载它。当前模型入口固定为 OpenAI 官方 `https://api.openai.com/v1`，不按模型名称猜测窗口。

## 命令入口

```bash
# 创建会话，读取工作目录里的项目文件。
uv run --env-file .env deta -C . -p '读取 README.md，说明项目入口与主要调用关系。'

# 记录请求、响应、工具结果和事件正文。
uv run --env-file .env deta -C . --capture-body -p '读取 target.md 并概括项目目标。'

# 对已完成的会话提交新任务，先把 stderr 输出的 ID 填入此变量。
DETA_SESSION_ID='替换为实际会话ID'
uv run --env-file .env deta -C . --session "$DETA_SESSION_ID" -p '继续说明上下文压缩的调用过程。'

# 从合法的未完成历史继续，不新增用户消息。
uv run --env-file .env deta -C . --session "$DETA_SESSION_ID" --continue

# 查看已提交的耐久事件；无需模型配置。
uv run deta -C . --session "$DETA_SESSION_ID" --timeline
```

启动时 stderr 输出 `session_id`。`--database` 可指定数据库位置，重新打开时保持同一数据库和工作目录。`--continue` 会先补齐中断工具的说明；结果未知的工具不自动重放。历史末尾已经是最终助手消息且没有排队输入时，请使用 `--session ... -p ...` 提交新任务。

退出码：完成为 `0`，失败为 `1`，达到预算为 `2`，取消为 `130`。Ctrl-C 会等待活动运行收尾。缺少配置时显示配置名称，模型异常只显示错误类型；具体调用过程保存在诊断目录。

## 实现与调用顺序

```text
CLI / Python 调用方
  → AgentSession：组装资源、会话、上下文、模型与工具
  → Agent：当前任务、队列、取消、订阅
  → run_loop：准备请求 → 完整响应提交 → 串行工具 → 结果提交 → 续轮/结束
      ├─ model.stream_once → ChatOpenAI
      ├─ tools.execute_tool → read / write / edit / bash
      └─ Session → SQLiteStore

请求准备：Session Entry → ContextView → 资源/Hook → 预算 → 必要时 Compaction
压缩：选择范围 → 摘要 → 重新预算 → 快照校验 → 事务提交 → 重建请求
```

Session 保存事实；Context 为请求构建视图；Compaction 保留原始历史，只改变后续请求。事件监听器负责观察，不能代替可靠提交。模型消息直接使用 LangChain 原生类型。

| 模块 | 已实现行为 |
| --- | --- |
| `agent.py`、`loop.py` | Steering、Follow-up、显式继续/结束、批次终止、取消及运行收尾 |
| `tools.py`、`builtin_tools/` | schema 与执行表一致，参数校验、唯一编辑、原子写回、命令输出限制与进程组清理 |
| `storage.py`、`session.py` | 独占数据库、消息与工具状态事务、执行意图、未知结果恢复 |
| `context.py` | 最新摘要与保留条目展开、来源映射、Hook 来源校验、usage 与启发式预算 |
| `compaction.py`、`runtime.py` | 首次/增量/任务前缀摘要，手动/阈值/结构化溢出压缩，有界请求重建 |
| `resources.py` | 目录作用域的 AGENTS.md、技能目录、显式技能、每次 Run 资源快照 |
| `observability/` | Span、请求与结果产物、采集统计、Badcase、严格录制响应回放 |
| `evaluation/` | 干净目录复制、固定全部计划、产物先冻结再评分、重复运行与版本配对 |

## Python API

完整组装与维护入口见 [API 用法](docs/api.md)。主要接口：

- `AgentSession.prompt(text)`：提交新任务并等待完整结果。
- `AgentSession.continue_()`：恢复并继续合法历史。
- `AgentSession.compact()`：空闲时手动压缩；返回明确 outcome。
- `AgentSession.use_skill(name)`：空闲时选用技能，下一次 Run 加载正文。
- `runtime.agent.steer(text)` / `follow_up(text)`：向指定边界排队输入。
- `runtime.agent.abort()` / `wait()` / `subscribe(listener)`：取消、等待与订阅。
- `Recorder.attach/finish`、`Replay.attach/finish`：录制并严格离线消费同一套 Loop。
- `run_batch`、`summarize`、`compare`、`render_report`：使用真实任务定义执行评测及生成报告。

手动压缩、显式技能、录制回放和评测目前通过 Python API 调用。CLI 只提供 `--help` 中列出的参数。

默认 Run 预算为 10 次模型尝试、20 次工具调度、120 秒；可通过 `RunOptions` 调整。重试与摘要请求共用模型尝试预算；SDK 内部重试关闭。token 预算默认关闭，启用后未知 usage 会阻止继续声称预算合规；它不是提供方硬计费上限。

## 项目资源与运行数据

`AGENTS.md` 按所在目录约束子目录；同一目录链由深层规则优先。技能约定为 `.agents/skills/<目录>/SKILL.md`，frontmatter 只支持单行 `name` 和 `description`。默认请求包含技能目录，模型用 `read` 按需读取正文；`use_skill` 可显式加载。资源在 Run 开始时固定，修改后下次 Run 生效。

默认数据放在工作目录的 `.deta/`，已加入 Git 忽略规则：

```text
.deta/
├── sessions.sqlite3
└── runs/<run_id>/
    ├── spans.jsonl
    ├── manifest-<id>.json
    ├── capture-status-<id>.json
    ├── artifacts/
    └── tool-output/
```

关闭正文采集时仍可保存会话与运行清单，但无法完整复查模型输入。CLI 的脱敏仅遮住当前 API key；直接 API 调用方需为 `Artifacts` 提供适合项目内容的脱敏函数。

## 检查与验证边界

```bash
uv run ruff check src/deta
uv run ruff format --check src/deta
uv run mypy
```

实际执行结果、源码指纹和未验证项见 [docs/verification.md](docs/verification.md)。未创建测试文件、模拟模型、编造任务集或 Badcase；评测任务应来自真实任务及已有验收。

当前范围为单模型、串行工具、单路径会话和本地 SQLite。数据库使用 POSIX 文件锁，命令清理使用进程组，Windows 尚未适配。工作区路径约束与评测目录复制不构成操作系统沙箱。上下文估算使用启发式方法；诊断 I/O 为同步写入，Span 导出可能不完整。回放只支持从空会话开始、仅初始输入且完整成功的录制，不支持失败流、真实取消时序或恢复历史导入；缺失与不匹配会拒绝回放。
