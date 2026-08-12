from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from rrc.attempts import AttemptRepository
from rrc.cell_journal import RootToolEventV1, SQLiteCellJournal
from rrc.contextmesh import (
    AcceptedReceiptPayloadV1,
    ReceiptRecordV1,
    RRCAcceptedTargetV1,
    WaitVerifierV1,
    build_wait_envelope,
)
from rrc.contract import (
    ArtifactRefV1,
    CostEventV1,
    ReferencedTaskInputV1,
    Slots,
    Spec,
    TargetPreimageV1,
    Task,
    canonical_json_bytes,
    canonical_test_artifact_bytes,
    seal_task_input,
    task_envelope_bytes,
)
from rrc.dispatch_permit import DispatchPermitV1, ProductCellDispatchRequestV1, product_call_id
from rrc.journal import (
    AcceptedCommitV1,
    AcceptedOutcomeRecordV1,
    CallRecordV1,
    SealedAttemptInputsV1,
    SQLiteRRCRepository,
    TerminalClaimV1,
    UsageRecordV1,
    parse_accepted_commit,
)
from rrc.pipeline.template import template_bundle_bytes, templatize
from rrc.pipeline.verify import CodeArtifactV1, VerificationResultV1, VerificationTierRowV1

HANDLERS = ("orders", "products", "reviews", "users")
SHARED = ("src/models.js", "src/utils.js", "src/middleware.js")


def _sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    path.chmod(0o600)


def _manifest(arm: Path, round_id: str, side: str, backend: str) -> dict[str, str]:
    target = arm / "target"
    (target / "src/handlers").mkdir(parents=True)
    handler_hashes: dict[str, str] = {}
    for handler in HANDLERS:
        path = target / f"src/handlers/{handler}.js"
        path.write_text(f"export function {handler}() {{ return true }}\n")
        handler_hashes[handler] = _sha(path.read_bytes())
    files: list[dict[str, object]] = []
    for relative in SHARED:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((f"export const {path.stem} = true;\n") * 100)
        digest = f"digest:{relative}"
        row: dict[str, object] = {
            "v": 1,
            "round_id": round_id,
            "arm": side,
            "memory_backend": backend,
            "path": relative,
            "raw_sha256": _sha(path.read_bytes()),
            "digest_sha256": _sha(digest),
            "digest": digest if backend == "sqlite" else None,
            "everos_key": f"key:{relative}" if backend == "everos" else None,
            "raw_chars": path.stat().st_size,
            "digest_chars": len(digest),
        }
        files.append(row)
    target_stat = target.resolve().stat()
    manifest: dict[str, object] = {
        "v": 1,
        "round_id": round_id,
        "arm": side,
        "memory_backend": backend,
        "target_root": str(target.resolve()),
        "target_device": target_stat.st_dev,
        "target_inode": target_stat.st_ino,
        "files": files,
    }
    unsigned = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["seal"] = _sha(unsigned)
    path = arm / "seed-manifest.json"
    path.write_text(json.dumps(manifest, separators=(",", ":")))
    path.chmod(0o600)
    return handler_hashes


def _usage(component: str, session: str, *, agent_id: str | None = None) -> dict[str, object]:
    return {
        "v": 1,
        "event": "native_usage",
        "memory_backend": "placeholder",
        "component": component,
        "agent_id": agent_id,
        "session_id": session,
        "model": "gpt-5.5",
        "transcript_sha256": _sha(session),
        "input_tokens": 90,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 0,
        "output_tokens": 10,
        "reasoning_output_tokens": 4,
        "total_tokens": 100,
    }


