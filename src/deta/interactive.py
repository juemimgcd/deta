"""Pi 风格的终端交互入口；所有任务复用 AgentSession。"""

import asyncio
import json
import time
import unicodedata
from collections import deque
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from prompt_toolkit.application import Application
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_focus
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from deta import __version__
from deta.events import AgentEvent, Event, ModelDone, TextDelta
from deta.runtime import AgentSession

# 颜色沿用 Pi dark 的 accent、text、muted 和 userMessageBg。
STYLE = Style.from_dict(
    {
        "": "#d4d4d4",
        "header": "#8abeb7 bold",
        "muted": "#808080",
        "status": "#8abeb7",
        "editor": "bg:#343541 #d4d4d4",
        "frame.border": "#505050",
        "frame.label": "#8abeb7",
        "completion-menu.completion": "bg:#343541 #d4d4d4",
        "completion-menu.completion.current": "bg:#3a3a4a #8abeb7",
        "scrollbar.background": "#505050",
        "scrollbar.button": "#d4d4d4",
    }
)
KEYS = {
    "send": ("enter",),
    "newline": ("c-j",),
    "followup": ("escape", "enter"),
    "cancel": ("escape",),
    "interrupt": ("c-c",),
    "quit": ("c-d",),
    "tools": ("c-o",),
    "page_up": ("pageup",),
    "page_down": ("pagedown",),
    "bottom": ("c-end",),
}
COMMANDS = (
    "/help",
    "/session",
    "/continue",
    "/compact",
    "/skill",
    "/steer",
    "/follow",
    "/clear",
    "/quit",
)
HELP = """Enter 发送；运行中 Enter 追加指令，Alt+Enter 排队后续任务。
Ctrl+J 换行；Esc 取消；Ctrl+C 取消任务或清空输入；空输入 Ctrl+D 退出。
PgUp/PgDn 翻阅；Ctrl+End 回到底部；Ctrl+O 展开/折叠工具输出。

/continue       继续已有历史或待处理队列
/compact        空闲时压缩上下文
/skill 名称     空闲时启用项目技能
/steer 内容     将指令放入 Steering 队列
/follow 内容    将任务放入 Follow-up 队列
/session        查看会话、工作目录和诊断位置
/clear          清空屏幕显示，保留会话历史
/help           显示帮助
/quit           等待当前任务取消并收尾后退出

排队输入只保存在当前进程；取消后用 /continue 继续，退出会丢弃未提交队列。"""
MAX_ITEM_CHARS = 64 * 1024
MAX_SCREEN_CHARS = 256 * 1024


def plain(text: str) -> str:
    """按文本显示外部内容，不执行模型或工具输出中的终端控制字符。"""
    return "".join(
        char
        for char in text
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf"}
    )


@dataclass
class DisplayItem:
    title: str
    text: str = ""
    tool: bool = False


