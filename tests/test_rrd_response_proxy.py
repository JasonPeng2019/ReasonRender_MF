from __future__ import annotations

import json
import stat
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def _sse(text: str, *, include_tool_call: bool = False) -> bytes:
    events = [
        {"type": "response.created", "response": {"id": "resp-test"}},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "id": "msg-test",
                "content": [{"type": "output_text", "text": text}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp-test",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "id": "msg-test",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 100, "total_tokens": 110},
            },
        },
    ]
    if include_tool_call:
        events.insert(
            1,
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "name": "read_file",
                    "call_id": "call-test",
                    "arguments": "{}",
                },
            },
        )
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


class Upstream(ThreadingHTTPServer):
    def __init__(
        self, text: str, *, include_tool_call: bool = False, malformed_event: bool = False
    ) -> None:
        self.text = text
        self.include_tool_call = include_tool_call
        self.malformed_event = malformed_event
        super().__init__(("127.0.0.1", 0), UpstreamHandler)


class UpstreamHandler(BaseHTTPRequestHandler):
    @property
    def upstream(self) -> Upstream:
        return self.server  # type: ignore[return-value]

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = _sse(self.upstream.text, include_tool_call=self.upstream.include_tool_call)
        if self.upstream.malformed_event:
            body = b"event: response.output_item.done\ndata: {\n\n" + body
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _post(port: int, *, worker: bool = True) -> bytes:
    headers = {"Content-Type": "application/json"}
    if worker:
        headers["x-openai-subagent"] = "collab_spawn"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/ollama/rrd-demo-rrd-test-a-outer/v1/responses",
        data=b"{}",
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read()


