import asyncio
import codecs
import logging
import os
import signal
from typing import BinaryIO
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from deta.builtin_tools import ToolContext, ToolOutput

logger = logging.getLogger(__name__)
# 每个通道只在内存和工具回复中保留末尾 12 KiB。
TAIL_BYTES = 12 * 1024
# 一次命令最多落盘 16 MiB；超过就终止进程组，并明确标记输出未完整采集。
CAPTURE_BYTES = 16 * 1024 * 1024


class BashArgs(BaseModel):
    """定义交给指定 shell 的命令与单次执行超时，工作目录来自 ToolContext。"""

    # 命令和时间参数使用严格校验。
    model_config = ConfigDict(extra="forbid", strict=True)
    # 原样交给 shell -c 的命令字符串，不再拼接路径或引号。
    command: str = Field(min_length=1, description="要执行的 shell 命令")
    # 命令等待上限；不包含必须完成的进程清理时间。
    timeout_seconds: float = Field(default=30, gt=0, le=120)


class CaptureLimitError(Exception):
    """表示命令输出达到本次磁盘采集预算，转为 output_limit 工具结果。"""


class OutputCaptureError(Exception):
    """表示输出文件或进程清理的内部故障，必须让 Run 失败而非伪装成命令失败。"""


async def terminate_group(process: asyncio.subprocess.Process) -> None:
    """终止独立进程组，再等待直接子进程退出；即使 shell 已退出也处理组内子进程。"""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        await asyncio.sleep(0.25)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError as exc:
        raise OutputCaptureError("无法在清理期限内确认子进程退出") from exc


async def run_bash(args: BashArgs, context: ToolContext) -> ToolOutput:
    """执行命令、并发读取两个输出通道、保存有界产物，并在所有出口清理进程组。

    普通非零退出和工具超时返回 ToolOutput；外部取消完成清理后继续向上传播。
    """
    token = uuid4().hex
    stdout_path = context.output_dir / f"{token}.stdout.log"
    stderr_path = context.output_dir / f"{token}.stderr.log"
    try:
        context.output_dir.mkdir(parents=True, exist_ok=True)
        stdout_file = stdout_path.open("xb")
        try:
            stderr_file = stderr_path.open("xb")
        except BaseException:
            stdout_file.close()
            raise
    except OSError as exc:
        raise OutputCaptureError("无法创建命令输出文件") from exc
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    captured = 0

    async def pump(
        reader: asyncio.StreamReader, output: BinaryIO, channel: str
    ) -> None:
        """读取一个通道，保存原始字节并发布解码增量；内存中仅保留有界尾部。"""
        nonlocal captured
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                rest = decoder.decode(b"", final=True)
                if rest:
                    context.on_output(f"[{channel}] {rest}")
                return
            exceeded = captured + len(chunk) > CAPTURE_BYTES
            chunk = chunk[: max(0, CAPTURE_BYTES - captured)]
            try:
                output.write(chunk)
            except OSError as exc:
                raise OutputCaptureError("写入命令输出失败") from exc
            captured += len(chunk)
            tails[channel].extend(chunk)
            del tails[channel][:-TAIL_BYTES]
            delta = decoder.decode(chunk)
            if delta:
                context.on_output(f"[{channel}] {delta}")
            if exceeded:
                raise CaptureLimitError

    process: asyncio.subprocess.Process | None = None
    jobs: list[asyncio.Task[None]] = []
    failure: str | None = None
    primary: BaseException | None = None
    try:
        try:
            spawning = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    context.shell,
                    "-c",
                    args.command,
                    cwd=context.workspace,
                    env=dict(context.environment),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            )
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                # 取得已经启动的进程句柄后再传播取消，确保 finally 能清理它。
                try:
                    process = await spawning
                except OSError:
                    pass
                raise
        except OSError as exc:
            return ToolOutput(f"启动命令失败：{type(exc).__name__}", "bash_failed")
        if process.stdout is None or process.stderr is None:
            raise OutputCaptureError("子进程未提供预期管道")
        jobs = [
            asyncio.create_task(pump(process.stdout, stdout_file, "stdout")),
            asyncio.create_task(pump(process.stderr, stderr_file, "stderr")),
        ]
        try:
            async with asyncio.timeout(args.timeout_seconds):
                await asyncio.gather(*jobs)
                await process.wait()
        except TimeoutError:
            failure = "bash_timeout"
        except CaptureLimitError:
            failure = "output_limit"
    except BaseException as exc:
        primary = exc
        raise
    finally:

        async def cleanup() -> None:
            """在后台清理进程组与输出任务，最后关闭文件，避免留下仍在执行的命令。"""
            try:
                if process is not None:
                    await terminate_group(process)
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                try:
                    stdout_file.close()
                finally:
                    stderr_file.close()

        cleaning = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleaning)
        except asyncio.CancelledError:
            await cleaning
            raise
        except Exception as exc:
            if primary is None:
                raise OutputCaptureError("命令清理或文件关闭失败") from exc
            logger.warning("cleanup failed while unwinding: %s", type(exc).__name__)
    if process is None:
        raise OutputCaptureError("进程未创建")
    code = failure or ("bash_failed" if process.returncode != 0 else None)
    stdout = tails["stdout"].decode("utf-8", errors="replace")
    stderr = tails["stderr"].decode("utf-8", errors="replace")
    complete = failure is None
    return ToolOutput(
        content=(
            f"exit_code={process.returncode}; outcome={code or 'success'}\n"
            f"stdout（末尾最多 {TAIL_BYTES} 字节）:\n{stdout}\n"
            f"stderr（末尾最多 {TAIL_BYTES} 字节）:\n{stderr}\n"
            f"输出文件：{stdout_path}；{stderr_path}\n"
            f"capture_complete={complete}"
        ),
        error_code=code,
        details={
            "exit_code": process.returncode,
            "pid": process.pid,
            "pgid": process.pid,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "captured_bytes": captured,
            "capture_complete": complete,
        },
    )
