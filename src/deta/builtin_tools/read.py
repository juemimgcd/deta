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