def _arm(root: Path, round_id: str, side: str, backend: str) -> None:
    arm = root / "runs/rrd-demo" / round_id / side
    hashes = _manifest(arm, round_id, side, backend)
    hooks: list[dict[str, object]] = []
    agents = [f"agent-{name}" for name in HANDLERS]
    for handler, agent in zip(HANDLERS, agents, strict=True):
        tool = f"tool-{handler}"
        hooks.extend(
            [
                {
                    "v": 1,
                    "event": "assignment",
                    "memory_backend": backend,
                    "tool_use_id": tool,
                    "handler": f"src/handlers/{handler}.js",
                    "handler_sha256": hashes[handler],
                },
                {
                    "v": 1,
                    "event": "spawned",
                    "memory_backend": backend,
                    "tool_use_id": tool,
                    "agent_id": agent,
                },
                {
                    "v": 1,
                    "event": "shared_context",
                    "memory_backend": backend,
                    "agent_id": agent,
                    "receipts": [
                        {
                            "path": relative,
                            "digest_sha256": _sha(f"digest:{relative}"),
                            "raw_sha256": _sha((arm / "target" / relative).read_bytes()),
                            "hit": True,
                        }
                        for relative in SHARED
                    ],
                },
                {
                    "v": 1,
                    "event": "result_final",
                    "memory_backend": backend,
                    "agent_id": agent,
                    "delivered_chars": 80,
                    "delivered_sha256": _sha(f"report-{agent}"),
                },
            ]
        )
        usage = _usage("worker", f"session-{agent}", agent_id=agent)
        usage["memory_backend"] = backend
        hooks.append(usage)
    hooks.extend(
        [
            *[
                {
                    "v": 1,
                    "event": "wait_result",
                    "memory_backend": backend,
                    "completed_agent_ids": [agent],
                    "result_count": 1,
                    "timed_out": False,
                }
                for agent in agents
            ],
            {
                "v": 1,
                "event": "compression_delivered",
                "memory_backend": backend,
                "receipts": {
                    agent: {
                        "receipt": _sha(agent)[:20],
                        "sha256": _sha(f"report-{agent}"),
                        "bytes": 5000,
                        "path": f"raw-results/{agent}.txt",
                    }
                    for agent in agents[:3]
                },
                "raw_bytes": 20_000,
                "delivered_bytes": 1800,
            },
            {
                "v": 1,
                "event": "compression_bypass",
                "memory_backend": backend,
                "agent_ids": [agents[3]],
                "receipts": {
                    agents[3]: {
                        "receipt": _sha(agents[3])[:20],
                        "sha256": _sha(f"report-{agents[3]}"),
                        "bytes": 500,
                        "path": f"raw-results/{agents[3]}.txt",
                    }
                },
                "raw_bytes": 600,
                "candidate_bytes": 500,
            },
            {
                "v": 1,
                "event": "root_merge",
                "memory_backend": backend,
                "chars": 500,
                "sha256": _sha("root"),
            },
        ]
    )
    root_usage = _usage("root", f"session-{side}-root")
    root_usage["memory_backend"] = backend
    hooks.append(root_usage)
    _jsonl(arm / "hook-events.jsonl", hooks)

    branches = ["miss"] * 4 if side == "a" else ["miss", "hit", "hit", "hit"]
    _jsonl(
        arm / "rrc-events.jsonl",
        [
            {
                "event": "packet",
                "memory_backend": backend,
                "branch": branch,
                "task_id": f"task-{handler}",
                "handler": f"src/handlers/{handler}.js",
            }
            for handler, branch in zip(HANDLERS, branches, strict=True)
        ],
    )
    _jsonl(
        arm / "rrc-model-events.jsonl",
        [
            {
                "parse_status": "ok",
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 10,
                    "total_tokens": 50,
                },
            }
            for _ in range(branches.count("miss"))
        ],
    )


def _round(root: Path, backend: str = "sqlite") -> str:
    round_id = f"rrd-{backend}-fixture"
    directory = root / "runs/rrd-demo" / round_id
    directory.mkdir(parents=True)
    meta = directory / "round-meta.json"
    meta.write_text(
        json.dumps(
            {
                "v": 2,
                "round_id": round_id,
                "memory_backend": backend,
                "provider": "native-codex",
                "model": "gpt-5.5",
            },
            separators=(",", ":"),
        )
    )
    meta.chmod(0o600)
    _arm(root, round_id, "a", backend)
    _arm(root, round_id, "b", backend)
    return round_id


