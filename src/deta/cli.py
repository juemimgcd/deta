import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from deta import __version__
from deta.events import Event, TextDelta
from deta.model import ModelConfig, open_model, stream_once
from deta.observability.artifacts import Artifacts
from deta.observability.tracing import local_tracing


def show(event: Event) -> None:
    """接收 emit 发来的事件，将 TextDelta 的新增正文立即打印到终端。
    其他事件不显示，函数返回 None，不负责保存消息或判断请求是否成功。
    """
    if isinstance(event, TextDelta):
        print(event.text, end="", flush=True)


async def request(prompt: str, capture_body: bool) -> int:
    """接收用户问题与正文采集开关，读取环境配置并组装模型客户端、Trace 和产物采集器。
    等待一次 stream_once，显示结束原因与诊断目录，再把退出码返回给 main。
    """
    model = os.environ.get("OPENAI_MODEL", "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not model or not key:
        raise ValueError("请设置 OPENAI_MODEL 和 OPENAI_API_KEY")
    config = ModelConfig(model=model, api_key=SecretStr(key))
    run_id = uuid4().hex
    root = Path(".deta/runs") / run_id
    artifacts = Artifacts(
        root / "artifacts",
        capture_body=capture_body,
        # 对写入产物的字符串遮住本次 API key，再交给采集器保存。
        redact=lambda value: value.replace(key, "[REDACTED_API_KEY]"),
    )
    with local_tracing(root / "spans.jsonl") as tracer:
        with tracer.start_as_current_span(
            "deta.request_probe",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("deta.run_id", run_id)
            span.set_attribute("deta.mode", "single_request")
            async with open_model(config) as client:
                message = await stream_once(
                    client,
                    config,
                    "You are a helpful assistant.",
                    [HumanMessage(content=prompt)],
                    [],
                    tracer=tracer,
                    artifacts=artifacts,
                    listeners=[show],
                )
    print()
    print(
        f"stop_reason={message.response_metadata.get('finish_reason')}; usage={message.usage_metadata}",
        file=sys.stderr,
    )
    if message.additional_kwargs.get("refusal"):
        print(f"refusal={message.additional_kwargs.get('refusal')}", file=sys.stderr)
    print(f"diagnostics={root}", file=sys.stderr)
    return 0 if message.response_metadata.get("finish_reason") == "stop" else 1


def main() -> int:
    """作为命令入口解析问题和采集开关，通过 asyncio.run 启动单次模型请求。
    将请求的退出码返回给启动器，并将取消或异常转换成相应退出码和简洁提示。
    """
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="deta", description="Deta 单次模型请求")
    parser.add_argument("--version", action="version", version=f"deta {__version__}")
    parser.add_argument("-p", "--prompt")
    parser.add_argument("--capture-body", action="store_true")
    args = parser.parse_args()
    if args.prompt is None:
        parser.print_help()
        return 0
    if not args.prompt.strip():
        parser.error("prompt 不能为空白")
    try:
        return asyncio.run(request(args.prompt, args.capture_body))
    except KeyboardInterrupt:
        print("请求已取消", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"请求失败：{type(exc).__name__}", file=sys.stderr)
        if isinstance(exc, ValueError):
            print("检查必填配置与参数；未输出原始异常正文。", file=sys.stderr)
        return 1