class TerminalChat:
    """拥有显示状态和一个界面操作任务，不另建模型或工具执行循环。"""

    def __init__(self, runtime: AgentSession) -> None:
        self.runtime = runtime
        self.items: deque[DisplayItem] = deque(maxlen=200)
        self.tools: dict[str, DisplayItem] = {}
        self.assistant: DisplayItem | None = None
        self.operation: asyncio.Task[None] | None = None
        self.started = 0.0
        self.state = "就绪"
        self.closing = False
        self.expanded = False
        self.follow_bottom = True
        self.dirty = True
        self.transcript = TextArea(
            read_only=True,
            focusable=False,
            scrollbar=True,
            wrap_lines=True,
        )
        self.editor = TextArea(
            multiline=True,
            height=Dimension(min=2, preferred=3, max=5),
            prompt="> ",
            style="class:editor",
            history=InMemoryHistory(),
            completer=WordCompleter(COMMANDS, sentence=True),
            complete_while_typing=False,
            wrap_lines=True,
        )
        keys = KeyBindings()

        @keys.add(*KEYS["send"], filter=has_focus(self.editor))
        def send(event: KeyPressEvent) -> None:
            self.submit()

        @keys.add(*KEYS["newline"], filter=has_focus(self.editor))
        def newline(event: KeyPressEvent) -> None:
            self.editor.buffer.insert_text("\n")

        @keys.add(*KEYS["followup"], filter=has_focus(self.editor))
        def followup(event: KeyPressEvent) -> None:
            self.submit(followup=True)

        @keys.add(*KEYS["cancel"])
        def cancel(event: KeyPressEvent) -> None:
            self.cancel()

        @keys.add(*KEYS["interrupt"])
        def interrupt(event: KeyPressEvent) -> None:
            if self.busy:
                self.cancel()
            else:
                self.editor.buffer.reset()

        @keys.add(*KEYS["quit"])
        def quit_(event: KeyPressEvent) -> None:
            if self.editor.text:
                self.editor.buffer.delete()
            else:
                self.request_exit()

        @keys.add(*KEYS["tools"])
        def toggle_tools(event: KeyPressEvent) -> None:
            self.expanded = not self.expanded
            self.dirty = True

        @keys.add(*KEYS["page_up"])
        def page_up(event: KeyPressEvent) -> None:
            self.follow_bottom = False
            self.transcript.buffer.cursor_up(count=10)

        @keys.add(*KEYS["page_down"])
        def page_down(event: KeyPressEvent) -> None:
            self.transcript.buffer.cursor_down(count=10)

        @keys.add(*KEYS["bottom"])
        def bottom(event: KeyPressEvent) -> None:
            self.follow_bottom = True
            self.dirty = True

        self.app: Application[None] = Application(
            layout=Layout(
                HSplit(
                    [
                        Window(
                            FormattedTextControl(
                                [
                                    ("class:header", f" Deta {__version__}"),
                                    ("class:muted", "  /help 帮助 · Ctrl+D 退出"),
                                ]
                            ),
                            height=1,
                        ),
                        self.transcript,
                        Window(FormattedTextControl(self.status), height=1),
                        Frame(self.editor, title="消息 · Enter 发送 · Ctrl+J 换行"),
                        Window(FormattedTextControl(self.footer), height=2),
                    ]
                ),
                focused_element=self.editor,
            ),
            key_bindings=keys,
            style=STYLE,
            full_screen=True,
            mouse_support=False,
            min_redraw_interval=0.05,
            refresh_interval=0.2,
            before_render=self.render,
        )

    @property
    def busy(self) -> bool:
        return self.operation is not None and not self.operation.done()

    def add(self, title: str, text: str = "", *, tool: bool = False) -> DisplayItem:
        item = DisplayItem(title, text[-MAX_ITEM_CHARS:], tool)
        self.items.append(item)
        self.dirty = True
        return item

    def status(self) -> list[tuple[str, str]]:
        elapsed = f" · {time.monotonic() - self.started:.0f}s" if self.busy else ""
        agent = self.runtime.agent
        queued = len(agent.steering.items) + len(agent.followups.items)
        return [
            ("class:status", f" {self.state}{elapsed} · 待处理 {queued} · Esc 取消")
        ]

    def footer(self) -> list[tuple[str, str]]:
        usage = self.runtime.usage_stats
        tokens = usage.get("known_tokens", 0)
        unknown = usage.get("unknown_usage_attempts", 0)
        note = "（含未知用量）" if unknown else ""
        return [
            (
                "class:muted",
                plain(
                    f" {self.runtime.workspace}\n"
                    f" {self.runtime.config.model} · 本次已知 tokens {tokens}{note}"
                    f" · 会话 {self.runtime.session.id[:8]}"
                ),
            )
        ]

    def render(self, app: Application[None]) -> None:
        if not self.dirty:
            return
        parts: list[str] = []
        for item in self.items:
            body = item.text
            if item.tool and not self.expanded:
                lines = body.splitlines()
                body = "\n".join(lines[:6])
                if len(lines) > 6:
                    body += f"\n… 另有 {len(lines) - 6} 行，Ctrl+O 展开"
            parts.append(f"{item.title}\n{body}\n")
        text = plain("\n".join(parts))
        if len(text) > MAX_SCREEN_CHARS:
            text = "[较早显示已省略，完整消息保留于会话]\n" + text[-MAX_SCREEN_CHARS:]
        cursor = (
            len(text)
            if self.follow_bottom
            else min(self.transcript.buffer.cursor_position, len(text))
        )
        self.transcript.buffer.set_document(
            Document(text, cursor), bypass_readonly=True
        )
        self.dirty = False

    def on_event(self, event: Event) -> None:
        if not isinstance(event, AgentEvent):
            return
        if event.kind == "turn_start":
            self.tools.clear()
            self.assistant = self.add("Deta")
            self.state = "正在思考"
        elif event.kind == "message_update" and isinstance(
            event.model_event, TextDelta
        ):
            if self.assistant is not None:
                self.assistant.text = (self.assistant.text + event.model_event.text)[
                    -MAX_ITEM_CHARS:
                ]
                self.dirty = True
            self.state = "正在回答"
        elif event.kind == "message_end" and isinstance(event.model_event, ModelDone):
            message = event.model_event.message
            if self.assistant is not None:
                self.assistant.text = message.text[-MAX_ITEM_CHARS:]
                self.dirty = True
            for call in message.tool_calls:
                proposed = self.add(
                    f"工具 · {call['name']} · 等待",
                    json.dumps(call["args"], ensure_ascii=False),
                    tool=True,
                )
                self.tools[call["id"] or ""] = proposed
        elif event.kind in {"tool_start", "tool_update", "tool_end"}:
            item = self.tools.get(event.tool_call_id or "")
            if item is not None:
                if event.kind == "tool_start":
                    item.title = item.title.removesuffix("等待") + "执行中"
                    self.state = "正在执行工具"
                elif event.kind == "tool_update":
                    item.text = (item.text + (event.text or ""))[-MAX_ITEM_CHARS:]
                else:
                    for committed in reversed(self.runtime.agent.messages):
                        if (
                            isinstance(committed, ToolMessage)
                            and committed.tool_call_id == event.tool_call_id
                        ):
                            item.title = f"工具 · {committed.name} · {event.status}"
                            item.text = committed.text[-MAX_ITEM_CHARS:]
                            break
                self.dirty = True

    def submit(self, *, followup: bool = False) -> None:
        text = self.editor.text.strip()
        if not text or self.closing:
            return
        self.editor.buffer.append_to_history()
        self.editor.buffer.reset()
        self.follow_bottom = True
        if text.startswith("/"):
            command, _, argument = text.partition(" ")
            argument = argument.strip()
            if command == "/quit":
                self.request_exit()
            elif command == "/help":
                self.add("帮助", HELP)
            elif command == "/session":
                self.add(
                    "会话",
                    f"ID: {self.runtime.session.id}\n工作目录: {self.runtime.workspace}\n诊断: {self.runtime.artifacts.root.parent}",
                )
            elif command == "/clear":
                self.items.clear()
                self.add("显示已清空", "会话历史仍然保留。")
            elif command in {"/steer", "/follow"}:
                if argument:
                    self.queue(argument, followup=command == "/follow")
                else:
                    self.add("请输入内容", f"用法：{command} 内容")
            elif command in {"/continue", "/compact", "/skill"}:
                if self.busy:
                    self.add("暂不可用", "请等待当前操作收尾，或按 Esc 取消。")
                elif command == "/skill" and not argument:
                    self.add("请输入技能名", "用法：/skill 名称")
                else:
                    self.start(command, argument)
            else:
                self.add("未知命令", "输入 /help 查看支持的命令。")
            return
        if self.busy:
            if self.runtime.agent.running:
                self.queue(text, followup=followup)
            else:
                self.editor.text = text
                self.add("暂不可用", "正在准备或维护会话；输入已保留，请稍后发送。")
        else:
            self.add("你", text)
            self.start("prompt", text)

    def queue(self, text: str, *, followup: bool) -> None:
        if followup:
            self.runtime.agent.follow_up(text)
        else:
            self.runtime.agent.steer(text)
        self.add("你 · 已排队后续任务" if followup else "你 · 已排队指令", text)
        if not self.busy:
            self.add("提示", "输入 /continue 处理排队消息。")

    def start(self, action: str, text: str) -> None:
        self.started = time.monotonic()
        self.state = "正在压缩" if action == "/compact" else "正在准备"
        self.operation = asyncio.create_task(self.execute(action, text))
        self.operation.add_done_callback(self.settled)

    async def execute(self, action: str, text: str) -> None:
        try:
            if action == "/skill":
                self.runtime.use_skill(text)
                self.add("技能已启用", text)
                self.state = "就绪"
            elif action == "/compact":
                outcome = await self.runtime.compact()
                self.add(
                    "压缩",
                    f"{outcome.status}: {outcome.reason}"
                    + (
                        f"\ntokens: {outcome.tokens_before} → {outcome.tokens_after}"
                        if outcome.tokens_before is not None
                        and outcome.tokens_after is not None
                        else ""
                    ),
                )
                self.state = "就绪"
            else:
                result = (
                    await self.runtime.continue_()
                    if action == "/continue"
                    else await self.runtime.prompt(text)
                )
                self.state = {
                    "completed": "完成",
                    "cancelled": "已取消",
                    "limited": "达到限制",
                    "failed": "失败",
                }[result.status]
                self.add(self.state, result.reason)
        except asyncio.CancelledError:
            self.state = "已取消"
            self.add("已取消", "未提交的排队输入仍保留在当前实例。")
        except Exception as exc:
            self.state = "失败"
            # 不输出异常正文，避免 SDK 错误携带请求数据或凭据。
            self.add("操作失败", f"{type(exc).__name__}；检查配置或诊断记录后重试。")

    def settled(self, task: asyncio.Task[None]) -> None:
        if self.operation is task:
            self.operation = None
        if self.closing and self.app.is_running:
            self.app.exit()
        self.app.invalidate()

    def cancel(self) -> None:
        if not self.busy or self.state == "正在取消":
            return
        self.state = "正在取消"
        if self.runtime.agent.running:
            self.runtime.agent.abort()
        elif self.operation is not None:
            self.operation.cancel()

    def request_exit(self) -> None:
        self.closing = True
        if self.busy:
            self.cancel()
        else:
            self.app.exit()

    async def run(self) -> None:
        recovered = self.runtime.session.recover()
        history = self.runtime.session.messages()
        if history:
            self.add(
                "会话已恢复",
                f"显示最近 {min(len(history), 60)} 条消息；完整历史仍用于运行。",
            )
            for message in history[-60:]:
                if isinstance(message, HumanMessage):
                    self.add("你", message.text)
                elif isinstance(message, AIMessage):
                    self.add("Deta", message.text)
                elif isinstance(message, ToolMessage):
                    self.add(
                        f"工具 · {message.name} · {message.status}",
                        message.text,
                        tool=True,
                    )
        else:
            self.add(
                "开始一个任务",
                "描述要读取或修改的项目内容。\n输入 /help 查看命令；Enter 发送，Ctrl+J 换行。",
            )
        if recovered:
            self.add(
                "中断恢复", f"已补齐 {recovered} 个工具中断结果；不会自动重放工具。"
            )
        self.runtime.agent.subscribe(self.on_event)
        try:
            with patch_stdout():
                await self.app.run_async()
        finally:
            self.closing = True
            self.cancel()
            if self.operation is not None:
                try:
                    await self.operation
                except asyncio.CancelledError:
                    pass
            self.runtime.agent.listeners.remove(self.on_event)