@pytest.mark.parametrize("backend", ["sqlite", "everos"])
def test_historical_audit_meter_reaches_ready_for_both_memory_backends(
    tmp_path: Path, backend: str
) -> None:
    from contextmesh.scripts.rrd_audit_meter import collect

    round_id = _round(tmp_path, backend)
    snapshot = collect(round_id, memory_backend=backend, root=tmp_path)

    assert snapshot["a"]["ready"] == snapshot["b"]["ready"] == 1
    assert snapshot["a"]["combined_tokens"] == 700
    assert snapshot["b"]["combined_tokens"] == 550
    assert (snapshot["a"]["misses"], snapshot["a"]["hits"]) == (4, 0)
    assert (snapshot["b"]["misses"], snapshot["b"]["hits"]) == (1, 3)


def test_missing_native_worker_usage_is_not_ready(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_audit_meter import collect

    round_id = _round(tmp_path)
    path = tmp_path / f"runs/rrd-demo/{round_id}/a/hook-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    _jsonl(
        path,
        [
            row
            for row in rows
            if not (row.get("event") == "native_usage" and row.get("agent_id") == "agent-orders")
        ],
    )

    side = collect(round_id, memory_backend="sqlite", root=tmp_path)["a"]
    assert side["usage_ok"] == 0
    assert side["ready"] == 0


