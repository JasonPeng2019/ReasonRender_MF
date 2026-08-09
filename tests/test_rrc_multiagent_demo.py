from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from rrc.orchestrator_contract import PlanSpecPacket
from rrc.store import SQLiteTemplateStore


def _packet() -> PlanSpecPacket:
    return PlanSpecPacket.from_dict(
        {
            "signature": "audit({handler}) -> findings",
            "slot_names": ["handler"],
            "plan": {
                "steps": [
                    "Read {handler} and every declared shared file exactly once.",
                    "Cross-reference every route and report concrete findings.",
                ],
                "invariants": [],
                "edges": ["Report malformed input and missing authorization checks."],
                "constraints": [],
            },
            "specification": (
                "Audit {handler} for input-validation, authorization, and error-handling bugs."
            ),
            "acceptance": [
                "Every route in {handler} is checked.",
                "Every issue has severity and a file:line reference.",
            ],
            "non_goals": ["Do not edit source files."],
            "write_paths": [],
            "read_first": [
                "{handler}",
                "src/models.js",
                "src/utils.js",
                "src/middleware.js",
            ],
        }
    )


def test_rrc_event_fifo_is_best_effort_and_nonblocking(tmp_path: Path) -> None:
    from rrc.multiagent_demo import _append_jsonl

    events = tmp_path / "events.jsonl"
    os.mkfifo(events)
    started = time.monotonic()
    _append_jsonl(events, {"event": "fail_open"})
    assert time.monotonic() - started < 0.25


class FakePlanner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, prompt: str, model: str) -> tuple[str, int]:
        self.calls.append((prompt, model))
        return json.dumps(_packet().to_dict()), 137


class MemoryCaseIndex:
    def __init__(self) -> None:
        self.ref: str | None = None
        self.indexed_shapes: list[str] = []

    def search(self, case_shape: str) -> list[tuple[str, float]]:
        return [] if self.ref is None else [(self.ref, 1.0)]

    def index(self, case_shape: str, external_ref: str) -> None:
        self.indexed_shapes.append(case_shape)
        self.ref = external_ref


def test_audit_assignment_uses_a_canonical_private_shape() -> None:
    from rrc.multiagent_demo import AUDIT_CASE_SHAPE, audit_task

    prompt = (
        "Please inspect src/handlers/auth.js. Fully read it and src/models.js, src/utils.js, "
        "and src/middleware.js, then report findings."
    )

    task, handler = audit_task("call-1", prompt)

    assert handler == "src/handlers/auth.js"
    assert task.case_shape == AUDIT_CASE_SHAPE
    assert task.slot_values == {"handler": "src/handlers/auth.js"}
    assert "auth.js" not in AUDIT_CASE_SHAPE


@pytest.mark.parametrize(
    "prompt",
    [
        "No handler was assigned.",
        "Compare src/handlers/auth.js with src/handlers/users.js.",
    ],
)
def test_audit_assignment_requires_exactly_one_handler(prompt: str) -> None:
    from rrc.multiagent_demo import audit_task

    with pytest.raises(ValueError, match="exactly one handler"):
        audit_task("bad", prompt)


def test_audit_policy_accepts_read_only_packets_and_rejects_write_paths() -> None:
    from rrc.multiagent_demo import AUDIT_POLICY, audit_task

    task, _ = audit_task("call-1", "Audit src/handlers/auth.js against the shared files.")

    assert AUDIT_POLICY.validate_packet(task, _packet())
    invalid = PlanSpecPacket.from_dict({**_packet().to_dict(), "write_paths": ["{handler}"]})
    with pytest.raises(ValueError, match="write_paths"):
        AUDIT_POLICY.validate_packet(task, invalid)


