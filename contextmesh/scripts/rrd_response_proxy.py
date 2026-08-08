#!/usr/bin/env python3
"""Loopback Responses proxy that safely compresses completed Codex worker reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SESSION_RE = re.compile(
    r"^/ollama/rrd-demo-(?P<round>rrd-[A-Za-z0-9_-]+)-(?P<arm>[ab])-outer/v1/responses$"
)
MAX_RESPONSE_BYTES = 20_000_000


def _append(path: Path, event: dict[str, object]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while recording proxy evidence")
                view = view[written:]
        finally:
            os.close(descriptor)
    except Exception:
        return False
    return True


def _source_socket(source: Any) -> Any:
    candidates = [
        getattr(getattr(getattr(source, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(source, "fp", None), "_sock", None),
    ]
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def _deadline_timer(source: Any, timeout: float) -> threading.Timer | None:
    sock = _source_socket(source)
    if sock is None:
        return None

    def abort() -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(max(0.001, timeout), abort)
    timer.daemon = True
    timer.start()
    return timer


def _read_bounded(source: Any, *, limit: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    total = 0
    timer = _deadline_timer(source, timeout)
    try:
        while total <= limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("upstream response exceeded wall-clock deadline")
            chunk = source.read(min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError as exc:
        if time.monotonic() >= deadline:
            raise TimeoutError("upstream response exceeded wall-clock deadline") from exc
        raise
    finally:
        if timer is not None:
            timer.cancel()
    return b"".join(chunks)


def _sse_events(body: bytes) -> list[dict[str, Any]] | None:
    events: list[dict[str, Any]] = []
    try:
        decoded = body.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return None
    for block in decoded.split("\n\n"):
        data = "\n".join(line[6:] for line in block.splitlines() if line.startswith("data: "))
        if not data or data == "[DONE]":
            continue
        try:
            value = json.loads(data)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        events.append(value)
    return events


def _message(events: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
    found: tuple[dict[str, Any], str] | None = None
    for event in events:
        if event.get("type") != "response.output_item.done":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"message", "reasoning"}:
            # Replacing a response that also asks Codex to run a tool would
            # discard that call and break the worker. Only terminal text turns
            # are eligible for result compression.
            return None
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        text = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"output_text", "text"}
            and isinstance(part.get("text"), str)
        )
        if text:
            found = (item, text)
    return found


def _replace_text(value: Any, raw: str, delivered: str) -> Any:
    if isinstance(value, str):
        return value.replace(raw, delivered)
    if isinstance(value, list):
        return [_replace_text(item, raw, delivered) for item in value]
    if isinstance(value, dict):
        return {key: _replace_text(item, raw, delivered) for key, item in value.items()}
    return value


def _synthesized(
    events: list[dict[str, Any]], item: dict[str, Any], raw: str, delivered: str
) -> bytes:
    created = next((event for event in events if event.get("type") == "response.created"), None)
    completed = next(
        (event for event in reversed(events) if event.get("type") == "response.completed"), None
    )
    if not isinstance(created, dict) or not isinstance(completed, dict):
        raise ValueError("stream lacks response.created/response.completed")
    replacement = {
        **item,
        "content": [{"type": "output_text", "text": delivered}],
    }
    sequence = [
        _replace_text(created, raw, delivered),
        {"type": "response.output_item.done", "item": replacement},
        _replace_text(completed, raw, delivered),
    ]
    body = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
        for event in sequence
    ).encode()
    if raw.encode() in body:
        raise ValueError("synthesized stream still contains the raw worker result")
    return body


class Server(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], *, upstream: str, runs: Path) -> None:
        super().__init__(address, Handler)
        self.upstream = upstream.rstrip("/")
        self.runs = runs


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> Server:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 20_000_000:
            self.send_error(413)
            return
        self.connection.settimeout(15)
        request_body = self.rfile.read(length)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}
        }
        headers["Accept-Encoding"] = "identity"
        request = urllib.request.Request(
            self.app.upstream + self.path,
            data=request_body,
            headers=headers,
            method="POST",
        )
        match = SESSION_RE.match(self.path)
        is_worker = bool(self.headers.get("x-openai-subagent"))
        upstream_timeout = float(os.environ.get("RRD_PROXY_UPSTREAM_TIMEOUT", "150"))
        upstream_deadline = time.monotonic() + upstream_timeout
        try:
            with urllib.request.urlopen(request, timeout=upstream_timeout) as response:  # noqa: S310
                status = response.status
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                if not (
                    status == 200 and match and is_worker and "text/event-stream" in content_type
                ):
                    self._stream_reply(
                        status,
                        content_type,
                        response,
                        timeout=max(0.001, upstream_deadline - time.monotonic()),
                    )
                    return
                response_body = _read_bounded(
                    response,
                    limit=MAX_RESPONSE_BYTES,
                    timeout=max(0.001, upstream_deadline - time.monotonic()),
                )
                if len(response_body) > MAX_RESPONSE_BYTES:
                    _append(
                        self.app.runs
                        / "rrd-demo"
                        / match.group("round")
                        / match.group("arm")
                        / "proxy-events.jsonl",
                        {
                            "v": 1,
                            "ts": time.time(),
                            "event": "compress_fail_open",
                            "error": "worker response exceeded compression size cap",
                        },
                    )
                    self._stream_reply(
                        status,
                        content_type,
                        response,
                        prefix=response_body,
                        timeout=max(0.001, upstream_deadline - time.monotonic()),
                    )
                    return
        except urllib.error.HTTPError as exc:
            status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json")
            try:
                response_body = _read_bounded(
                    exc,
                    limit=MAX_RESPONSE_BYTES,
                    timeout=max(0.001, upstream_deadline - time.monotonic()),
                )
            except OSError as read_exc:
                payload = json.dumps(
                    {"error": {"message": f"RRD upstream error body unavailable: {read_exc}"}}
                ).encode()
                self._reply(502, "application/json", payload)
                return
            if len(response_body) > MAX_RESPONSE_BYTES:
                self._stream_reply(
                    status,
                    content_type,
                    exc,
                    prefix=response_body,
                    timeout=max(0.001, upstream_deadline - time.monotonic()),
                )
                return
        except (OSError, urllib.error.URLError) as exc:
            payload = json.dumps(
                {"error": {"message": f"RRD upstream unavailable: {exc}"}}
            ).encode()
            self._reply(502, "application/json", payload)
            return
        if status == 200 and match and is_worker and "text/event-stream" in content_type:
            try:
                response_body = self._compress(match, response_body)
            except Exception as exc:
                arm_dir = self.app.runs / "rrd-demo" / match.group("round") / match.group("arm")
                _append(
                    arm_dir / "proxy-events.jsonl",
                    {
                        "v": 1,
                        "ts": time.time(),
                        "event": "compress_fail_open",
                        "error": f"internal compression failure: {type(exc).__name__}: {exc}",
                    },
                )
        self._reply(status, content_type, response_body)

    def _compress(self, match: re.Match[str], body: bytes) -> bytes:
        round_id, arm = match.group("round"), match.group("arm")
        arm_dir = self.app.runs / "rrd-demo" / round_id / arm
        events = _sse_events(body)
        if events is None:
            _append(
                arm_dir / "proxy-events.jsonl",
                {
                    "v": 1,
                    "ts": time.time(),
                    "event": "compress_fail_open",
                    "error": "worker SSE was malformed",
                },
            )
            return body
        found = _message(events)
        if found is None:
            return body
        item, raw = found
        threshold = int(os.environ.get("RRD_TASK_COMPRESS_CHARS", "2000"))
        if len(raw) <= threshold:
            return body
        receipt = hashlib.sha256(raw.encode()).hexdigest()[:20]
        raw_dir = arm_dir / "raw-results"
        raw_path = raw_dir / f"proxy-{receipt}.txt"
        created = False
        try:
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_bytes = raw.encode()
            try:
                descriptor = os.open(raw_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if raw_path.read_bytes() != raw_bytes:
                    raise OSError("existing raw receipt does not match completed result")
            else:
                created = True
                try:
                    view = memoryview(raw_bytes)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("short write while preserving raw result")
                        view = view[written:]
                finally:
                    os.close(descriptor)
            if raw_path.stat().st_mode & 0o777 != 0o600:
                os.chmod(raw_path, 0o600)
        except Exception as exc:
            _append(
                arm_dir / "proxy-events.jsonl",
                {
                    "v": 1,
                    "ts": time.time(),
                    "event": "compress_fail_open",
                    "receipt": receipt,
                    "error": f"raw persistence failed: {type(exc).__name__}: {exc}",
                },
            )
            if created:
                try:
                    raw_path.unlink()
                except OSError:
                    pass
            return body
        bundle = self.app.runs / "rrd-demo" / round_id / "bundle" / "rrd_codex_hook.py"
        env = {
            **os.environ,
            "RRD_TARGET_ROOT": str(arm_dir / "target"),
            "RRD_SUMMARIZER_CODEX_HOME": str(arm_dir / "summarizer-home"),
        }
        try:
            process = subprocess.Popen(  # noqa: S603
                ["python3", str(bundle), "summarize", "--input", str(raw_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=arm_dir / "target",
                start_new_session=True,
            )
            try:
                summary, stderr = process.communicate(
                    timeout=float(os.environ.get("RRD_SUMMARIZER_TIMEOUT", "90")) + 5
                )
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise TimeoutError("summarizer timed out")
            if process.returncode != 0:
                raise RuntimeError(stderr[-500:])
            summary = summary.strip()
            if not summary:
                raise ValueError("summarizer returned an empty result")
            delivered = (
                summary
                + f"\n\n[ContextMesh] Full worker report preserved locally; receipt={receipt}."
            )
            if len(delivered) >= len(raw) * 0.65:
                raise ValueError("delivered result was not meaningfully smaller")
            replaced = _synthesized(events, item, raw, delivered)
        except Exception as exc:
            _append(
                arm_dir / "proxy-events.jsonl",
                {
                    "v": 1,
                    "ts": time.time(),
                    "event": "compress_fail_open",
                    "receipt": receipt,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return body
        _append(
            arm_dir / "proxy-events.jsonl",
            {
                "v": 1,
                "ts": time.time(),
                "event": "result_compress",
                "receipt": receipt,
                "raw_chars": len(raw),
                "compressed_chars": len(delivered),
                "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "delivered_sha256": hashlib.sha256(delivered.encode()).hexdigest(),
                "raw_path": str(raw_path),
            },
        )
        return replaced

    def _reply(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _stream_reply(
        self,
        status: int,
        content_type: str,
        source: Any,
        *,
        prefix: bytes = b"",
        timeout: float,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            content_length = source.headers.get("Content-Length")
            if isinstance(content_length, str) and content_length.isdigit():
                self.send_header("Content-Length", content_length)
            self.send_header("Connection", "close")
            self.end_headers()
            if prefix:
                self.wfile.write(prefix)
                self.wfile.flush()
            deadline = time.monotonic() + timeout
            timer = _deadline_timer(source, timeout)
            try:
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("upstream stream exceeded wall-clock deadline")
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            finally:
                if timer is not None:
                    timer.cancel()
        except Exception:
            # Headers may already be on the wire, so a second synthetic response
            # would corrupt SSE. Closing is the only safe bounded behavior.
            self.close_connection = True

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--upstream", default="http://127.0.0.1:8789")
    parser.add_argument("--runs", type=Path, required=True)
    args = parser.parse_args()
    Server((args.host, args.port), upstream=args.upstream, runs=args.runs).serve_forever()


if __name__ == "__main__":
    main()