def test_malformed_native_usage_and_jsonl_are_not_coerced_to_zero(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_audit_meter import collect

    round_id = _round(tmp_path)
    hook_path = tmp_path / f"runs/rrd-demo/{round_id}/b/hook-events.jsonl"
    rows = [json.loads(line) for line in hook_path.read_text().splitlines()]
    usage = next(row for row in rows if row.get("component") == "root")
    usage["input_tokens"] = "bad"
    _jsonl(hook_path, rows)
    side = collect(round_id, memory_backend="sqlite", root=tmp_path)["b"]
    assert side["usage_ok"] == 0 and side["ready"] == 0

    with hook_path.open("a") as stream:
        stream.write("{truncated\n")
    side = collect(round_id, memory_backend="sqlite", root=tmp_path)["b"]
    assert side["evidence_ok"] == 0 and side["ready"] == 0


def test_backend_or_compression_mismatch_is_not_ready(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_audit_meter import collect

    round_id = _round(tmp_path)
    path = tmp_path / f"runs/rrd-demo/{round_id}/a/hook-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    compression = next(row for row in rows if row.get("event") == "compression_delivered")
    del compression["receipts"]["agent-orders"]
    _jsonl(path, rows)

    side = collect(round_id, memory_backend="sqlite", root=tmp_path)["a"]
    assert side["protocol_ok"] == 0 and side["ready"] == 0


def test_render_discloses_observational_not_billing_exact(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_audit_meter import collect, render

    round_id = _round(tmp_path)
    output = render(
        round_id,
        collect(round_id, memory_backend="sqlite", root=tmp_path),
        "sqlite",
    )
    assert "READY" in output
    assert "provider-visible" in output
    assert "billing_exact=false" in output
    assert "hidden_retry_observable=false" in output


# Canonical RRCv2 meter fixtures intentionally use the real SQLite journal and
# receipt/apply authorities.  The historical audit tests above remain isolated
# behind rrd_audit_meter.


def _canonical_meter_attempt(
    repository: SQLiteRRCRepository,
    *,
    round_dir: Path,
    side: str,
    index: int,
    branch: str,
) -> tuple[str, str, str, dict[str, object]]:
    mode = "cold" if side == "a" else "warm"
    task_id = f"meter-task-{index}"
    artifact_path = f"rrcv2_tasks/{task_id}.py"
    target = round_dir / side / "target"
    target.mkdir(parents=True, exist_ok=True)
    starter = "def value() -> int:\n    raise NotImplementedError"
    accepted_source = "def value() -> int:\n    return 1"
    target_path = target / artifact_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(starter, encoding="utf-8")
    target_path.chmod(0o644)
    task = Task(task_id, "Implement value() returning one.", artifact_path=artifact_path)
    sealed = round_dir / "sealed" / side / task_id
    source_ref_path = sealed / artifact_path
    source_ref_path.parent.mkdir(parents=True)
    source_ref_path.write_text(starter, encoding="utf-8")
    source_ref_path.chmod(0o644)
    public = canonical_test_artifact_bytes(("def test_value():\n    assert value() == 1",))
    public_path = sealed / ".rrcv2/public-tests.v1.json"
    public_path.parent.mkdir(parents=True)
    public_path.write_bytes(public)
    public_path.chmod(0o600)
    input_root = (round_dir / "inputs" / side / task_id).absolute()
    input_root.parent.mkdir(parents=True, exist_ok=True)
    envelope = seal_task_input(
        ReferencedTaskInputV1(
            task,
            sealed.absolute(),
            ArtifactRefV1(
                hashlib.sha256(starter.encode()).hexdigest(),
                len(starter.encode()),
                artifact_path,
            ),
            ArtifactRefV1(
                hashlib.sha256(public).hexdigest(),
                len(public),
                ".rrcv2/public-tests.v1.json",
            ),
            None,
            TargetPreimageV1.regular(
                artifact_path,
                hashlib.sha256(starter.encode()).hexdigest(),
                len(starter.encode()),
                0o644,
            ),
        ),
        input_root=input_root,
    )
    envelope_raw = task_envelope_bytes(envelope)
    attempt = repository.begin_attempt(
        "meter-owner",
        f"meter-{side}-{index}",
        SealedAttemptInputsV1(
            hashlib.sha256(envelope_raw).hexdigest(),
            mode,  # type: ignore[arg-type]
            "spec_pipeline",
            "contextmesh",
            "2" * 64,
            "3" * 64,
            "4" * 64,
        ),
    )
    attempts = AttemptRepository(repository)
    tool_id = f"tool-{side}-{index}"
    assignment = canonical_json_bytes({"task_id": task_id, "v": 1})
    attempts.register_prepared_input(
        attempt,
        task_envelope=envelope_raw,
        input_root=envelope.input_root,  # type: ignore[arg-type]
        target_root=target.absolute(),
        assignment=assignment,
        expected_tool_use_id=tool_id,
    )
    cell_id = f"rrcv2-{round_dir.name}-{side}"
    stages = ["implement"] if branch == "reuse" else ["spec", "independent_tests", "implement"]
    call_ids: list[str] = []
    transcript_sha = ""
    for ordinal, stage in enumerate(stages, 1):
        call_id = f"meter-{side}-{index}-{stage}"
        call_ids.append(call_id)
        prompt_sha = hashlib.sha256(f"{task_id}:{stage}".encode()).hexdigest()
        record = CallRecordV1(
            call_id,
            stage,
            ordinal,
            "native_worker" if stage == "implement" else ("strong" if stage == "spec" else "small"),
            "gpt-5.6-luna" if stage != "spec" else "gpt-5.5",
            "5" * 64,
            prompt_sha,
            "6" * 64,
        )
        attempt = repository.prepare_call(
            attempt,
            record,
            expected_state=attempt.state,
            expected_generation=attempt.generation,
            expected_cursor=attempt.cursor,
        )
        attempt = repository.mark_call_started(
            attempt,
            call_id,
            canonical_json_bytes({"call_id": call_id, "v": 1}),
            expected_state=attempt.state,
            expected_generation=attempt.generation,
            expected_cursor=attempt.cursor,
        )
        input_tokens = 10 + ordinal
        output_tokens = 3 + ordinal
        total = input_tokens + output_tokens
        transcript_sha = hashlib.sha256(f"transcript:{call_id}".encode()).hexdigest()
        attempt = repository.observe_call(
            attempt,
            call_id,
            canonical_json_bytes({"sha256": transcript_sha, "v": 1}),
            UsageRecordV1(input_tokens, output_tokens, total, 2),
            expected_state=attempt.state,
            expected_generation=attempt.generation,
            expected_cursor=attempt.cursor,
        )
        cost = CostEventV1(
            call_id,
            cell_id,
            attempt.attempt_id,
            task_id,
            mode,
            stage,
            ordinal,
            prompt_sha,
            "7" * 64,
            transcript_sha,
            "openai",
            record.model,
            "low",
            "priority",
            "usage_only",
            "unattested",
            "unattested",
            "unattested",
            "unattested",
            input_tokens,
            2,
            output_tokens,
            1,
            total,
        )
        attempt = repository.commit_call(
            attempt,
            call_id,
            outcome=canonical_json_bytes({"call_id": call_id, "v": 1}),
            cost_event=cost.canonical_bytes(),
            expected_state=attempt.state,
            expected_generation=attempt.generation,
            expected_cursor=attempt.cursor,
        )
    attempt = repository.claim_finishing(
        attempt,
        owner_id=f"meter-finisher-{side}",
        expected_state=attempt.state,
        expected_generation=attempt.generation,
        expected_cursor=attempt.cursor,
    )
    artifact = CodeArtifactV1(attempt.attempt_id, artifact_path, accepted_source)
    artifact_raw = canonical_json_bytes(
        {
            "artifact_path": artifact.artifact_path,
            "attempt_id": artifact.attempt_id,
            "source": artifact.source,
            "v": 1,
        }
    )
    passed = hashlib.sha256(b"passed\n").hexdigest()
    empty = hashlib.sha256(b"").hexdigest()
    artifact_sha = hashlib.sha256(artifact_raw).hexdigest()
    result = VerificationResultV1(
        attempt.attempt_id,
        "rrcv2_general_v1",
        artifact_sha,
        tuple(
            VerificationTierRowV1(name, "passed", artifact_sha, passed, empty)
            for name in ("assembly", "ruff", "pyright", "pytest")
        ),
        True,
    )
    result_sha, artifact_record = repository.persist_verification(artifact, result)
    artifact_record_raw = canonical_json_bytes(
        {
            "artifact_path": artifact_record.artifact_path,
            "attempt_id": artifact_record.attempt_id,
            "blob_bytes": artifact_record.blob_bytes,
            "blob_mode": artifact_record.blob_mode,
            "blob_path": artifact_record.blob_path,
            "blob_sha256": artifact_record.blob_sha256,
            "source_bytes": artifact_record.source_bytes,
            "source_sha256": artifact_record.source_sha256,
            "v": 1,
        }
    )
    commit_id = hashlib.sha256(f"commit:{side}:{index}".encode()).hexdigest()
    receipt_payload = AcceptedReceiptPayloadV1(
        attempt.attempt_id,
        commit_id,
        artifact_path,
        artifact_record.source_sha256,
        artifact_record.source_bytes,
        result_sha,
    )
    receipt = ReceiptRecordV1.bind(
        receipt_payload,
        artifact_record_sha256=hashlib.sha256(artifact_record_raw).hexdigest(),
    )
    bundle = None
    disposition = "cold_no_store"
    if mode == "warm":
        bundle = template_bundle_bytes(
            templatize(
                Spec(
                    "Return one.",
                    "def value() -> int",
                    "Return one.",
                    ("def test_value():\n    assert value() == 1",),
                    Slots(identifiers=("value",), constants=("1",)),
                ),
                ("def test_other():\n    assert value() == 1",),
                slot_values=(("constant", "1"), ("function", "value")),
                primary="value",
            )
        )
        disposition = "unindexed_primary_ambiguous"
    accepted = AcceptedCommitV1(
        commit_id,
        mode,  # type: ignore[arg-type]
        "contextmesh",
        AcceptedOutcomeRecordV1(
            attempt.attempt_id,
            task_id,
            mode,  # type: ignore[arg-type]
            "contextmesh",
            branch,  # type: ignore[arg-type]
            False,
            disposition,  # type: ignore[arg-type]
            hashlib.sha256(artifact_record_raw).hexdigest(),
            result_sha,
            tuple(call_ids),
        ),
        bundle,
        None,
        None,
        None,
        None,
        artifact_record,
        receipt.canonical_bytes(),
    )
    repository.commit_accepted(
        attempt,
        TerminalClaimV1(f"meter-finisher-{side}", attempt.generation, "finishing"),
        accepted,
    )
    repository.record_oracle_score(
        attempt.attempt_id,
        status="passed",
        score=True,
        evidence_sha256=hashlib.sha256(f"oracle:{task_id}".encode()).hexdigest(),
    )
    target_path.write_text(accepted_source, encoding="utf-8")
    target_path.chmod(0o644)
    implement_raw = repository.load_call_inventory(attempt.attempt_id)[-1][2]
    assert implement_raw is not None
    implement = json.loads(implement_raw)
    return attempt.attempt_id, tool_id, receipt.receipt, implement


def _canonical_meter_round(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "contextmesh"
    round_id = "rrd-sqlite-meter"
    round_dir = root / "runs" / "rrd-demo" / round_id
    round_dir.mkdir(parents=True)
    meta = round_dir / "round-meta.json"
    meta.write_text(
        json.dumps(
            {
                "v": 2,
                "round_id": round_id,
                "memory_backend": "sqlite",
                "provider": "native-codex",
                "model": "gpt-5.5",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    meta.chmod(0o600)
    database = round_dir / "rrcv2.sqlite3"
    hook_rows: dict[str, list[dict[str, object]]] = {"a": [], "b": []}
    with SQLiteRRCRepository(database) as repository:
        for side in ("a", "b"):
            branches = ("miss",)
            agents: list[str] = []
            combined_inputs: list[tuple[str, str, str, dict[str, object], str]] = []
            for index, branch in enumerate(branches, 1):
                attempt_id, tool_id, receipt, implement = _canonical_meter_attempt(
                    repository,
                    round_dir=round_dir,
                    side=side,
                    index=index,
                    branch=branch,
                )
                agent = f"agent-{side}-{index}"
                agents.append(agent)
                combined_inputs.append((attempt_id, tool_id, receipt, implement, agent))
                common = {"v": 1, "memory_backend": "sqlite"}
                hook_rows[side].extend(
                    (
                        {
                            **common,
                            "event": "rrcv2_assignment_prepared",
                            "attempt_id": attempt_id,
                            "task_id": f"meter-task-{index}",
                            "tool_use_id": tool_id,
                            "branch": branch,
                        },
                        {
                            **common,
                            "event": "rrcv2_spawn_bound",
                            "attempt_id": attempt_id,
                            "tool_use_id": tool_id,
                            "agent_id": agent,
                        },
                        {
                            **common,
                            "event": "rrcv2_subagent_started",
                            "attempt_id": attempt_id,
                            "agent_id": agent,
                        },
                        {
                            **common,
                            "event": "rrcv2_worker_submitted",
                            "attempt_id": attempt_id,
                            "agent_id": agent,
                            "transcript_sha256": implement["transcript_sha256"],
                        },
                        {
                            **common,
                            "event": "native_usage",
                            "component": "worker",
                            "agent_id": agent,
                            "session_id": f"session-{agent}",
                            "transcript_sha256": implement["transcript_sha256"],
                            "input_tokens": implement["input_tokens"],
                            "cached_input_tokens": implement["cached_input_tokens"],
                            "cache_write_input_tokens": 0,
                            "output_tokens": implement["output_tokens"],
                            "reasoning_output_tokens": implement["reasoning_output_tokens"],
                            "total_tokens": implement["provider_total_tokens"],
                        },
                        {**common, "event": "rrcv2_result_reader_allowed"},
                    )
                )
            attempt_id, tool_id, receipt, _implement, agent = combined_inputs[0]
            registered = AttemptRepository(repository).load_registered_input(attempt_id)
            request = ProductCellDispatchRequestV1(
                call_id="",
                scope="interactive",
                controller="contextmesh",
                task_id=registered.task_envelope.task.task_id,
                task_envelope_sha256=hashlib.sha256(
                    task_envelope_bytes(registered.task_envelope)
                ).hexdigest(),
                run_id=round_id,
                replicate_id="interactive",
                arm="rrc_cold" if side == "a" else "rrc_warm",
                branch="combined",
                stage="contextmesh_root_session",
                stage_ordinal=1,
                journal_cursor=1,
                cell_id=f"rrcv2-{round_id}-{side}",
                attempt_id=None,
                transport="contextmesh",
                surface_id="root_strong_medium_native",
            )
            request = replace(request, call_id=product_call_id(request))
            cells = SQLiteCellJournal(repository, authority_root=round_dir / "cell-authority")
            cell = cells.begin_cell(request)
            cells.prepare_root_call(cell)
            started = cells.mark_root_started(
                cells.load_cell(cell.cell_id),
                permit=DispatchPermitV1(
                    "product",
                    request.call_id,
                    "root_strong_medium_native",
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                ),
                session_id=f"launch-{side}",
                transcript_baseline_sha256="0" * 64,
            )
            cells.bind_attempt(
                started,
                tool_use_id=tool_id,
                attempt_id=attempt_id,
                task_envelope_sha256=request.task_envelope_sha256,
                expected_generation=started.cell.generation,
            )
            cells.complete_bound_attempt(
                cell_id=cell.cell_id,
                attempt_id=attempt_id,
                tool_use_id=tool_id,
                agent_id=agent,
                expected_generation=started.cell.generation,
            )
            terminal = repository.load_terminal_intent(attempt_id)
            assert terminal is not None and terminal[0] == "accepted"
            source = repository.load_accepted_source(
                attempt_id,
                parse_accepted_commit(terminal[1]).outcome.artifact_record_sha256,
            ).encode()
            wait = build_wait_envelope(
                round_id,
                (
                    RRCAcceptedTargetV1(
                        agent,
                        attempt_id,
                        receipt,
                        "results/accepted-code.v1.utf8",
                        hashlib.sha256(source).hexdigest(),
                        len(source),
                        WaitVerifierV1("d" * 64, "e" * 64),
                        True,
                    ),
                ),
            )
            wait_raw = wait.canonical_bytes()
            cells.record_root_tool_event(
                cell_id=cell.cell_id,
                event=RootToolEventV1(
                    "wait",
                    f"wait-{side}",
                    hashlib.sha256(f"wait-input:{side}".encode()).hexdigest(),
                    hashlib.sha256(wait_raw).hexdigest(),
                    wait_raw,
                ),
                expected_generation=started.cell.generation,
            )
            apply_raw = canonical_json_bytes(
                {
                    "artifact_path": registered.task_envelope.task.artifact_path,
                    "attempt_id": attempt_id,
                    "bytes": len(source),
                    "receipt": receipt,
                    "sha256": hashlib.sha256(source).hexdigest(),
                    "v": 1,
                }
            )
            cells.record_root_tool_event(
                cell_id=cell.cell_id,
                event=RootToolEventV1(
                    "apply",
                    "apply-" + attempt_id,
                    hashlib.sha256(f"apply-input:{side}".encode()).hexdigest(),
                    hashlib.sha256(apply_raw).hexdigest(),
                    apply_raw,
                ),
                expected_generation=started.cell.generation,
            )
            observed = cells.observe_root_call(
                cell_id=cell.cell_id,
                expected_generation=started.cell.generation,
                root_session_id=f"root-session-{side}",
                prompt_sha256="1" * 64,
                final_message_sha256="a" * 64,
                transcript_sha256=hashlib.sha256(f"root:{side}".encode()).hexdigest(),
                transcript_bytes=100,
                requested_provider="openai",
                requested_model="gpt-5.5",
                requested_reasoning="medium",
                requested_service_tier="priority",
                identity_attestation="usage_only",
                effective_provider="unattested",
                effective_model="unattested",
                effective_reasoning="unattested",
                effective_service_tier="unattested",
                usage={
                    "input_tokens": 40,
                    "cached_input_tokens": 10,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 3,
                    "provider_total_tokens": 50,
                },
            )
            cells.commit_root_cost_event(
                cell_id=cell.cell_id, expected_generation=observed.generation
            )
            combined = cells.build_combined_session(cell_id=cell.cell_id, round_id=round_id)
            cells.commit_combined_session(
                combined, expected_generation=cells.load_cell(cell.cell_id).generation
            )
            common = {"v": 1, "memory_backend": "sqlite"}
            hook_rows[side].extend(
                (
                    {
                        **common,
                        "event": "rrcv2_wait_substituted",
                        "target_count": 1,
                        "wait_id": wait.wait_id,
                        "wait_sha256": hashlib.sha256(wait_raw).hexdigest(),
                    },
                    {**common, "event": "root_merge", "chars": 10, "sha256": "a" * 64},
                    {
                        **common,
                        "event": "native_usage",
                        "component": "root",
                        "agent_id": None,
                        "session_id": f"root-session-{side}",
                        "transcript_sha256": hashlib.sha256(f"root:{side}".encode()).hexdigest(),
                        "input_tokens": 40,
                        "cached_input_tokens": 10,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 3,
                        "total_tokens": 50,
                    },
                    {
                        **common,
                        "event": "rrcv2_combined_session_committed",
                        "cell_id": cell.cell_id,
                        "combined_session_sha256": hashlib.sha256(
                            combined.canonical_bytes()
                        ).hexdigest(),
                        "root_cost_event_id": request.call_id,
                    },
                )
            )
    database.chmod(0o600)
    for side, rows in hook_rows.items():
        path = round_dir / side / "hook-events.jsonl"
        path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
        path.chmod(0o600)
    return root, round_id


def test_canonical_meter_reopens_attempts_receipts_applied_bytes_and_exact_usage(
    tmp_path: Path,
) -> None:
    from contextmesh.scripts import rrd_combined_meter as meter

    root, round_id = _canonical_meter_round(tmp_path)
    snapshot = meter.collect(round_id, memory_backend="sqlite", root=root)

    assert snapshot["a"]["ready"] == 1
    assert snapshot["b"]["ready"] == 1
    assert (snapshot["a"]["misses"], snapshot["a"]["reuses"]) == (1, 0)
    assert (snapshot["b"]["misses"], snapshot["b"]["reuses"]) == (1, 0)
    assert snapshot["a"]["combined_tokens"] == (
        snapshot["a"]["root_tokens"]
        + snapshot["a"]["worker_tokens"]
        + snapshot["a"]["pipeline_tokens"]
    )
    rendered = meter.render(round_id, snapshot, "sqlite")
    assert "functional-only" in rendered
    assert "no demonstrated RRCv2 savings" in rendered
    assert "request_estimate_eligible=false" in rendered


def test_canonical_meter_fails_closed_on_applied_source_or_usage_mutation(tmp_path: Path) -> None:
    from contextmesh.scripts import rrd_combined_meter as meter

    root, round_id = _canonical_meter_round(tmp_path)
    round_dir = root / "runs" / "rrd-demo" / round_id
    target = round_dir / "b/target/rrcv2_tasks/meter-task-1.py"
    target.write_text("def value() -> int:\n    return 999", encoding="utf-8")
    assert meter.collect(round_id, memory_backend="sqlite", root=root)["b"]["ready"] == 0
    target.write_text("def value() -> int:\n    return 1", encoding="utf-8")
    hooks = round_dir / "a/hook-events.jsonl"
    rows = hooks.read_text().splitlines()
    value = json.loads(rows[4])
    value["input_tokens"] = "11"
    rows[4] = json.dumps(value, separators=(",", ":"))
    hooks.write_text("\n".join(rows) + "\n")
    assert meter.collect(round_id, memory_backend="sqlite", root=root)["a"]["ready"] == 0


def test_canonical_meter_requires_the_durable_combined_root_cost_union(tmp_path: Path) -> None:
    from contextmesh.scripts import rrd_combined_meter as meter

    root, round_id = _canonical_meter_round(tmp_path)
    database = root / "runs" / "rrd-demo" / round_id / "rrcv2.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE rrcv2p_cells SET combined_session=NULL WHERE cell_id=?",
            (f"rrcv2-{round_id}-b",),
        )
        connection.commit()
    finally:
        connection.close()
    snapshot = meter.collect(round_id, memory_backend="sqlite", root=root)
    assert snapshot["a"]["ready"] == 1
    assert snapshot["b"]["ready"] == 0
    assert snapshot["b"]["authority_ok"] == 0
