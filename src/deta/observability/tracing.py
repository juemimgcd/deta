import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Tracer, get_current_span
from pydantic import JsonValue

from deta.events import AgentEvent, Event, Listener
from deta.observability.artifacts import Artifacts

logger = logging.getLogger(__name__)


@contextmanager
def local_tracing(path: Path) -> Iterator[Tracer]:
    """根据 path 创建本地 Span 导出环境，在 with 语句中把 Tracer 交给调用方。
    退出时启动后台收尾并最多等待一秒，将导出资源的生命周期限制在这个上下文内。
    """
    provider = TracerProvider(
        resource=Resource.create({"service.name": "deta"}),
        shutdown_on_exit=False,
    )
    output = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        output = path.open("a", encoding="utf-8")
        exporter = ConsoleSpanExporter(
            out=output,
            # 将一个 Span 序列化为单行 JSON，便于逐行读取导出文件。
            formatter=lambda span: span.to_json(indent=None) + "\n",
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=256,
                max_export_batch_size=64,
                schedule_delay_millis=200,
                export_timeout_millis=1000,
            )
        )
    except OSError as exc:
        logger.warning("trace export unavailable: %s", type(exc).__name__)
    try:
        yield provider.get_tracer("deta", "0.1.0")
    finally:

        def shutdown() -> None:
            """关闭外层 local_tracing 创建的 provider，并在最后关闭输出文件。
            该函数由收尾线程调用，普通关闭异常只记录类型，返回 None。
            """
            try:
                provider.shutdown()
            except Exception as exc:
                logger.warning("trace shutdown failed: %s", type(exc).__name__)
            finally:
                if output is not None:
                    output.close()

        worker = threading.Thread(target=shutdown, daemon=True)
        worker.start()
        worker.join(timeout=1.0)
        if worker.is_alive():
            logger.warning("trace flush incomplete: shutdown timeout")


def event_record(event: AgentEvent) -> dict[str, JsonValue]:
    """在同步事件产生边界取得 OTel 身份；不可延迟到后台再读取当前 Span。"""
    context = get_current_span().get_span_context()
    return {
        "run_id": event.run_id,
        "trace_id": f"{context.trace_id:032x}" if context.is_valid else None,
        "span_id": f"{context.span_id:016x}" if context.is_valid else None,
        "event": event.model_dump(mode="json"),
    }


def artifact_listener(artifacts: Artifacts) -> Listener:
    """为 CLI、评测和 Python 调用方提供同一个同步事件记录入口。"""

    def record(event: Event) -> None:
        if isinstance(event, AgentEvent):
            artifacts.save("event", event_record(event))

    return record
