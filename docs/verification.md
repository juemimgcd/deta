# Deta 验收记录

记录日期：2026-10-01。
环境：macOS / zsh，Python 3.14.7；依赖使用现有 `uv.lock`，本轮未修改依赖或锁文件。
源码指纹：`312bb6dce73d537aac4bb685ebc748b14b8537af50764e3f6a6cce01aa7d749c`，由 `source_version(src/deta)` 计算。

## 实现交付

已从当前 Day1–14 的完整答案与接入补丁组装 30 个 Python 模块，26 处补丁均精确应用。累计代码写入 `src/deta`，保留同一套 Loop、模型边界与 Session；未留下练习 TODO 或 NotImplementedError。Day15 的 README、API 用法、Pi 对照和本记录已创建。

落地时补齐 CLI 配置提示及空白输入检查、把观测模块的局部 import 移至文件顶部、统一模型快照与 Span 的配置版本，并清理源码中的阶段性注释。这些工程整理不改变计划的责任边界。

## 静态检查与入口

原始输出：[2026-10-01-checks.json](../.deta/verification/2026-10-01-checks.json)。本地证据目录被 Git 忽略；复制代码到另一台机器后应重新执行以下命令。

| 命令或检查 | 实际结果 |
| --- | --- |
| `uv sync --locked` | 通过，49 个包解析完成，48 个已安装包检查完成 |
| `uv run --locked ruff check src/deta` | 通过，All checks passed |
| `uv run --locked ruff format --check src/deta` | 通过，30 files already formatted |
| `uv run --locked mypy` | 通过，30 个源文件严格类型检查无问题 |
| `uv run --locked deta --help` | 通过，显示 prompt / continue / timeline / workspace / session / database / capture-body |
| `uv run --locked python -m deta --version` | 通过，deta 0.1.0 |
| 包模块导入 | 29 个模块导入完成；会执行入口的 `__main__` 由上面的模块命令单独验证 |
| `git diff --check` | 通过 |

未创建或修改测试文件、测试案例、模拟模型或测试 fixtures。仓库目前没有独立测试套件，本轮未运行不存在的测试命令。

## 实际工具与本地存储

以下操作直接调用当前 `execute_tool`，使用真实文件与命令，没有经过模型选择或 Agent Loop。证据：[delivery.json](../.deta/delivery/29e9fda231034ef88e13b33f62d22825/delivery.json)与 [spans.jsonl](../.deta/delivery/29e9fda231034ef88e13b33f62d22825/spans.jsonl)。

| 操作 | 实际结果 | 证据能说明的范围 |
| --- | --- | --- |
| write README.md | 成功创建运行说明，返回 diff 与文件指纹 | write、参数校验、统一结果与 dispatch 正常调用 |
| edit target.md | 成功精确替换项目状态段，返回 diff | 单次唯一匹配和写回执行成功 |
| read README.md | 成功读取前 8 行 | 正常读取和行范围输出可用 |
| bash `uv run --locked ruff check src/deta` | 退出码 0，返回真实输出文件 | shell 启动、输出采集和正常进程清理可用 |
| 四个工具 dispatch | execution_started=true，outcome=success | 前置校验成功后实际进入 handler；修复了闲置 dispatch 参数 |
| SQLite 默认数据库初始化 | user_version=1，六张表存在，integrity_check=ok | schema 可创建且数据库完整；不证明恢复和故障事务语义 |
| 实际工作区资源扫描 | 0 份 AGENTS.md，0 个技能；成功完成 | 当前空资源工作区可加载；不证明非空目录作用域与技能加载 |

本轮工具操作未创建 Session Run，也未把手工调用伪装成模型执行证据。数据库为 `.deta/sessions.sqlite3`，未写入模拟会话。

## 真实模型入口

已执行实际入口：

```bash
uv run --locked deta -p '读取 README.md，说明项目入口与主要调用关系。'
```

退出码为 1，提示 `配置错误：请设置 OPENAI_MODEL 和 OPENAI_API_KEY`。当前 shell 的 `OPENAI_API_KEY`、`OPENAI_MODEL`、`OPENAI_CONTEXT_WINDOW` 均未配置，本地 `.env` 原有配置未改动。没有发出真实模型请求，没有产生可作为端到端证据的 Run。

配置后按 [README](../README.md) 的实际入口运行，不需要再填写教程练习函数。

## 尚未完成的行为验收

下面均为已实现、尚未获得对应真实运行证据的路径：

| 路径 | 还需要什么证据 |
| --- | --- |
| 正常 Agent 读取、修改、执行已有检查 | 真实模型选工具、完整响应与配对结果、最终文件及项目检查 |
| Steering / Follow-up / end / continue | 实际注入时机、消息顺序和请求次数 |
| 模型重试、预算与取消 | 实际网络错误/取消记录、尝试计数、清理和终态 |
| Session 重启、工具未知结果恢复 | 真实中断后的 intent 状态、配对恢复结果、不重放副作用 |
| Context 来源与 Hook 改写 | 最终 request artifact、原 Entry 与来源映射 |
| 首次/增量/任务前缀摘要 | 真实摘要输入与输出、保留条目、原始历史、usage |
| manual / threshold / overflow 压缩 | 快照、提交、重新预算、后续真实请求与任务产物 |
| 非空项目规则、技能与实例隔离 | 同一 Run 资源快照、目录作用域及两实例独立记录 |
| 严格录制回放 | 从真实完整 Run 封口的索引、离线消费与终态核对 |
| Badcase 定位与原案例复跑 | 原任务真实失败、最早偏离证据、修复及干净环境复跑 |
| 固定任务集与版本评测 | 真实任务与验收、全部计划 Trial、重复运行、评分和报告 |

本轮没有既有真实 Badcase 或已确认任务集，未编造案例、Run ID、通过率或提升数字。Day15 的真实失败闭环和模型行为验收仍待补齐，代码实现完成不代表这些效果已经验证。

## 已知范围

一个 OpenAI 官方提供方、串行工具、单路径 Session、POSIX SQLite 文件锁、本地输出。评测目录复制提供文件与会话分离，不提供权限或网络沙箱。Context token 数属于启发式估算；usage 缺失保持未知。同步诊断 I/O、Span 可能缺失、仅初始输入成功 Run 的回放范围、批内提示词对照等限制见 [README](../README.md)和 [API 用法](api.md)。
