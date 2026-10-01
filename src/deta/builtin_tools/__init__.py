from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue


@dataclass(frozen=True)
class ToolOutput:
    """保存工具函数的业务输出，由调度器补上调用 ID 形成 ToolMessage。

    文件工具可以提供修改摘要和结构化细节，后续 bash 也复用这份输出契约。
    """

    # 要回传模型的有界文本，包含操作摘要和必要的 diff。
    content: str
    # 可预期的工具失败码；None 表示成功。
    error_code: str | None = None
    # 用于诊断的 JSON 数据，例如内容摘要、文件字节数或输出文件引用。
    details: dict[str, JsonValue] = field(default_factory=dict)
    # 本工具请求停止自然续轮的提示；批次聚合规则由 Loop 执行。
    terminate: bool = False


@dataclass(frozen=True)
class ToolContext:
    """给所有工具传递执行环境与增量输出回调，避免工具自行读取全局配置。"""

    # shell 的当前工作目录，与文件工具的路径基准一致。
    workspace: Path
    # stdout/stderr 原始字节文件的存放目录。
    output_dir: Path
    # 要启动的 shell 可执行文件，本机示例显式使用 /bin/zsh。
    shell: str
    # 显式传递的环境变量快照，不自动记录到 Trace。
    environment: Mapping[str, str]
    # 接收带通道标记的输出增量，交给 Loop 发布工具更新事件。
    on_output: Callable[[str], None]