def test_worker_response_proxy_replaces_raw_stream_only_after_summary_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_response_proxy import Server

    monkeypatch.setenv("RRD_TASK_COMPRESS_CHARS", "100")
    raw = "RAW-PRIVATE-WORKER-REPORT\n" + "finding\n" * 300
    upstream = Upstream(raw)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    runs = tmp_path / "runs"
    arm = runs / "rrd-demo/rrd-test/a"
    (arm / "bundle").mkdir(parents=True)
    (arm / "target").mkdir()
    bundle = runs / "rrd-demo/rrd-test/bundle/rrd_codex_hook.py"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("print('COMPRESSED FINDINGS WITH LINES')\n")
    proxy = Server(
        ("127.0.0.1", 0),
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        runs=runs,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        body = _post(proxy.server_port)
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    assert raw.encode() not in body
    assert b"COMPRESSED FINDINGS WITH LINES" in body
    assert b"receipt=" in body
    assert b'"total_tokens":110' in body
    event = json.loads((arm / "proxy-events.jsonl").read_text())
    assert event["event"] == "result_compress"
    raw_path = Path(event["raw_path"])
    assert raw_path.read_text() == raw
    assert stat.S_IMODE(raw_path.stat().st_mode) == 0o600


def test_worker_response_proxy_fails_open_with_the_original_stream(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_response_proxy import Server

    monkeypatch.setenv("RRD_TASK_COMPRESS_CHARS", "100")
    raw = "SECOND-RAW-WORKER-REPORT\n" + "finding\n" * 300
    upstream = Upstream(raw)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    runs = tmp_path / "runs"
    arm = runs / "rrd-demo/rrd-test/a"
    (arm / "target").mkdir(parents=True)
    bundle = runs / "rrd-demo/rrd-test/bundle/rrd_codex_hook.py"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("raise SystemExit(9)\n")
    proxy = Server(
        ("127.0.0.1", 0),
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        runs=runs,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        body = _post(proxy.server_port)
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    assert b"SECOND-RAW-WORKER-REPORT" in body
    event = json.loads((arm / "proxy-events.jsonl").read_text())
    assert event["event"] == "compress_fail_open"


def test_compression_ratio_includes_the_delivered_receipt_footer(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_response_proxy import Server

    monkeypatch.setenv("RRD_TASK_COMPRESS_CHARS", "100")
    raw = "BOUNDARY-WORKER-REPORT\n" + "finding\n" * 300
    upstream = Upstream(raw)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    runs = tmp_path / "runs"
    arm = runs / "rrd-demo/rrd-test/a"
    (arm / "target").mkdir(parents=True)
    bundle = runs / "rrd-demo/rrd-test/bundle/rrd_codex_hook.py"
    bundle.parent.mkdir(parents=True)
    almost_too_large = "S" * (int(len(raw) * 0.65) - 1)
    bundle.write_text(f"print({almost_too_large!r})\n")
    proxy = Server(
        ("127.0.0.1", 0),
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        runs=runs,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        body = _post(proxy.server_port)
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    assert body == _sse(raw)
    event = json.loads((arm / "proxy-events.jsonl").read_text())
    assert event["event"] == "compress_fail_open"
    assert "delivered result" in event["error"]


def test_raw_directory_failure_returns_the_original_worker_stream(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_response_proxy import Server

    monkeypatch.setenv("RRD_TASK_COMPRESS_CHARS", "100")
    raw = "RAW-PERSISTENCE-FAILURE\n" + "finding\n" * 300
    upstream = Upstream(raw)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    runs = tmp_path / "runs"
    arm = runs / "rrd-demo/rrd-test/a"
    arm.mkdir(parents=True)
    (arm / "raw-results").write_text("not a directory")
    proxy = Server(
        ("127.0.0.1", 0),
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        runs=runs,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        body = _post(proxy.server_port)
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    assert body == _sse(raw)
    event = json.loads((arm / "proxy-events.jsonl").read_text())
    assert event["event"] == "compress_fail_open"
    assert "raw persistence failed" in event["error"]


def test_worker_response_proxy_reuses_an_identical_raw_receipt_safely(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_response_proxy import Server

    monkeypatch.setenv("RRD_TASK_COMPRESS_CHARS", "100")
    raw = "RETRIED-WORKER-REPORT\n" + "finding\n" * 300
    upstream = Upstream(raw)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    runs = tmp_path / "runs"
    arm = runs / "rrd-demo/rrd-test/a"
    (arm / "target").mkdir(parents=True)
    bundle = runs / "rrd-demo/rrd-test/bundle/rrd_codex_hook.py"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("print('RETRIED REPORT SUMMARY')\n")
    proxy = Server(
        ("127.0.0.1", 0),
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        runs=runs,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        first = _post(proxy.server_port)
        second = _post(proxy.server_port)
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    assert b"RETRIED REPORT SUMMARY" in first and b"RETRIED REPORT SUMMARY" in second
    events = [json.loads(line) for line in (arm / "proxy-events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["result_compress", "result_compress"]
    assert len(list((arm / "raw-results").glob("proxy-*.txt"))) == 1


def test_root_response_bypasses_worker_compression(tmp_path: Path, monkeypatch) -> None:
    from contextmesh.scripts.rrd_response_proxy import Server

    monkeypatch.setenv("RRD_TASK_COMPRESS_CHARS", "10")
    raw = "ROOT-STREAM-MUST-NOT-BE-COMPRESSED" * 20
    upstream = Upstream(raw)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = Server(
        ("127.0.0.1", 0),
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        runs=tmp_path / "runs",
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        body = _post(proxy.server_port, worker=False)
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    assert body == _sse(raw)
    assert not list((tmp_path / "runs").rglob("proxy-events.jsonl"))


def test_worker_tool_call_response_bypasses_result_compression(tmp_path: Path, monkeypatch) -> None:
    from contextmesh.scripts.rrd_response_proxy import Server

    monkeypatch.setenv("RRD_TASK_COMPRESS_CHARS", "10")
    raw = "INTERMEDIATE-WORKER-TEXT" * 100
    upstream = Upstream(raw, include_tool_call=True)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = Server(
        ("127.0.0.1", 0),
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        runs=tmp_path / "runs",
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        body = _post(proxy.server_port)
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    assert body == _sse(raw, include_tool_call=True)
    assert not list((tmp_path / "runs").rglob("proxy-events.jsonl"))


def test_malformed_worker_sse_fails_open_without_partial_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_response_proxy import Server

    monkeypatch.setenv("RRD_TASK_COMPRESS_CHARS", "10")
    raw = "RAW-AFTER-MALFORMED-EVENT" * 100
    expected = b"event: response.output_item.done\ndata: {\n\n" + _sse(raw)
    upstream = Upstream(raw, malformed_event=True)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = Server(
        ("127.0.0.1", 0),
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        runs=tmp_path / "runs",
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        body = _post(proxy.server_port)
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    assert body == expected
    event = json.loads((tmp_path / "runs/rrd-demo/rrd-test/a/proxy-events.jsonl").read_text())
    assert event["event"] == "compress_fail_open"
    assert "malformed" in event["error"]


def test_oversized_worker_stream_is_forwarded_whole_instead_of_synthetic_502(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts import rrd_response_proxy

    monkeypatch.setattr(rrd_response_proxy, "MAX_RESPONSE_BYTES", 100)
    monkeypatch.setenv("RRD_TASK_COMPRESS_CHARS", "10")
    raw = "RAW-OVER-SIZE-CAP" * 100
    upstream = Upstream(raw)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = rrd_response_proxy.Server(
        ("127.0.0.1", 0),
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        runs=tmp_path / "runs",
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        body = _post(proxy.server_port)
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    assert body == _sse(raw)
    event = json.loads((tmp_path / "runs/rrd-demo/rrd-test/a/proxy-events.jsonl").read_text())
    assert event["event"] == "compress_fail_open"
    assert "size cap" in event["error"]


def test_proxy_evidence_write_is_best_effort(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_response_proxy import _append

    parent = tmp_path / "parent-is-a-file"
    parent.write_text("not a directory")
    assert _append(parent / "events.jsonl", {"event": "x"}) is False


def test_upstream_buffering_has_a_wall_clock_deadline() -> None:
    from contextmesh.scripts.rrd_response_proxy import _read_bounded

    class TrickleHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), TrickleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback deadline fixture
            f"http://127.0.0.1:{server.server_port}", timeout=1
        ) as response:
            with pytest.raises(TimeoutError, match="wall-clock"):
                _read_bounded(response, limit=1_000, timeout=0.1)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert elapsed < 0.5