def test_audit_policy_rejects_implementation_instructions() -> None:
    from rrc.multiagent_demo import AUDIT_POLICY, audit_task

    task, _ = audit_task("call-1", "Audit src/handlers/auth.js against the shared files.")
    payload = _packet().to_dict()
    plan = payload["plan"]
    assert isinstance(plan, dict)
    plan["steps"] = [
        "Implement fixes in {handler}.",
        "Run tests and patch every discovered bug.",
    ]
    contradictory = PlanSpecPacket.from_dict(payload)

    with pytest.raises(ValueError, match="implementation instruction"):
        AUDIT_POLICY.validate_packet(task, contradictory)


@pytest.mark.parametrize(
    "instruction",
    [
        "Write source code for {handler} and commit it.",
        "Author a patch for {handler}.",
        "Repair and alter {handler}.",
        "Change the source implementation in {handler}.",
        "Generate new source code for {handler}.",
        "Produce and develop source code for {handler}.",
    ],
)
def test_audit_policy_rejects_source_writing_bypasses(instruction: str) -> None:
    from rrc.multiagent_demo import AUDIT_POLICY, audit_task

    task, _ = audit_task("call-1", "Audit src/handlers/auth.js against the shared files.")
    payload = _packet().to_dict()
    plan = payload["plan"]
    assert isinstance(plan, dict)
    plan["steps"] = [
        "Read and audit {handler}, then report findings.",
        instruction,
    ]

    with pytest.raises(ValueError, match="implementation instruction|allowed audit action"):
        AUDIT_POLICY.validate_packet(task, PlanSpecPacket.from_dict(payload))


def test_audit_policy_allows_add_and_remove_route_names() -> None:
    from rrc.multiagent_demo import AUDIT_POLICY, audit_task

    task, _ = audit_task("call-1", "Audit src/handlers/auth.js against the shared files.")
    payload = _packet().to_dict()
    plan = payload["plan"]
    assert isinstance(plan, dict)
    plan["steps"] = [
        "Read {handler} and inspect the add and remove routes.",
        "Cross-reference shared files and report findings.",
    ]

    assert AUDIT_POLICY.validate_packet(task, PlanSpecPacket.from_dict(payload))


def test_audit_policy_rejects_an_allowed_prefix_with_a_code_generation_suffix() -> None:
    from rrc.multiagent_demo import AUDIT_POLICY, audit_task

    task, _ = audit_task("call-1", "Audit src/handlers/auth.js against the shared files.")
    payload = _packet().to_dict()
    plan = payload["plan"]
    assert isinstance(plan, dict)
    plan["steps"] = [
        "Read and audit {handler}, then generate new source code for it.",
        "Cross-reference every route and report concrete findings.",
    ]

    with pytest.raises(ValueError, match="allowed audit action catalog"):
        AUDIT_POLICY.validate_packet(task, PlanSpecPacket.from_dict(payload))


def test_cold_assignments_each_plan_without_persisting(tmp_path: Path) -> None:
    from rrc.multiagent_demo import resolve_assignment

    planner = FakePlanner()
    index = MemoryCaseIndex()
    store = SQLiteTemplateStore(tmp_path / "cold.sqlite")

    first = resolve_assignment(
        task_id="one",
        task_prompt="Audit src/handlers/auth.js against the shared files.",
        mode="cold",
        planner=planner,
        store=store,
        case_index=index,
        planner_model="fake",
    )
    second = resolve_assignment(
        task_id="two",
        task_prompt="A different wording for src/handlers/users.js.",
        mode="cold",
        planner=planner,
        store=store,
        case_index=index,
        planner_model="fake",
    )

    assert first.hit is second.hit is False
    assert first.planner_tokens == second.planner_tokens == 137
    assert len(planner.calls) == 2
    assert index.ref is None and index.indexed_shapes == []
    assert store.get_plan_spec(first.external_ref) is None
    assert "src/handlers/auth.js" not in planner.calls[0][0]
    assert "src/handlers/users.js" not in planner.calls[1][0]


