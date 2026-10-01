from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.messages import ToolCall, ToolMessage
from pydantic import Field, JsonValue, TypeAdapter

from deta.storage import SQLiteStore
from deta.types import AgentMessage, Data, RunResult

if TYPE_CHECKING:
    from deta.context import CompactionRecord

MESSAGE: TypeAdapter[AgentMessage] = TypeAdapter(AgentMessage)


class Entry(Data):
    """表示一个已提交会话条目；原始记录与后续 Context 投影分开保存。"""

    # 稳定条目编号，供请求来源和后续摘要引用。
    id: str
    # 数据库分配的递增序号；同一会话内有序，不要求相邻序号连续。
    seq: int = Field(ge=1)
    # 创建该条目的 Run；未来独立维护操作可以没有 Run。
    run_id: str | None
    # 条目用途；消息、压缩或其他明确类型，由 Context 决定是否投影。
    kind: str
    # 按 kind 解释的 JSON 正文，消息条目直接保存内部消息字段。
    payload: dict[str, JsonValue]


class Session:
    """解释持久化会话、提交消息并处理未知结果，不调用模型或工具。

    数据库是事实来源；Agent.messages 只是提交成功后更新的进程内视图。
    """

    def __init__(
        self, store: SQLiteStore, workspace: Path, session_id: str | None = None
    ) -> None:
        """创建或打开一个工作目录匹配的会话；恢复在运行入口显式完成。"""
        # 调用方拥有并负责关闭的唯一存储连接。
        self.store = store
        # 保存于数据库、重启后保持不变的会话编号。
        self.id = store.open_session(workspace, session_id)
        # 当前进程正在提交的 Run；开始成功后设置，结束后清空。
        self.run_id: str | None = None

    def entries(self) -> tuple[Entry, ...]:
        """读取并校验条目形状，返回独立对象快照给加载和 Context 构建。"""
        return tuple(
            Entry(
                id=row["id"],
                seq=row["seq"],
                run_id=row["run_id"],
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
            )
            for row in self.store.entries(self.id)
        )

    def messages(self) -> tuple[AgentMessage, ...]:
        """提取完整事实历史中的消息；非消息条目不伪装成聊天内容。"""
        return tuple(
            MESSAGE.validate_python(entry.payload)
            for entry in self.entries()
            if entry.kind == "message"
        )

    def start_run(self, run_id: str, config: JsonValue) -> None:
        """先保存运行身份与非秘密配置，成功后才允许提交本次消息。"""
        if self.run_id is not None:
            raise RuntimeError("会话已经拥有活动 Run")
        self.store.start_run(self.id, run_id, config)
        self.run_id = run_id

    def finish_run(self, result: RunResult) -> None:
        """提交终态并清理本地运行身份；数据库失败向外传播，后续须重新恢复。"""
        if self.run_id != result.run_id:
            raise ValueError("运行终态与当前会话不匹配")
        try:
            self.store.finish_run(self.id, result)
        finally:
            self.run_id = None

    def commit(self, message: AgentMessage) -> str:
        """提交一条完整消息及关联工具状态，返回新条目的稳定 ID。"""
        if self.run_id is None:
            raise RuntimeError("消息提交必须处于已登记的 Run 中")
        return self.store.append_message(self.id, self.run_id, message)

    def begin_tool(self, call: ToolCall) -> None:
        """在 handler 开始前提交意图；这条记录仍不等于外部效果已经完成。"""
        if self.run_id is None:
            raise RuntimeError("工具执行必须处于已登记的 Run 中")
        self.store.begin_tool(self.id, self.run_id, call)

    def recover(self) -> int:
        """补齐上次未结清调用的中断结果，不自动执行或重放任何工具。"""
        if self.run_id is not None:
            raise RuntimeError("活动 Run 中不能执行会话恢复")
        results: list[tuple[str, ToolMessage]] = []
        for row in self.store.pending_tools(self.id):
            unknown = row["state"] == "intent"
            result = ToolMessage(
                tool_call_id=row["call_id"],
                name=row["name"],
                content="执行中断，外部结果未知；继续前核对实际文件或进程，不要直接重复该操作。"
                if unknown
                else "上次运行在工具 handler 开始前结束，本次调用未执行。",
                status="error",
                artifact={
                    "error_code": "interrupted_unknown"
                    if unknown
                    else "interrupted_not_started",
                    "details": {
                        "old_run_id": row["run_id"],
                        "prior_state": row["state"],
                    },
                },
            )
            results.append((row["run_id"], result))
        self.store.recover(self.id, results)
        return len(results)

    def commit_compaction(self, expected_tip: str, record: CompactionRecord) -> str:
        """保存运行时已验证的候选；Store 在事务中核对快照、Run 与未结清工具。"""
        return self.store.append_compaction(
            self.id, self.run_id, expected_tip, record.model_dump_json()
        )
