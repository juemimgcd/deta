"""Optional passive Eyes exporter. Network failures never control the Agent loop."""

import fcntl
import hashlib
import json
import logging
import os
import queue
import threading
import time
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import get_current_span

from deta.events import AgentEvent, Event, ModelDone

logger = logging.getLogger(__name__)
MAX_DISK_BYTES = 64 * 1024 * 1024
SENSITIVE = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "token",
    "secret",
    "access_token",
    "refresh_token",
    "cookie",
    "set_cookie",
    "x_api_key",
}


class EyesObserver(SpanProcessor):
    def __init__(
        self,
        url: str,
        token: str,
        session_id: str,
        model: str,
        workspace: Path,
        *,
        capture_body: bool,
        model_key: str,
    ) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
        ):
            raise ValueError(
                "EYES_OBSERVATION_URL must be an HTTP(S) URL without credentials"
            )
        if parsed.query or parsed.fragment:
            raise ValueError(
                "EYES_OBSERVATION_URL must not contain a query or fragment"
            )
        self.url = url.rstrip("/")
        self.token = token
        self.local = not token
        if self.local:
            try:
                is_local = (
                    parsed.hostname == "localhost"
                    or ip_address(parsed.hostname).is_loopback
                )
            except ValueError:
                is_local = False
            if not is_local:
                raise ValueError("remote observation requires a token")
        self.endpoint = "/v1/observation/local" if self.local else "/v1/observation"
        self.session_id, self.model = session_id, model
        self.capture_body = capture_body
        self.secrets = (token, model_key)
        scope = hashlib.sha256((self.url + "\0" + token).encode()).hexdigest()[:24]
        self.root = workspace / ".deta" / "eyes" / scope
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_file = (self.root / "producer.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise
        identity_path = self.root / "source-id"
        if self.local and not identity_path.exists():
            identity_path.write_text(str(uuid4()))
            identity_path.chmod(0o600)
        self.headers = (
            {"X-Eyes-Local-Source": identity_path.read_text().strip()}
            if self.local
            else {"Authorization": "Bearer " + token}
        )
        self.pending = self.root / "pending"
        self.pending.mkdir(exist_ok=True, mode=0o700)
        self.rejected = self.root / "rejected"
        self.rejected.mkdir(exist_ok=True, mode=0o700)
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        self.lock = threading.RLock()
        self.disk_lock = threading.Lock()
        self.tool_started: dict[str, float] = {}
        self.stopping = threading.Event()
        self.shutdown_deadline = float("inf")
        self.current: str | None = None
        self.turn: int | None = None
        self.sequences: dict[str, int] = {}
        self.dropped: dict[str, int] = {}
        self.disk_bytes = sum(p.stat().st_size for p in self.root.glob("*/*.json"))
        self.thread = threading.Thread(
            target=self._work, name="eyes-observation", daemon=True
        )
        self.thread.start()

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self.secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {
                k: "[REDACTED]"
                if k.lower().replace("-", "_") in SENSITIVE
                else self._clean(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._clean(item) for item in value]
        return value

    def _emit(
        self,
        kind: str,
        data: dict[str, Any],
        *,
        run_id: str | None = None,
        turn: int | None = None,
        tool_call_id: str | None = None,
        span: ReadableSpan | Span | None = None,
    ) -> None:
        try:
            with self.lock:
                run_id = run_id or self.current
                if run_id is None:
                    return
                seq = self.sequences.get(run_id, 0) + 1
                self.sequences[run_id] = seq
                if kind == "run_end":
                    data["dropped_events"] = self.dropped.get(run_id, 0)
                context = (
                    span.get_span_context()
                    if span
                    else get_current_span().get_span_context()
                )
                parent = span.parent if span else None
                event = {
                    "schema_version": "1.0",
                    "event_id": str(uuid4()),
                    "sequence": seq,
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "type": kind,
                    "turn": turn if turn is not None else self.turn,
                    "tool_call_id": tool_call_id,
                    "trace_id": f"{context.trace_id:032x}"
                    if context and context.is_valid
                    else None,
                    "span_id": f"{context.span_id:016x}"
                    if context and context.is_valid
                    else None,
                    "parent_span_id": f"{parent.span_id:016x}"
                    if parent and parent.is_valid
                    else None,
                    "data": self._clean(data),
                    "truncated": bool(data.get("body_truncated", False)),
                }
                encoded = json.dumps(event, ensure_ascii=False)
                if len(encoded.encode()) > 60000:
                    event["truncated"] = True
                    event["data"] = {
                        k: v
                        for k, v in event["data"].items()
                        if k
                        in {
                            "status",
                            "reason",
                            "dropped_events",
                            "name",
                            "duration_ms",
                            "usage",
                        }
                    }
                    event["data"]["preview"] = encoded[:6000]
                envelope = {
                    "schema_version": "1.0",
                    "session_id": self.session_id,
                    "run_id": run_id,
                    "agent": "Deta",
                    "model": self.model,
                    "capture_body": self.capture_body,
                    "events": [event],
                }
                try:
                    self.queue.put_nowait(envelope)
                except queue.Full:
                    self.dropped[run_id] = self.dropped.get(run_id, 0) + 1
                    logger.warning("Eyes observation queue full; event omitted")
        except Exception as exc:
            logger.warning("Eyes observation event unavailable: %s", type(exc).__name__)

    def __call__(self, event: Event) -> None:
        if not isinstance(event, AgentEvent) or event.kind in {
            "message_update",
            "tool_update",
        }:
            return
        with self.lock:
            if event.kind == "run_start":
                self.current, self.turn = event.run_id, None
            if event.kind == "turn_start":
                self.turn = event.turn
            data: dict[str, Any] = dict(event.data)
            if event.status:
                data["status"] = event.status
            if event.kind == "tool_start" and event.tool_call_id:
                self.tool_started[event.tool_call_id] = time.monotonic()
            if event.kind == "tool_end" and event.tool_call_id:
                started = self.tool_started.pop(event.tool_call_id, None)
                if started is not None:
                    data["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
            if event.kind == "message_end" and isinstance(event.model_event, ModelDone):
                message = event.model_event.message
                data = {
                    "text": message.text,
                    "tool_calls": message.tool_calls,
                    "finish_reason": message.response_metadata.get("finish_reason"),
                }
            if not self.capture_body:
                data = {
                    k: v
                    for k, v in data.items()
                    if k in {"name", "status", "reason", "finish_reason", "duration_ms"}
                }
            self._emit(
                event.kind,
                data,
                run_id=event.run_id,
                turn=event.turn,
                tool_call_id=event.tool_call_id,
            )
            if event.kind == "run_end":
                self.current, self.turn = None, None

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if span.name in {"deta.model.attempt", "deta.compaction"}:
            kind = (
                "model_start"
                if span.name == "deta.model.attempt"
                else "compaction_start"
            )
            self._emit(kind, {"name": span.name}, span=span)

    def on_end(self, span: ReadableSpan) -> None:
        try:
            attrs = dict(span.attributes or {})
            duration = ((span.end_time or 0) - (span.start_time or 0)) / 1_000_000
            data: dict[str, Any] = {
                "name": span.name,
                "duration_ms": round(duration, 2),
                "status": span.status.status_code.name,
                "error": span.status.description,
            }
            if span.name == "deta.model.attempt":
                data["usage"] = {
                    key: attrs.get("deta.usage." + key)
                    for key in ("input_tokens", "output_tokens", "total_tokens")
                }
                data["attempt"] = attrs.get("deta.attempt")
                data["finish_reason"] = attrs.get("deta.stop_reason")
                self._emit("model_end", data, span=span)
            elif span.name == "deta.compaction":
                data["reason"] = attrs.get("deta.compaction.reason")
                data["outcome"] = attrs.get("deta.compaction.outcome")
                self._emit("compaction_end", data, span=span)
            elif span.name == "deta.model.input" and self.capture_body:
                for field in ("request", "response"):
                    path = attrs.get(f"deta.{field}_artifact")
                    if isinstance(path, str):
                        with Path(path).open("rb") as stream:
                            raw = stream.read(65537)
                        data[field] = (
                            json.loads(raw)
                            if len(raw) <= 65536
                            else "[body exceeds 64 KiB]"
                        )
                        if len(raw) > 65536:
                            data["body_truncated"] = True
                self._emit("model_input", data, span=span)
        except Exception as exc:
            logger.warning("Eyes span observation unavailable: %s", type(exc).__name__)

    def _persist(self, envelope: dict[str, Any]) -> None:
        with self.disk_lock:
            self._write_envelope(envelope)

    def _write_envelope(self, envelope: dict[str, Any]) -> None:
        raw = json.dumps(envelope, ensure_ascii=False).encode()
        if self.disk_bytes + len(raw) > MAX_DISK_BYTES:
            self.dropped[envelope["run_id"]] = (
                self.dropped.get(envelope["run_id"], 0) + 1
            )
            logger.warning("Eyes outbox full; event omitted")
            return
        path = self.pending / f"{time.time_ns()}-{uuid4().hex}.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("xb") as output:
            os.chmod(temporary, 0o600)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        self.disk_bytes += len(raw)

    def _work(self) -> None:
        retry_at = heartbeat_at = 0.0
        try:
            with httpx.Client(
                timeout=2,
                trust_env=False,
                follow_redirects=False,
                headers=self.headers,
            ) as client:
                while True:
                    try:
                        envelope = self.queue.get(timeout=0.2)
                    except queue.Empty:
                        envelope = None
                    if envelope is not None:
                        try:
                            self._persist(envelope)
                        except Exception as exc:
                            logger.warning(
                                "Eyes outbox write failed: %s", type(exc).__name__
                            )
                        finally:
                            self.queue.task_done()
                    if time.monotonic() >= retry_at:
                        for path in sorted(self.pending.glob("*.json"))[:50]:
                            if time.monotonic() >= self.shutdown_deadline:
                                break
                            try:
                                raw = path.read_bytes()
                                response = client.post(
                                    self.url + self.endpoint + "/events",
                                    content=raw,
                                    headers={"Content-Type": "application/json"},
                                )
                                if response.is_success:
                                    expected = json.loads(raw)["events"][0]["event_id"]
                                    if expected not in response.json().get(
                                        "accepted", []
                                    ):
                                        raise ValueError(
                                            "missing durable event acknowledgment"
                                        )
                                    with self.disk_lock:
                                        path.unlink()
                                        self.disk_bytes -= len(raw)
                                elif response.status_code in {
                                    400,
                                    401,
                                    403,
                                    404,
                                    409,
                                    413,
                                    422,
                                }:
                                    path.replace(self.rejected / path.name)
                                    logger.warning(
                                        "Eyes rejected observation (HTTP %s); retained locally",
                                        response.status_code,
                                    )
                                else:
                                    response.raise_for_status()
                            except Exception as exc:
                                logger.warning(
                                    "Eyes unavailable; buffered locally (%s)",
                                    type(exc).__name__,
                                )
                                retry_at = time.monotonic() + 3
                                break
                    if self.current and time.monotonic() >= heartbeat_at:
                        try:
                            client.post(
                                self.url + self.endpoint + "/heartbeat",
                                params={"run_id": self.current},
                            )
                        except httpx.HTTPError:
                            pass
                        heartbeat_at = time.monotonic() + 5
                    if self.stopping.is_set() and self.queue.empty():
                        if (
                            not any(self.pending.glob("*.json"))
                            or time.monotonic() >= self.shutdown_deadline
                            or retry_at > time.monotonic()
                        ):
                            break
        except Exception as exc:
            logger.warning("Eyes exporter stopped: %s", type(exc).__name__)
        finally:
            self.lock_file.close()

    def shutdown(self) -> None:
        if self.stopping.is_set():
            return
        self.shutdown_deadline = time.monotonic() + 2.5
        self.stopping.set()
        # Persist the remaining bounded memory queue before the CLI can exit, even
        # when the sender is currently waiting for an unavailable server.
        while True:
            try:
                envelope = self.queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._persist(envelope)
            except Exception as exc:
                logger.warning("Eyes final outbox write failed: %s", type(exc).__name__)
            finally:
                self.queue.task_done()
        self.thread.join(timeout=3)
        pending = len(list(self.pending.glob("*.json"))) + self.queue.qsize()
        if pending:
            logger.warning(
                "Eyes observation pending: %s; restart Deta to resume disk uploads",
                pending,
            )

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.queue.empty() and not any(self.pending.glob("*.json"))


def from_environment(
    session_id: str, model: str, workspace: Path, model_key: str
) -> EyesObserver | None:
    if os.environ.get("EYES_OBSERVATION_ENABLED", "true").lower() == "false":
        return None
    url = os.environ.get("EYES_OBSERVATION_URL", "").strip() or "http://127.0.0.1:8000"
    token = os.environ.get("EYES_OBSERVATION_TOKEN", "").strip()
    if not url:
        logger.warning("Eyes requires EYES_OBSERVATION_URL; disabled")
        return None
    try:
        return EyesObserver(
            url,
            token,
            session_id,
            model,
            workspace,
            capture_body=os.environ.get("EYES_CAPTURE_BODY", "false").lower() == "true",
            model_key=model_key,
        )
    except Exception as exc:
        logger.warning("Eyes observation disabled: %s", type(exc).__name__)
        return None
