import fcntl
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from pydantic import JsonValue

from deta.types import AgentMessage, RunResult

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    previous_run_id TEXT REFERENCES runs(id),
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL
);
CREATE UNIQUE INDEX one_active_run ON runs(session_id) WHERE status = 'running';
CREATE TABLE entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    run_id TEXT REFERENCES runs(id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE tool_calls (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    assistant_entry_id TEXT NOT NULL REFERENCES entries(id),
    position INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('announced', 'intent', 'completed', 'interrupted')),
    result_entry_id TEXT REFERENCES entries(id),
    PRIMARY KEY (session_id, call_id)
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    run_id TEXT REFERENCES runs(id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
PRAGMA user_version = 1;
COMMIT;
"""


class SQLiteStore:
    """管理一份本地 SQLite 的结构、事务与记录读写，不启动或重放工具。

    文件锁使这份数据库同时只由一个 Deta 实例使用；调用方负责 close。
    """

    def __init__(self, path: Path) -> None:
        """取得独占使用权，打开数据库并核对格式版本；失败时释放已取得的资源。"""
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 连接存活期间持有的 macOS/POSIX 文件锁，防止另一实例误恢复活动 Run。
        self._lock = path.with_suffix(path.suffix + ".lock").open("a+b")
        # 一个 Store 只组装一个 Session，防止同进程内的第二个对象恢复活动会话。
        self._session_id: str | None = None
        connection: sqlite3.Connection | None = None
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            connection = sqlite3.connect(path, isolation_level=None)
            # 只在当前线程访问的连接；业务写入通过 transaction 显式提交。
            self.db = connection
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys = ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ValueError(f"不支持的会话数据版本：{version}")
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA synchronous = FULL")
            if version == 0:
                self.db.executescript(SCHEMA)
        except BaseException:
            if connection is not None:
                connection.close()
            self._lock.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """让调用方的一组 SQL 一起提交或回滚；不允许嵌套调用此入口。"""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException as primary:
            try:
                self.db.rollback()
            except sqlite3.Error as cleanup:
                primary.add_note(f"回滚也失败：{type(cleanup).__name__}")
            raise

    def close(self) -> None:
        """关闭连接并释放文件锁；应在 Agent 与工具全部结束后调用。"""
        try:
            self.db.close()
        finally:
            self._lock.close()

    def open_session(self, workspace: Path, session_id: str | None) -> str:
        """未给 ID 时创建会话；给出 ID 时仅加载既有会话并核对工作目录。"""
        if self._session_id is not None:
            raise RuntimeError("一个 SQLiteStore 只能绑定一个 Session")
        root = str(workspace.resolve(strict=True))
        if session_id is None:
            session_id = uuid4().hex
            with self.transaction() as db:
                db.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, root))
            self._session_id = session_id
            return session_id
        row = self.db.execute(
            "SELECT workspace FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None or row["workspace"] != root:
            raise ValueError("会话不存在或工作目录不一致")
        self._session_id = session_id
        return session_id

    def _event(
        self, session_id: str, run_id: str | None, kind: str, payload: JsonValue
    ) -> None:
        """在当前事务内记录耐久状态变化，事件不承担消息恢复的事实来源。"""
        self.db.execute(
            "INSERT INTO events(session_id, run_id, kind, payload_json) VALUES (?, ?, ?, ?)",
            (session_id, run_id, kind, json.dumps(payload, ensure_ascii=False)),
        )

    def _insert_entry(
        self, session_id: str, run_id: str | None, kind: str, payload_json: str
    ) -> str:
        """在当前事务内插入一个有稳定 ID 的条目，返回 ID 供工具状态与 Trace 关联。"""
        entry_id = uuid4().hex
        self.db.execute(
            "INSERT INTO entries(id, session_id, run_id, kind, payload_json) VALUES (?, ?, ?, ?, ?)",
            (entry_id, session_id, run_id, kind, payload_json),
        )
        self._event(
            session_id, run_id, "entry_committed", {"id": entry_id, "kind": kind}
        )
        return entry_id

    def start_run(self, session_id: str, run_id: str, config: JsonValue) -> None:
        """保存新 Run 及前一 Run 的关联；唯一索引拒绝同一会话的第二个活动 Run。"""
        with self.transaction() as db:
            previous = db.execute(
                "SELECT id FROM runs WHERE session_id = ? ORDER BY rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            db.execute(
                "INSERT INTO runs(id, session_id, previous_run_id, status, config_json) VALUES (?, ?, ?, 'running', ?)",
                (
                    run_id,
                    session_id,
                    previous[0] if previous else None,
                    json.dumps(config),
                ),
            )
            self._event(session_id, run_id, "run_start", {})

    def finish_run(self, session_id: str, result: RunResult) -> None:
        """保存运行终态与对应事件；提交失败必须交回运行层，不能报告为成功。"""
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE runs SET status = ?, reason = ? WHERE id = ? AND session_id = ? AND status = 'running'",
                (result.status, result.reason, result.run_id, session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("没有可结束的活动 Run")
            self._event(
                session_id,
                result.run_id,
                "run_end",
                {"status": result.status, "reason": result.reason},
            )

    def append_message(
        self, session_id: str, run_id: str, message: AgentMessage
    ) -> str:
        """把消息与其工具状态一起提交：助手声明调用，工具结果结清对应调用。"""
        with self.transaction() as db:
            entry_id = self._insert_entry(
                session_id, run_id, "message", message.model_dump_json()
            )
            if isinstance(message, AIMessage):
                for position, call in enumerate(message.tool_calls):
                    db.execute(
                        "INSERT INTO tool_calls(session_id, call_id, name, run_id, assistant_entry_id, position, state) VALUES (?, ?, ?, ?, ?, ?, 'announced')",
                        (
                            session_id,
                            (call["id"] or ""),
                            call["name"],
                            run_id,
                            entry_id,
                            position,
                        ),
                    )
            elif isinstance(message, ToolMessage):
                cursor = db.execute(
                    "UPDATE tool_calls SET state = 'completed', result_entry_id = ? WHERE session_id = ? AND call_id = ? AND name = ? AND run_id = ? AND state IN ('announced', 'intent')",
                    (entry_id, session_id, message.tool_call_id, message.name, run_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("工具结果没有对应的未结清调用")
            return entry_id

    def begin_tool(self, session_id: str, run_id: str, call: ToolCall) -> None:
        """在进入 handler 之前提交执行意图；失败时调用方必须停止执行工具。"""
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE tool_calls SET state = 'intent' WHERE session_id = ? AND call_id = ? AND name = ? AND run_id = ? AND state = 'announced'",
                (session_id, (call["id"] or ""), call["name"], run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("工具调用没有可执行的声明状态")
            self._event(
                session_id, run_id, "tool_intent", {"call_id": (call["id"] or "")}
            )

    def pending_tools(self, session_id: str) -> list[sqlite3.Row]:
        """按助手条目与调用顺序读取未结清记录，交给 Session 决定中断说明。"""
        return self.db.execute(
            "SELECT t.* FROM tool_calls t JOIN entries e ON e.id = t.assistant_entry_id WHERE t.session_id = ? AND t.state IN ('announced', 'intent') ORDER BY e.seq, t.position",
            (session_id,),
        ).fetchall()

    def recover(
        self, session_id: str, results: Sequence[tuple[str, ToolMessage]]
    ) -> None:
        """原子保存 Session 准备的中断结果，并结束遗留 running 记录，不调用工具。"""
        with self.transaction() as db:
            for old_run_id, result in results:
                entry_id = self._insert_entry(
                    session_id, old_run_id, "message", result.model_dump_json()
                )
                cursor = db.execute(
                    "UPDATE tool_calls SET state = 'interrupted', result_entry_id = ? WHERE session_id = ? AND call_id = ? AND name = ? AND run_id = ? AND state IN ('announced', 'intent')",
                    (
                        entry_id,
                        session_id,
                        result.tool_call_id,
                        result.name,
                        old_run_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("恢复时工具状态发生变化")
                self._event(
                    session_id,
                    old_run_id,
                    "tool_interrupted",
                    {
                        "call_id": result.tool_call_id,
                        "error_code": (result.artifact or {}).get("error_code", None),
                    },
                )
            for row in db.execute(
                "SELECT id FROM runs WHERE session_id = ? AND status = 'running'",
                (session_id,),
            ).fetchall():
                db.execute(
                    "UPDATE runs SET status = 'interrupted', reason = '上次运行未提交终态' WHERE id = ?",
                    (row["id"],),
                )
                self._event(session_id, row["id"], "run_interrupted", {})

    def entries(self, session_id: str) -> list[sqlite3.Row]:
        """返回按 seq 排序的原始条目行，JSON 与业务类型由 Session 解释。"""
        return self.db.execute(
            "SELECT id, seq, run_id, kind, payload_json FROM entries WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()

    def timeline(self, session_id: str) -> list[sqlite3.Row]:
        """读取已提交的状态事件，不包含未落盘的模型增量和完整 Trace。"""
        return self.db.execute(
            "SELECT seq, run_id, kind, payload_json, recorded_at FROM events WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()

    def append_compaction(
        self, session_id: str, run_id: str | None, expected_tip: str, payload: str
    ) -> str:
        """在同一事务里核对快照和工具状态，再追加摘要；原始条目不删除。"""
        with self.transaction() as db:
            tip = db.execute(
                "SELECT id FROM entries WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if tip is None or tip["id"] != expected_tip:
                raise ValueError("压缩准备快照已过期")
            if self.pending_tools(session_id):
                raise ValueError("工具尚未结清，不能提交摘要")
            active = db.execute(
                "SELECT id FROM runs WHERE session_id = ? AND status = 'running'",
                (session_id,),
            ).fetchone()
            if (active["id"] if active else None) != run_id:
                raise ValueError("压缩提交与活动 Run 不匹配")
            return self._insert_entry(session_id, run_id, "compaction", payload)
