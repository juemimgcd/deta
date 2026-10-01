# Pi 核心语义对照

参考仓库：`/Users/jquery/python_files/pi`。
参考提交：`1a584a7a56eb5e7b4ff8ccbd46430f1533282eed`，2026-10-01 已核对本地 HEAD 与该基线一致。
Deta 初始交付源码指纹：`312bb6dce73d537aac4bb685ebc748b14b8537af50764e3f6a6cce01aa7d749c`；后续终端界面修改见验收记录。

下表记录当前实现的职责与有意差异。源码与类型检查可确认实现位置；行为对齐仍需要真实输入、运行记录及产物。完整验证状态见 [verification.md](verification.md)。

| Pi 源码职责 | Deta 对应 | 当前证据 | 差异与范围 |
| --- | --- | --- | --- |
| 交互模式：消息区、编辑器、footer 与快捷键 | [interactive.py](../src/deta/interactive.py) | 已接入 prompt-toolkit；本地终端交互检查见验收记录 | 复用 AgentSession；未实现模型选择器、会话树、文件补全和 Markdown 高亮 |
| `packages/agent/src/types.ts`：消息、事件、工具契约 | [types.py](../src/deta/types.py)、[events.py](../src/deta/events.py)、[hooks.py](../src/deta/hooks.py) | 已实现，严格类型检查通过 | 消息直接使用 LangChain 原生类型；Deta 只定义运行与业务状态 |
| `agent-loop.ts`：准备、响应、工具、续轮和结束顺序 | [loop.run_loop](../src/deta/loop.py)、`execute_tool_batch` | 已实现，真实模型顺序待验收 | 串行工具；显式 end 优先于队列，continue 不叠加自然续轮；预算属于 Deta 策略 |
| `agent.ts`：运行状态、继续、队列与取消 | [Agent](../src/deta/agent.py)、`InputQueue` | 已实现，实际队列与取消时序待验收 | asyncio Task；仅在任务与清理结束后释放活动占用 |
| `harness/tools/`：文件与命令工具 | [tools.py](../src/deta/tools.py)、[builtin_tools](../src/deta/builtin_tools/__init__.py) | 四工具实际完成本次文档交付；dispatch 均记录 execution_started=true | 文件限定工作区内 UTF-8；1 MiB 文件、24 KiB diff；bash 为 POSIX 进程组 |
| `harness/runtime/drive/tools.ts`：执行意图、结果保存与恢复 | [Session](../src/deta/session.py)、[SQLiteStore](../src/deta/storage.py)、`AgentSession._execute_tool` | 已实现；数据库格式与完整性检查通过；中断恢复待验收 | 工具开始前提交 intent；结果与状态同事务；未知效果不自动重放 |
| `harness/session/context.ts`：会话投影 | [context.build_context](../src/deta/context.py)、`resolve_retained_tail` | 已实现，长历史实际投影待验收 | 单路径 SQLite Entry；最新摘要加原条目 ID 尾部，原始历史不删除 |
| `harness/messages.ts`：提供方消息转换 | [model.stream_once](../src/deta/model.py)、Context 指纹与预算 | 已实现，真实请求正文待验收 | 协议转换交给 ChatOpenAI，流分片用 AIMessageChunk 合并 |
| `harness/compaction/compaction.ts`：切点、准备与摘要 | [compaction.py](../src/deta/compaction.py) | 已实现，真实首次/增量/任务前缀摘要待验收 | UTF-8 字节启发式估算；完整工具批次尾部；文件操作信息区分失败和未知 |
| `compactWithRequest`：摘要生成与组合 | `generate_compaction`、`AgentSession._request_summary` | 已实现，真实摘要请求待验收 | 正文与摘要共享模型边界和 RunBudget；候选生成不代表已经提交 |
| `harness/runtime/drive/structural.ts`：触发、压缩与续接 | [AgentSession._request / _compact](../src/deta/runtime.py) | 已实现，阈值/溢出续接待验收 | manual、threshold、结构化 overflow；有限重建；快照和活动 Run 在事务中校验 |
| `harness/hooks.ts`：控制回调与观察通知 | `Hooks`、`LoopBindings`、`events.emit` | 已实现；工具默认 Hook 路径实际执行 | 六个有限 Hook，无插件框架；来源改写显式标记 hook；监听器只观察 |
| `harness/skills.ts`、`system-prompt.ts`：发现与按需读取 | [resources.py](../src/deta/resources.py)、`use_skill` | 已实现，当前项目空目录扫描执行成功；作用域与技能正文待验收 | 仅工作区 `.agents/skills/*/SKILL.md`；有限 frontmatter；每次 Run 固定资源快照 |
| telemetry 文档与上下文传播设计 | [observability/tracing.py](../src/deta/observability/tracing.py)、[artifacts.py](../src/deta/observability/artifacts.py) | 本次工具 Span 实际导出；模型/压缩全链路待验收 | 本地 JSONL 与同步正文产物；不宣称 Pi telemetry 文档中的所有能力已完成 |
| `packages/evals/`：计划、隔离、评分与对照 | [evaluation](../src/deta/evaluation/runner.py) | 已实现，真实批次待验收 | 目录隔离；先冻结产物再评分；同代码提示词对照，代码版本对照采用独立环境 |

Badcase 管理与严格录制响应回放是 Deta 增加的能力，见 [replay.py](../src/deta/observability/replay.py)。回放消费同一 Loop 的边界结果，不验证原运行的磁盘副作用；不完整或不匹配时拒绝运行。

本版未实现 TypeScript API、Pi 会话格式或插件格式兼容；未实现会话分支、多 Agent、跨会话 Memory、Web 前端、远程 RPC 或安全沙箱。终端界面参考 Pi 的布局与交互，不代表完整功能对齐。核心实现与真实行为验收分别记录，不能用本表替代模型运行证据。