def test_warm_assignments_miss_then_reuse_one_generic_packet(tmp_path: Path) -> None:
    from rrc.multiagent_demo import resolve_assignment

    planner = FakePlanner()
    index = MemoryCaseIndex()
    store = SQLiteTemplateStore(tmp_path / "warm.sqlite")

    first = resolve_assignment(
        task_id="one",
        task_prompt="Audit src/handlers/auth.js against the shared files.",
        mode="warm",
        planner=planner,
        store=store,
        case_index=index,
        planner_model="fake",
    )
    second = resolve_assignment(
        task_id="two",
        task_prompt="Different wording for src/handlers/users.js.",
        mode="warm",
        planner=planner,
        store=store,
        case_index=index,
        planner_model="fake",
    )

    assert first.hit is False and first.planner_tokens == 137
    assert second.hit is True and second.planner_tokens == 0
    assert first.external_ref == second.external_ref
    assert len(planner.calls) == 1
    assert "src/handlers/users.js" in json.dumps(second.rendered_packet)
    assert "src/handlers/auth.js" not in json.dumps(second.rendered_packet)
    assert index.indexed_shapes == [first.case_shape]


def test_exact_ref_visibility_waits_for_the_new_reference() -> None:
    from rrc.multiagent_demo import wait_for_external_ref

    class DelayedIndex:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, case_shape: str) -> list[tuple[str, float]]:
            del case_shape
            self.calls += 1
            return [("stale", 0.9)] if self.calls < 3 else [("wanted", 1.0)]

    index = DelayedIndex()
    wait_for_external_ref(index, "shape", "wanted", timeout=0.2, poll_interval=0)
    assert index.calls == 3


def test_sqlite_case_index_is_exact_round_scoped_and_process_persistent(tmp_path: Path) -> None:
    from rrc.multiagent_demo import SQLiteCaseIndex

    database = tmp_path / "packets.sqlite"
    SQLiteCaseIndex(database, "round-a").index("same shape", "ref-a")

    assert SQLiteCaseIndex(database, "round-a").search("same shape") == [("ref-a", 1.0)]
    assert SQLiteCaseIndex(database, "round-a").search("same shape ") == []
    assert SQLiteCaseIndex(database, "round-b").search("same shape") == []


def test_round_everos_case_store_round_trips_without_flush() -> None:
    from rrc.multiagent_demo import RoundEverOSCaseIndex

    records: dict[str, str] = {}
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            paths.append(self.path)
            if self.path.endswith("/add"):
                records[str(payload["session_id"])] = str(payload["messages"][0]["content"])
                body = {"data": {"status": "accumulated"}}
            else:
                key = str(payload["filters"]["session_id"])
                content = records.get(key)
                body = {
                    "data": {
                        "unprocessed_messages": [] if content is None else [{"content": content}]
                    }
                }
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        index = RoundEverOSCaseIndex(f"http://127.0.0.1:{server.server_port}", "round-a")
        index.index("same shape", "ref-a")
        assert index.search("same shape") == [("ref-a", 1.0)]
        assert index.search("other shape") == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert paths == [
        "/api/v2/memory/add",
        "/api/v2/memory/search",
        "/api/v2/memory/search",
    ]
    assert not any(path.endswith("/flush") for path in paths)


def test_planner_failure_persists_raw_process_evidence(tmp_path: Path) -> None:
    from rrc.multiagent_demo import CodexPacketPlanner, audit_task

    task, _ = audit_task("bad", "Audit src/handlers/auth.js against the shared files.")
    artifact = tmp_path / "planner.jsonl"

    def failed_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["codex"], 9, stdout="RAW STDOUT", stderr="RAW STDERR")

    planner = CodexPacketPlanner(
        model="fake",
        task=task,
        artifact_log=artifact,
        run=failed_run,
    )

    with pytest.raises(RuntimeError, match="exit code 9"):
        planner("generic prompt", "fake")

    event = json.loads(artifact.read_text())
    assert event["exit_code"] == 9
    assert event["stdout"] == "RAW STDOUT"
    assert event["stderr"] == "RAW STDERR"
    assert event["parse_status"] == "process_error"


