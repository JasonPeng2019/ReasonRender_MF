"""ReasonRenderCoding Plan+Spec augmentation for the ContextMesh worker demo."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, cast

from rrc.everos import EverOSClient
from rrc.model import parse_codex_jsonl
from rrc.orchestrator_contract import (
    Complete,
    OrchestratorTask,
    PlanSpecPacket,
    PlanSpecTemplate,
)
from rrc.orchestrator_policy import (
    OrchestratorPolicy,
    PacketValidationError,
    PolicyDecision,
)
from rrc.orchestrator_runtime import OrchestratorRuntime
from rrc.store import SQLiteTemplateStore

HANDLER_RE = re.compile(r"(?<![A-Za-z0-9_.-])(src/handlers/[A-Za-z0-9_-]+\.js)\b")
AUDIT_READ_FIRST = (
    "{handler}",
    "src/models.js",
    "src/utils.js",
    "src/middleware.js",
)
AUDIT_CASE_SHAPE = """Audit exactly {handler} for input-validation, authorization, and error-handling bugs.
Read the assigned handler plus src/models.js, src/utils.js, and src/middleware.js in full.
Cross-reference every route and every consumed request field against shared validators, helpers,
and middleware. Report one concise issue per line with high/medium/low severity and file:line.
This is read-only analysis: do not edit files and do not spawn another agent."""
AUDIT_SIGNATURE = "audit({handler}) -> findings"
AUDIT_PLAN_STEPS = (
    "Read {handler} and every declared shared file exactly once.",
    "Read and audit {handler}, then report findings.",
    "Read {handler} and inspect the add and remove routes.",
    "Audit every route and consumed request field in {handler}.",
    "Cross-reference every route and report concrete findings.",
    "Cross-reference shared files and report findings.",
    "Trace authorization, ownership, role, and state-transition checks.",
    "Report concrete findings with severity and file:line.",
)
AUDIT_PLAN_INVARIANTS = ("Keep the audit read-only.",)
AUDIT_PLAN_EDGES = (
    "Report malformed input and missing authorization checks.",
    "Check unhandled and over-broad error handling.",
    "Check request-body type coercion and missing required-field validation.",
)
AUDIT_PLAN_CONSTRAINTS = ("Do not infer validation without tracing the shared implementation.",)
AUDIT_SPECIFICATION = (
    "Audit {handler} for input-validation, authorization, and error-handling bugs."
)
AUDIT_ACCEPTANCE = (
    "Every route in {handler} is checked.",
    "Every issue has severity and a file:line reference.",
    "Every consumed request field is cross-referenced against shared validation.",
)


class PacketStore(Protocol):
    def get_plan_spec(self, external_ref: str) -> PlanSpecTemplate | None: ...

    def put_plan_spec(self, template: PlanSpecTemplate) -> None: ...


class CaseIndex(Protocol):
    def search(self, case_shape: str) -> list[tuple[str, float]]: ...

    def index(self, case_shape: str, external_ref: str) -> None: ...


class SearchIndex(Protocol):
    def search(self, case_shape: str) -> list[tuple[str, float]]: ...


class RunProcess(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
        env: Mapping[str, str] | None,
    ) -> subprocess.CompletedProcess[str]: ...


def _run_process_group(
    args: Sequence[str],
    *,
    capture_output: bool,
    text: bool,
    check: bool,
    timeout: float,
    env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    """Run a planner in its own process group and reap the whole group on timeout."""

    if not capture_output or not text or check:
        raise ValueError("bounded planner runner requires captured text with check=False")
    process = subprocess.Popen(  # noqa: S603 - argv is controller-owned
        list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=None if env is None else dict(env),
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            cmd=list(args), timeout=timeout, output=stdout, stderr=stderr
        ) from exc
    return subprocess.CompletedProcess(list(args), process.returncode, stdout, stderr)


class AuditPacketPolicy(OrchestratorPolicy):
    """Strict read/report-only variant of the generic Plan+Spec policy."""

    def planner_guidance(self, decision: PolicyDecision) -> str:
        return (
            super().planner_guidance(decision)
            + "\n\nThis worker performs a read-only security audit, not implementation. "
            + 'Return "write_paths": []. Use exactly these four read_first entries: '
            + json.dumps(list(AUDIT_READ_FIRST), separators=(",", ":"))
            + '. Include the exact non-goal "Do not edit source files." '
            + "Plan only reading, cross-referencing, and reporting; never instruct the worker "
            + "to implement, edit, patch, or run tests."
        )

    def validate_packet(
        self,
        task: OrchestratorTask,
        packet: PlanSpecPacket,
        decision: PolicyDecision | None = None,
    ) -> bool:
        if packet.write_paths:
            raise PacketValidationError("audit packets require empty write_paths")
        if tuple(packet.read_first) != AUDIT_READ_FIRST:
            raise PacketValidationError("audit packets require the canonical read_first list")
        if packet.non_goals != ("Do not edit source files.",):
            raise PacketValidationError("audit packets require the read-only non-goal")

        # Expose only a finite controller-owned action vocabulary. The planner
        # still selects and orders clauses, but cannot append a compound
        # source-writing imperative after an allowed prefix.
        catalog_fields = (
            ("plan.steps", packet.plan.steps, AUDIT_PLAN_STEPS),
            ("plan.invariants", packet.plan.invariants, AUDIT_PLAN_INVARIANTS),
            ("plan.edges", packet.plan.edges, AUDIT_PLAN_EDGES),
            ("plan.constraints", packet.plan.constraints, AUDIT_PLAN_CONSTRAINTS),
            ("acceptance", packet.acceptance, AUDIT_ACCEPTANCE),
        )
        if packet.signature != AUDIT_SIGNATURE or packet.specification != AUDIT_SPECIFICATION:
            raise PacketValidationError(
                "audit packet contains an implementation instruction or text outside the "
                "allowed audit action catalog"
            )
        for field_name, values, allowed in catalog_fields:
            if len(values) != len(set(values)) or any(value not in allowed for value in values):
                raise PacketValidationError(
                    f"{field_name} contains an implementation instruction or text outside the "
                    "allowed audit action catalog"
                )

        # Reuse the complete generic validation contract, substituting one
        # placeholder-only path solely for its implementation-oriented
        # non-empty write_paths precondition.
        bridge = PlanSpecPacket(
            signature=packet.signature,
            slot_names=packet.slot_names,
            plan=packet.plan,
            specification=packet.specification,
            acceptance=packet.acceptance,
            non_goals=packet.non_goals,
            write_paths=("{handler}",),
            read_first=packet.read_first,
        )
        super().validate_packet(task, bridge, decision)
        return True


AUDIT_POLICY = AuditPacketPolicy()


class _NoopPacketStore:
    def get_plan_spec(self, external_ref: str) -> PlanSpecTemplate | None:
        del external_ref
        return None

    def put_plan_spec(self, template: PlanSpecTemplate) -> None:
        del template
        return None


class _NoopCaseIndex:
    def search(self, case_shape: str) -> list[tuple[str, float]]:
        del case_shape
        return []

    def index(self, case_shape: str, external_ref: str) -> None:
        del case_shape, external_ref
        return None


@dataclass(frozen=True)
class AssignmentResolution:
    """One rendered packet ready to append to a native Codex worker task."""

    task_id: str
    handler: str
    case_shape: str
    hit: bool
    external_ref: str
    planner_tokens: int
    profile: str
    rendered_packet: Mapping[str, object]


def audit_task(task_id: str, task_prompt: str) -> tuple[OrchestratorTask, str]:
    """Extract one assignment while keeping LLM-authored wording out of reuse shape."""

    handlers = sorted(set(HANDLER_RE.findall(task_prompt)))
    if len(handlers) != 1:
        raise ValueError("worker task must name exactly one handler in src/handlers/*.js")
    handler = handlers[0]
    return (
        OrchestratorTask(
            task_id=task_id,
            family="http-handler-audit",
            text=task_prompt,
            oracle_tests="",
            case_shape=AUDIT_CASE_SHAPE,
            slot_values={"handler": handler},
        ),
        handler,
    )


def _worker_passthrough(prompt: str, _model: str) -> tuple[str, int]:
    return prompt, 0


def _rendered_packet(worker_output: str) -> Mapping[str, object]:
    prefix = "Product worker input:\n"
    if not worker_output.startswith(prefix):
        raise ValueError("RRC runtime returned an invalid worker packet envelope")
    payload = json.loads(worker_output[len(prefix) :])
    if not isinstance(payload, dict) or not isinstance(payload.get("packet"), dict):
        raise ValueError("RRC runtime returned no rendered packet")
    return cast(dict[str, object], payload["packet"])


def resolve_assignment(
    *,
    task_id: str,
    task_prompt: str,
    mode: str,
    planner: Complete,
    store: PacketStore,
    case_index: CaseIndex,
    planner_model: str,
) -> AssignmentResolution:
    """Resolve one audit packet through the existing RRC product controller."""

    if mode not in {"cold", "warm"}:
        raise ValueError(f"RRC multi-agent mode must be cold or warm, got {mode}")
    task, handler = audit_task(task_id, task_prompt)
    active_store: PacketStore = _NoopPacketStore() if mode == "cold" else store
    active_index: CaseIndex = _NoopCaseIndex() if mode == "cold" else case_index
    runtime = OrchestratorRuntime(
        planner,
        planner_model,
        _worker_passthrough,
        "native-codex-worker",
        cast(SQLiteTemplateStore, active_store),
        cast(EverOSClient, active_index),
        policy=AUDIT_POLICY,
    )
    result = runtime.run(task)
    return AssignmentResolution(
        task_id=task_id,
        handler=handler,
        case_shape=AUDIT_CASE_SHAPE,
        hit=result.hit,
        external_ref=result.external_ref,
        planner_tokens=result.planner_tokens,
        profile=result.profile,
        rendered_packet=_rendered_packet(result.worker_output),
    )


def packet_output_schema(task: OrchestratorTask) -> dict[str, object]:
    """Return the strict Structured Outputs schema for an audit packet."""

    decision = AUDIT_POLICY.decide(task)
    if decision.profile == "lean":
        steps = (1, 2)
        acceptance = (1, 2)
        invariants = (0, 0)
        edges = (1, 1)
        constraints = (0, 0)
    else:
        steps = (2, 4)
        acceptance = (2, 4)
        invariants = (1, 4)
        edges = (1, 4)
        constraints = (1, 4)

    def string_array(
        minimum: int, maximum: int, *, enum: Sequence[str] | None = None
    ) -> dict[str, object]:
        items: dict[str, object] = {"type": "string"}
        if enum is not None:
            items["enum"] = list(enum)
        return {
            "type": "array",
            "items": items,
            "minItems": minimum,
            "maxItems": maximum,
        }

    plan_properties = {
        "steps": string_array(*steps, enum=AUDIT_PLAN_STEPS),
        "invariants": string_array(*invariants, enum=AUDIT_PLAN_INVARIANTS),
        "edges": string_array(*edges, enum=AUDIT_PLAN_EDGES),
        "constraints": string_array(*constraints, enum=AUDIT_PLAN_CONSTRAINTS),
    }
    properties: dict[str, object] = {
        "signature": {"type": "string", "enum": [AUDIT_SIGNATURE]},
        "slot_names": string_array(1, 1, enum=("handler",)),
        "plan": {
            "type": "object",
            "properties": plan_properties,
            "required": list(plan_properties),
            "additionalProperties": False,
        },
        "specification": {"type": "string", "enum": [AUDIT_SPECIFICATION]},
        "acceptance": string_array(*acceptance, enum=AUDIT_ACCEPTANCE),
        "non_goals": string_array(1, 3, enum=("Do not edit source files.",)),
        "write_paths": string_array(0, 0),
        "read_first": string_array(4, 4, enum=AUDIT_READ_FIRST),
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _append_jsonl(path: Path, event: Mapping[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        descriptor = os.open(
            path,
            os.O_APPEND
            | os.O_CREAT
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    return
                view = view[written:]
        finally:
            os.close(descriptor)
    except Exception:
        # Evidence is best-effort and must not mask the planner result or its error.
        return


def _recover_codex_usage(stdout: str) -> dict[str, int] | None:
    """Recover provider usage even when the final response cannot be parsed."""

    recovered: dict[str, int] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, Mapping) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, Mapping):
            continue
        prompt = _nonnegative_integer(usage.get("input_tokens"))
        completion = _nonnegative_integer(usage.get("output_tokens"))
        total = _nonnegative_integer(usage.get("total_tokens"))
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        if total is None:
            continue
        recovered = {
            "prompt_tokens": prompt or 0,
            "completion_tokens": completion or 0,
            "total_tokens": total,
        }
    return recovered


def _nonnegative_integer(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _credential_free_environment(env: Mapping[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    blocked = re.compile(
        r"(API_?KEY|ACCESS_?TOKEN|SECRET|PASSWORD|CREDENTIAL|COOKIE|AUTH_?TOKEN)", re.I
    )
    return {name: value for name, value in env.items() if blocked.search(name) is None}


class CodexPacketPlanner:
    """One structured Codex planner completion with durable raw evidence."""

    def __init__(
        self,
        *,
        model: str,
        task: OrchestratorTask,
        artifact_log: str | Path,
        executable: str = "codex",
        timeout: float = 45.0,
        env: Mapping[str, str] | None = None,
        run: RunProcess = _run_process_group,
    ) -> None:
        if timeout <= 0:
            raise ValueError("planner timeout must be positive")
        self._model = model
        self._task = task
        self._artifact_log = Path(artifact_log)
        self._executable = executable
        self._timeout = timeout
        self._env = _credential_free_environment(env)
        self._run = run

    def __call__(self, prompt: str, model: str) -> tuple[str, int]:
        schema = packet_output_schema(self._task)
        command = [
            self._executable,
            "exec",
            "--strict-config",
            "-c",
            "features.hooks=false",
            "-c",
            "features.multi_agent=false",
            "--json",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            model or self._model,
        ]
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="rrc-audit-schema-") as directory:
            schema_path = Path(directory) / "packet.schema.json"
            schema_path.write_text(
                json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                result = self._run(
                    [*command, "--output-schema", str(schema_path), prompt],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self._timeout,
                    env=self._env,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                stderr = exc.stderr if isinstance(exc.stderr, str) else ""
                _append_jsonl(
                    self._artifact_log,
                    {
                        "ts": started,
                        "prompt": prompt,
                        "model": model,
                        "exit_code": None,
                        "stdout": stdout,
                        "stderr": stderr,
                        "parse_status": "timeout",
                        "timeout_seconds": self._timeout,
                    },
                )
                raise TimeoutError(f"Codex planner timed out after {self._timeout:.2f}s") from exc
            except OSError as exc:
                _append_jsonl(
                    self._artifact_log,
                    {
                        "ts": started,
                        "prompt": prompt,
                        "model": model,
                        "exit_code": None,
                        "stdout": "",
                        "stderr": str(exc),
                        "parse_status": "start_error",
                    },
                )
                raise RuntimeError(f"could not start codex exec: {exc}") from exc

        base_event: dict[str, object] = {
            "ts": started,
            "prompt": prompt,
            "model": model,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        recovered_usage = _recover_codex_usage(result.stdout)
        if recovered_usage is not None:
            base_event["usage"] = recovered_usage
        if result.returncode != 0:
            _append_jsonl(self._artifact_log, {**base_event, "parse_status": "process_error"})
            detail = f": {result.stderr.strip()}" if result.stderr.strip() else ""
            raise RuntimeError(f"codex exec failed with exit code {result.returncode}{detail}")
        try:
            response, usage = parse_codex_jsonl(result.stdout)
        except ValueError as exc:
            _append_jsonl(
                self._artifact_log,
                {**base_event, "parse_status": "jsonl_error", "parse_error": str(exc)},
            )
            raise
        try:
            payload = json.loads(response)
            if not isinstance(payload, dict):
                raise TypeError("planner response must be one JSON object")
            # These fields belong to the deterministic controller, not the
            # planner. Normalizing them prevents harmless ordering drift from
            # turning a valid audit plan into a strict-schema MISS.
            payload["slot_names"] = ["handler"]
            payload["write_paths"] = []
            payload["read_first"] = list(AUDIT_READ_FIRST)
            payload["non_goals"] = ["Do not edit source files."]
            normalized_response = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (json.JSONDecodeError, TypeError) as exc:
            _append_jsonl(
                self._artifact_log,
                {
                    **base_event,
                    "parse_status": "response_error",
                    "parse_error": str(exc),
                    "usage": recovered_usage
                    or {
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                    },
                },
            )
            raise ValueError(f"planner response was not a JSON object: {exc}") from exc
        _append_jsonl(
            self._artifact_log,
            {
                **base_event,
                "parse_status": "ok",
                "response": response,
                "normalized_response": normalized_response,
                "usage": {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                },
            },
        )
        return normalized_response, usage.total_tokens


def _case_shape_sha(case_shape: str) -> str:
    return hashlib.sha256(case_shape.encode()).hexdigest()


class SQLiteCaseIndex:
    """Exact round-scoped case/ref mapping in the packet database."""

    def __init__(self, database: Path, round_id: str) -> None:
        self._database = database
        self._round_id = round_id
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rrd_case_index (
                    round_id TEXT NOT NULL,
                    case_shape_sha256 TEXT NOT NULL,
                    case_shape TEXT NOT NULL,
                    external_ref TEXT NOT NULL,
                    PRIMARY KEY (round_id, case_shape_sha256)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database, timeout=5)

    def search(self, case_shape: str) -> list[tuple[str, float]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT case_shape, external_ref FROM rrd_case_index
                WHERE round_id = ? AND case_shape_sha256 = ?
                """,
                (self._round_id, _case_shape_sha(case_shape)),
            ).fetchone()
        if row is None or row[0] != case_shape or not isinstance(row[1], str) or not row[1]:
            return []
        return [(row[1], 1.0)]

    def index(self, case_shape: str, external_ref: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO rrd_case_index
                    (round_id, case_shape_sha256, case_shape, external_ref)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(round_id, case_shape_sha256) DO UPDATE SET
                    case_shape = excluded.case_shape,
                    external_ref = excluded.external_ref
                """,
                (self._round_id, _case_shape_sha(case_shape), case_shape, external_ref),
            )


class RoundEverOSCaseIndex(EverOSClient):
    """Round-isolated exact EverOS record store with no extraction flush."""

    def __init__(self, base_url: str, round_id: str) -> None:
        super().__init__(base_url)
        safe_round = re.sub(r"[^A-Za-z0-9_-]", "-", round_id).strip("-")
        if not safe_round:
            raise ValueError("round_id must contain a letter or number")
        self.PROJECT_ID = f"rrc-demo-{safe_round[:80]}"
        self._round_id = round_id

    def _session_id(self, case_shape: str) -> str:
        return f"rrc:{self.PROJECT_ID}:{_case_shape_sha(case_shape)}"

    def index(self, case_shape: str, external_ref: str) -> None:
        record = {
            "v": 1,
            "round_id": self._round_id,
            "case_shape_sha256": _case_shape_sha(case_shape),
            "case_shape": case_shape,
            "external_ref": external_ref,
        }
        response = self._post(
            "/api/v2/memory/add",
            {
                "session_id": self._session_id(case_shape),
                "app_id": self.APP_ID,
                "project_id": self.PROJECT_ID,
                "messages": [
                    {
                        "role": "assistant",
                        "sender_id": self.USER_ID,
                        "timestamp": int(time.time() * 1000),
                        "content": json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                    }
                ],
            },
        )
        if response.get("data", {}).get("status") != "accumulated":
            raise RuntimeError("EverOS did not acknowledge RRD case record")

    def search(self, case_shape: str) -> list[tuple[str, float]]:
        response = self._post(
            "/api/v2/memory/search",
            {
                "user_id": self.USER_ID,
                "app_id": self.APP_ID,
                "project_id": self.PROJECT_ID,
                "query": "case-index",
                "method": "keyword",
                "filters": {"session_id": self._session_id(case_shape)},
            },
        )
        data = response.get("data")
        messages = data.get("unprocessed_messages") if isinstance(data, dict) else None
        for message in messages if isinstance(messages, list) else []:
            content = message.get("content") if isinstance(message, dict) else None
            try:
                record = json.loads(content) if isinstance(content, str) else None
            except json.JSONDecodeError:
                continue
            if (
                isinstance(record, dict)
                and record.get("v") == 1
                and record.get("round_id") == self._round_id
                and record.get("case_shape_sha256") == _case_shape_sha(case_shape)
                and record.get("case_shape") == case_shape
                and isinstance(record.get("external_ref"), str)
                and record["external_ref"]
            ):
                return [(str(record["external_ref"]), 1.0)]
        return []


# Compatibility name for callers that imported the earlier demo-only class.
RoundCaseIndex = RoundEverOSCaseIndex


def wait_for_external_ref(
    case_index: SearchIndex,
    case_shape: str,
    external_ref: str,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.25,
) -> None:
    """Wait until search can retrieve the exact ref just stored under the lock."""

    deadline = time.monotonic() + timeout
    while True:
        if any(ref == external_ref for ref, _score in case_index.search(case_shape)):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"RRC case index did not expose {external_ref} within {timeout:.1f}s"
            )
        time.sleep(min(max(poll_interval, 0.0), remaining))


def acquire_exclusive_lock(
    lock_stream: IO[str], *, timeout: float, poll_interval: float = 0.05
) -> float:
    """Acquire an advisory lock without allowing a dead leader to hang followers."""

    if timeout <= 0:
        raise ValueError("lock timeout must be positive")
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    while True:
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return time.monotonic() - started
        except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"RRC packet lock timed out after {timeout:.2f}s") from exc
            time.sleep(min(max(poll_interval, 0.0), remaining))


def _result_payload(result: AssignmentResolution) -> dict[str, object]:
    return {
        "task_id": result.task_id,
        "handler": result.handler,
        "case_shape": result.case_shape,
        "branch": "hit" if result.hit else "miss",
        "hit": result.hit,
        "external_ref": result.external_ref,
        "planner_tokens": result.planner_tokens,
        "profile": result.profile,
        "rendered_packet": result.rendered_packet,
    }


def _case_index_for(args: argparse.Namespace) -> CaseIndex:
    if args.memory_backend == "everos":
        if not args.everos_url:
            raise ValueError("--everos-url is required for the EverOS memory backend")
        return RoundEverOSCaseIndex(args.everos_url, args.round_id)
    return SQLiteCaseIndex(args.database, args.round_id)


def _resolve_cli(args: argparse.Namespace) -> int:
    task, _handler = audit_task(args.task_id, args.task_prompt)
    planner = CodexPacketPlanner(
        model=args.model,
        task=task,
        artifact_log=args.model_events,
        executable=os.environ.get("RRD_CODEX_BIN", "codex"),
        timeout=args.planner_timeout,
        env={
            **os.environ,
            **(
                {"CODEX_HOME": str(args.planner_codex_home)}
                if args.planner_codex_home is not None
                else {}
            ),
        },
    )
    try:
        if args.mode == "warm":
            Path(args.lock).parent.mkdir(parents=True, exist_ok=True)
            with Path(args.lock).open("a+") as lock_stream:
                lock_wait = acquire_exclusive_lock(lock_stream, timeout=args.lock_timeout)
                lock_wait_ms = round(lock_wait * 1000)
                store: PacketStore = SQLiteTemplateStore(args.database)
                case_index = _case_index_for(args)
                result = resolve_assignment(
                    task_id=args.task_id,
                    task_prompt=args.task_prompt,
                    mode=args.mode,
                    planner=planner,
                    store=store,
                    case_index=case_index,
                    planner_model=args.model,
                )
                if not result.hit:
                    wait_for_external_ref(
                        case_index,
                        result.case_shape,
                        result.external_ref,
                        timeout=args.visibility_timeout,
                    )
        else:
            lock_wait_ms = 0
            store = _NoopPacketStore()
            case_index = _NoopCaseIndex()
            result = resolve_assignment(
                task_id=args.task_id,
                task_prompt=args.task_prompt,
                mode=args.mode,
                planner=planner,
                store=store,
                case_index=case_index,
                planner_model=args.model,
            )
        payload = {
            **_result_payload(result),
            "mode": args.mode,
            "memory_backend": args.memory_backend,
            "lock_wait_ms": lock_wait_ms,
        }
        _append_jsonl(Path(args.events), {"ts": time.time(), "event": "packet", **payload})
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        _append_jsonl(
            Path(args.events),
            {
                "ts": time.time(),
                "event": "fail_open",
                "failure_id": args.task_id,
                "source": "python_bridge",
                "task_id": args.task_id,
                "mode": args.mode,
                "memory_backend": args.memory_backend,
                "error": str(exc),
            },
        )
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve", help="resolve one Codex worker audit packet")
    resolve.add_argument("--mode", choices=("cold", "warm"), required=True)
    resolve.add_argument("--memory-backend", choices=("everos", "sqlite"), required=True)
    resolve.add_argument("--round-id", required=True)
    resolve.add_argument("--task-id", required=True)
    resolve.add_argument("--task-prompt", required=True)
    resolve.add_argument("--database", type=Path, required=True)
    resolve.add_argument("--lock", type=Path, required=True)
    resolve.add_argument("--events", type=Path, required=True)
    resolve.add_argument("--model-events", type=Path, required=True)
    resolve.add_argument("--model", required=True)
    resolve.add_argument("--everos-url")
    resolve.add_argument(
        "--planner-timeout",
        type=float,
        default=float(os.environ.get("RRC_PLANNER_TIMEOUT", "45")),
    )
    resolve.add_argument(
        "--lock-timeout",
        type=float,
        default=float(os.environ.get("RRC_LOCK_TIMEOUT", "45")),
    )
    resolve.add_argument(
        "--visibility-timeout",
        type=float,
        default=float(os.environ.get("RRC_VISIBILITY_TIMEOUT", "30")),
    )
    resolve.add_argument(
        "--planner-codex-home",
        type=Path,
        default=os.environ.get("RRC_PLANNER_CODEX_HOME"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "resolve":
        return _resolve_cli(args)
    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