def test_planner_passes_a_bounded_deadline_and_isolated_environment(tmp_path: Path) -> None:
    from rrc.multiagent_demo import CodexPacketPlanner, audit_task

    task, _ = audit_task("bounded", "Audit src/handlers/auth.js against the shared files.")
    artifact = tmp_path / "planner.jsonl"
    observed: dict[str, object] = {}

    def timed_out_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        timeout = kwargs["timeout"]
        assert isinstance(timeout, int | float)
        raise subprocess.TimeoutExpired(cmd=["codex"], timeout=float(timeout))

    planner = CodexPacketPlanner(
        model="fake",
        task=task,
        artifact_log=artifact,
        timeout=0.05,
        env={"CODEX_HOME": "/isolated/planner", "EXAMPLE_API_KEY": "not-logged"},
        run=timed_out_run,
    )

    with pytest.raises(TimeoutError, match="planner timed out"):
        planner("generic prompt", "fake")

    assert observed["timeout"] == 0.05
    assert observed["env"] == {"CODEX_HOME": "/isolated/planner"}
    event = json.loads(artifact.read_text())
    assert event["parse_status"] == "timeout"
    assert event["timeout_seconds"] == 0.05
    assert "not-logged" not in artifact.read_text()


def test_nonblocking_lock_deadline_does_not_wait_for_the_holder(tmp_path: Path) -> None:
    import fcntl

    from rrc.multiagent_demo import acquire_exclusive_lock

    lock = tmp_path / "packets.lock"
    with lock.open("a+") as holder, lock.open("a+") as follower:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="RRC packet lock"):
            acquire_exclusive_lock(follower, timeout=0.03, poll_interval=0.005)
        assert time.monotonic() - started < 0.25


def test_planner_normalizes_controller_owned_audit_fields(tmp_path: Path) -> None:
    from rrc.multiagent_demo import AUDIT_READ_FIRST, CodexPacketPlanner, audit_task

    task, _ = audit_task("ok", "Audit src/handlers/auth.js against the shared files.")
    raw_packet = _packet().to_dict()
    raw_packet["read_first"] = list(reversed(AUDIT_READ_FIRST))
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(raw_packet)},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                }
            ),
        )
    )

    def successful_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["codex"], 0, stdout=stdout, stderr="")

    artifact = tmp_path / "planner.jsonl"
    planner = CodexPacketPlanner(
        model="fake",
        task=task,
        artifact_log=artifact,
        run=successful_run,
    )
    response, tokens = planner("generic prompt", "fake")

    normalized = json.loads(response)
    assert normalized["slot_names"] == ["handler"]
    assert normalized["write_paths"] == []
    assert normalized["read_first"] == list(AUDIT_READ_FIRST)
    assert normalized["non_goals"] == ["Do not edit source files."]
    assert tokens == 15
    event = json.loads(artifact.read_text())
    assert event["response"] == json.dumps(raw_packet)
    assert json.loads(event["normalized_response"])["read_first"] == list(AUDIT_READ_FIRST)


def test_planner_preserves_usage_when_response_json_is_invalid(tmp_path: Path) -> None:
    from rrc.multiagent_demo import CodexPacketPlanner, audit_task

    task, _ = audit_task("bad-json", "Audit src/handlers/auth.js against the shared files.")
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "not-json"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                }
            ),
        )
    )

    def successful_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["codex"], 0, stdout=stdout, stderr="")

    artifact = tmp_path / "planner.jsonl"
    planner = CodexPacketPlanner(model="fake", task=task, artifact_log=artifact, run=successful_run)

    with pytest.raises(ValueError, match="not a JSON object"):
        planner("generic prompt", "fake")

    event = json.loads(artifact.read_text())
    assert event["parse_status"] == "response_error"
    assert event["usage"]["total_tokens"] == 15


def test_four_parallel_warm_resolutions_make_one_planner_call(tmp_path: Path) -> None:
    state: dict[str, str | None] = {"content": None}
    state_lock = threading.Lock()

    class EverOSHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            with state_lock:
                if self.path.endswith("/add"):
                    state["content"] = payload["messages"][0]["content"]
                content = state["content"]
            body: dict[str, object] = {}
            if self.path.endswith("/search"):
                messages = [] if content is None else [{"content": content}]
                body = {"data": {"unprocessed_messages": messages}}
            elif self.path.endswith("/add"):
                body = {"data": {"status": "accumulated"}}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), EverOSHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        calls = tmp_path / "codex-calls"
        packet = json.dumps(_packet().to_dict())
        codex = fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            f"open({str(calls)!r}, 'a').write('call\\n')\n"
            f"packet = {packet!r}\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':packet}}))\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}}))\n"
        )
        codex.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        common = [
            sys.executable,
            "-m",
            "rrc.multiagent_demo",
            "resolve",
            "--mode",
            "warm",
            "--memory-backend",
            "everos",
            "--round-id",
            "parallel-test",
            "--database",
            str(tmp_path / "packets.sqlite"),
            "--lock",
            str(tmp_path / "packets.lock"),
            "--events",
            str(tmp_path / "events.jsonl"),
            "--model-events",
            str(tmp_path / "models.jsonl"),
            "--model",
            "fake",
            "--everos-url",
            f"http://127.0.0.1:{server.server_port}",
        ]
        handlers = ("auth", "users", "orders", "admin")
        processes = [
            subprocess.Popen(
                [
                    *common,
                    "--task-id",
                    name,
                    "--task-prompt",
                    f"Audit src/handlers/{name}.js against the shared files.",
                ],
                cwd=Path(__file__).parents[1],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for name in handlers
        ]
        completed = [process.communicate(timeout=15) for process in processes]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert [process.returncode for process in processes] == [0, 0, 0, 0], completed
    payloads = [json.loads(stdout) for stdout, _stderr in completed]
    assert sorted(payload["branch"] for payload in payloads) == ["hit", "hit", "hit", "miss"]
    assert calls.read_text().splitlines() == ["call"]
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 4


def test_local_cli_miss_then_hit_ignores_a_poison_everos_listener(tmp_path: Path) -> None:
    requests: list[str] = []

    class PoisonHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            requests.append(self.path)
            self.send_response(500)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), PoisonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        packet = json.dumps(_packet().to_dict())
        codex = fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"packet = {packet!r}\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':packet}}))\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}}))\n"
        )
        codex.chmod(0o755)
        common = [
            sys.executable,
            "-m",
            "rrc.multiagent_demo",
            "resolve",
            "--mode",
            "warm",
            "--memory-backend",
            "sqlite",
            "--round-id",
            "local-cli",
            "--database",
            str(tmp_path / "packets.sqlite"),
            "--lock",
            str(tmp_path / "packets.lock"),
            "--events",
            str(tmp_path / "events.jsonl"),
            "--model-events",
            str(tmp_path / "models.jsonl"),
            "--model",
            "fake",
            "--everos-url",
            f"http://127.0.0.1:{server.server_port}",
        ]
        env = {**os.environ, "RRD_CODEX_BIN": str(codex)}
        outputs = []
        for name in ("auth", "users"):
            result = subprocess.run(
                [
                    *common,
                    "--task-id",
                    name,
                    "--task-prompt",
                    f"Audit src/handlers/{name}.js against the shared files.",
                ],
                cwd=Path(__file__).parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            outputs.append(json.loads(result.stdout))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert [payload["branch"] for payload in outputs] == ["miss", "hit"]
    assert all(payload["memory_backend"] == "sqlite" for payload in outputs)
    assert requests == []
