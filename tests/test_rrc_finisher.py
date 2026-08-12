from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from contextmesh.scripts.rrc_finisher import finish_one
from contextmesh.scripts.rrd_result_reader import ResultReaderError, apply_receipt
from rrc.attempts import AttemptRepository, event_payload
from rrc.contextmesh import (
    CodingAssignmentV1,
    RRCAcceptedTargetV1,
    RRCNativeFallbackTargetV1,
    parse_receipt_record,
    parse_wait_envelope,
)
from rrc.contextmesh_runtime import (
    ContextMeshController,
    derive_native_worker_result,
    subagent_start_context,
)
from rrc.contract import (
    Completion,
    Config,
    ModelRole,
    TargetPreimageV1,
    Task,
    Usage,
    canonical_json_bytes,
    canonical_test_artifact_bytes,
)
from rrc.journal import SQLiteRRCRepository, parse_accepted_commit, parse_rejected_commit
from rrc.pipeline.verify import (
    CodeArtifactV1,
    VerificationResultV1,
    VerificationRunV1,
    VerificationTierRowV1,
    code_artifact_bytes,
)
from rrc.retrieval import SQLiteHybridRetrieval


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class PrepareModel:
    provider = "openai"

    def complete(self, role: ModelRole, prompt: str, ctx, stage: str) -> Completion:
        del role, prompt, ctx
        if stage == "spec":
            text = json.dumps(
                {
                    "contract": "Return one.",
                    "plan": "Implement value.",
                    "signature": "def value() -> int",
                    "slots": {
                        "constants": [],
                        "edge_values": [],
                        "entity": None,
                        "fields": [],
                        "identifiers": [],
                        "types": [],
                    },
                    "tests": ["def test_spec():\n    assert value() == 1"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        elif stage == "independent_tests":
            text = json.dumps(
                {"tests": ["def test_independent():\n    assert value() == 1"], "v": 1},
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            raise AssertionError(f"unexpected finisher model stage {stage}")
        return Completion(text, Usage(1, 1, 2), "test-model")


def _assignment(target: Path) -> CodingAssignmentV1:
    source = b"def value() -> int:\n    raise NotImplementedError"
    target.joinpath("solution.py").write_bytes(source)
    target.joinpath("solution.py").chmod(0o644)
    target.joinpath("tests").mkdir()
    public = canonical_test_artifact_bytes(("def test_public():\n    assert value() == 1",))
    target.joinpath("tests/public.v1.json").write_bytes(public)
    target.joinpath("tests/public.v1.json").chmod(0o600)
    return CodingAssignmentV1(
        mode="cold",
        task=Task("task-1", "Implement value()."),
        source_path="solution.py",
        public_test_path="tests/public.v1.json",
        oracle_test_path=None,
        owned_paths=("solution.py",),
        target_preimage=TargetPreimageV1.regular("solution.py", _sha(source), len(source), 0o644),
    )


def _message(role: str, text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _rollout(agent_id: str, prompt: str, final: str) -> bytes:
    rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": agent_id,
                "session_id": "root-session",
                "parent_thread_id": "root-session",
                "agent_role": "worker",
                "thread_source": "subagent",
                "model_provider": "openai",
            },
        },
        _message("developer", "base"),
        _message("user", "environment"),
        {"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}},
        _message("developer", subagent_start_context()),
        _message("user", prompt),
        {"type": "event_msg", "payload": {"type": "agent_message", "message": final}},
        _message("assistant", final),
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 0,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 15,
                    }
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    return b"".join(canonical_json_bytes(row) + b"\n" for row in rows)


def _accepted_verification(*, attempt_id: str, task: Task, source: str, **_kwargs):
    artifact = CodeArtifactV1(attempt_id, task.artifact_path, source)
    artifact_sha = _sha(code_artifact_bytes(artifact))
    passed = _sha(b"passed\n")
    empty = _sha(b"")
    tiers = tuple(
        VerificationTierRowV1(name, "passed", artifact_sha, passed, empty)
        for name in ("assembly", "ruff", "pyright", "pytest")
    )
    return VerificationRunV1(
        VerificationResultV1(
            attempt_id,
            task.verification_profile,
            artifact_sha,
            tiers,
            True,
        ),
        artifact,
        (),
    )


def test_finisher_accepts_submitted_native_candidate_and_closes_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rrc.pipeline.solve as solve_module

    monkeypatch.setattr(solve_module, "_run_verifier", _accepted_verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: None)
    target = tmp_path / "target"
    target.mkdir()
    assignment = _assignment(target)
    config = Config("owner")
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repository:
        attempts = AttemptRepository(repository)
        controller = ContextMeshController(
            attempts=attempts,
            model=PrepareModel(),
            retrieval=SQLiteHybridRetrieval(repository),
            config=config,
            target_root=target,
            attempts_root=tmp_path / "attempts",
            route_id="rrcv2-coding-v1",
            round_id="round-1",
        )
        prepared = controller.prepare(assignment, tool_use_id="tool-1")
        attempt = attempts.bind_spawn(
            prepared.prepared.attempt,
            event_id="post-1",
            tool_use_id="tool-1",
            agent_id="agent-1",
            payload=event_payload("post_tool_spawn", tool_use_id="tool-1", agent_id="agent-1"),
        )
        final = canonical_json_bytes(
            {
                "artifact_path": "solution.py",
                "attempt_id": attempt.attempt_id,
                "source": "def value() -> int:\n    return 1",
                "v": 1,
            }
        ).decode()
        rollout = _rollout("agent-1", prepared.worker_prompt, final)
        derived = derive_native_worker_result(
            registered_assignment=prepared,
            tool_use_id="tool-1",
            agent_id="agent-1",
            final_message=final,
            transcript_raw=rollout,
            root_sentinel="root-private",
            parent_history_sentinel="parent-private",
        )
        attempt = attempts.submit_stop(
            attempt,
            event_id="stop-1",
            worker_evidence=derived.worker_evidence.canonical_bytes(),
            context_attestation=derived.context_attestation.canonical_bytes(),
            candidate=derived.candidate.canonical_bytes(),
            transcript=derived.transcript_raw,
            payload=event_payload("subagent_stop", agent_id="agent-1"),
        )
        assert attempt.state == "submitted"

        assert (
            finish_one(
                attempts,
                model=PrepareModel(),
                config=config,
                owner_id="finisher-1",
                now_ms=1_000,
            )
            == "task-1"
        )
        terminal = repository.load_terminal_intent(attempt.attempt_id)
        assert terminal is not None and terminal[0] == "accepted"
        accepted = parse_accepted_commit(terminal[1])
        assert accepted.receipt_record is not None
        committed_costs = {
            call_id: json.loads(cost_raw)
            for call_id, state, cost_raw in repository.load_call_inventory(attempt.attempt_id)
            if state == "call_committed" and cost_raw is not None
        }
        assert set(committed_costs) == set(accepted.outcome.cost_event_ids)
        worker_costs = [row for row in committed_costs.values() if row["stage"] == "implement"]
        assert len(worker_costs) == 1
        assert worker_costs[0]["provider_total_tokens"] == 15
        receipt_record = parse_receipt_record(accepted.receipt_record)
        from contextmesh.scripts import rrd_codex_hook

        monkeypatch.setenv("RRC_DEMO_DATABASE", str(tmp_path / "journal.sqlite3"))
        monkeypatch.setenv("RRCV2_OWNER_SCOPE", "owner")
        monkeypatch.setenv("RRC_DEMO_ROUND", "round-1")
        monkeypatch.setenv("RRD_ENABLE_RRC", "1")
        monkeypatch.setenv("RRD_ENABLE_CONTEXTMESH", "1")
        monkeypatch.setenv("RRD_TARGET_ROOT", str(target))
        result = rrd_codex_hook._rrcv2_wait(
            {
                "tool_input": {"targets": ["agent-1"]},
                "tool_response": {
                    "status": {"agent-1": {"completed": final}},
                    "timed_out": False,
                },
            }
        )
        assert result is not None
        stop_reason = result["stopReason"]
        assert isinstance(stop_reason, str)
        envelope = parse_wait_envelope(stop_reason.encode())
        assert len(envelope.targets) == 1
        target_row = envelope.targets[0]
        assert isinstance(target_row, RRCAcceptedTargetV1)
        assert target_row.attempt_id == attempt.attempt_id
        assert target_row.sha256 == _sha(b"def value() -> int:\n    return 1")
        result_file = (
            attempts.load_registered_input(attempt.attempt_id).input_root.parent / target_row.path
        )
        assert result_file.read_bytes() == b"def value() -> int:\n    return 1"
        assert result_file.stat().st_mode & 0o777 == 0o600
        materialize = rrd_codex_hook._materialize_rrcv2_result
        monkeypatch.setattr(
            rrd_codex_hook,
            "_materialize_rrcv2_result",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
        )
        fallback_result = rrd_codex_hook._rrcv2_wait(
            {
                "tool_input": {"targets": ["agent-1"]},
                "tool_response": {
                    "status": {"agent-1": {"completed": final}},
                    "timed_out": False,
                },
            }
        )
        assert fallback_result is not None
        fallback_raw = fallback_result["stopReason"]
        assert isinstance(fallback_raw, str)
        fallback_envelope = parse_wait_envelope(fallback_raw.encode())
        assert isinstance(fallback_envelope.targets[0], RRCNativeFallbackTargetV1)
        assert final not in fallback_raw
        monkeypatch.setattr(rrd_codex_hook, "_materialize_rrcv2_result", materialize)
        applied = apply_receipt(
            database=tmp_path / "journal.sqlite3",
            attempt_id=attempt.attempt_id,
            receipt=receipt_record.receipt,
        )
        assert applied["sha256"] == _sha(b"def value() -> int:\n    return 1")
        assert target.joinpath("solution.py").read_bytes() == b"def value() -> int:\n    return 1"
        assert target.joinpath("solution.py").stat().st_mode & 0o777 == 0o644
        assert (
            apply_receipt(
                database=tmp_path / "journal.sqlite3",
                attempt_id=attempt.attempt_id,
                receipt=receipt_record.receipt,
            )
            == applied
        )
        target.joinpath("solution.py").chmod(0o600)
        with pytest.raises(ResultReaderError, match="preimage differs"):
            apply_receipt(
                database=tmp_path / "journal.sqlite3",
                attempt_id=attempt.attempt_id,
                receipt=receipt_record.receipt,
            )
        assert (
            finish_one(
                attempts,
                model=PrepareModel(),
                config=config,
                owner_id="finisher-2",
            )
            is None
        )


def test_active_hook_uses_canonical_prepare_and_submits_attested_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextmesh.scripts import rrd_codex_hook

    target = tmp_path / "target"
    target.mkdir()
    assignment = _assignment(target)
    database = tmp_path / "hook.sqlite3"
    attempts_root = tmp_path / "attempts"
    codex_home = tmp_path / "codex-home"
    transcript_path = codex_home / "sessions" / "worker.jsonl"
    transcript_path.parent.mkdir(parents=True)
    events = tmp_path / "hook-events.jsonl"
    monkeypatch.setattr(rrd_codex_hook, "CodexModel", lambda **_kwargs: PrepareModel())
    for name, value in {
        "CODEX_HOME": str(codex_home),
        "RRC_DEMO_DATABASE": str(database),
        "RRC_DEMO_ROUND": "round-1",
        "RRCV2_ATTEMPTS_ROOT": str(attempts_root),
        "RRCV2_OWNER_SCOPE": "owner",
        "RRCV2_PARENT_HISTORY_SENTINEL": "parent-private",
        "RRCV2_ROOT_SENTINEL": "root-private",
        "RRD_ENABLE_CONTEXTMESH": "1",
        "RRD_ENABLE_RRC": "1",
        "RRD_HOOK_EVENTS": str(events),
        "RRD_TARGET_ROOT": str(target),
    }.items():
        monkeypatch.setenv(name, value)

    pre = rrd_codex_hook.handle(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_use_id": "tool-1",
            "tool_input": {"message": "Implement the assignment.\n" + assignment.marker()},
        }
    )
    assert pre is not None
    updated = pre["hookSpecificOutput"]["updatedInput"]  # type: ignore[index]
    assert updated["fork_context"] is False
    assert updated["model"] == "gpt-5.6-luna"
    assert "NotImplementedError" not in updated["message"]
    payload = json.loads(updated["message"].splitlines()[-1])
    attempt_id = payload["attempt_id"]

    assert (
        rrd_codex_hook.handle(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "spawn_agent",
                "tool_use_id": "tool-1",
                "tool_response": {"agent_id": "agent-1"},
            }
        )
        is None
    )
    start = rrd_codex_hook.handle(
        {"hook_event_name": "SubagentStart", "agent_id": "agent-1", "agent_type": "worker"}
    )
    assert start is not None
    assert (
        start["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
        == subagent_start_context()
    )
    final = canonical_json_bytes(
        {
            "artifact_path": "solution.py",
            "attempt_id": attempt_id,
            "source": "def value() -> int:\n    return 1",
            "v": 1,
        }
    ).decode()
    transcript_path.write_bytes(_rollout("agent-1", updated["message"], final))
    assert (
        rrd_codex_hook.handle(
            {
                "hook_event_name": "SubagentStop",
                "agent_id": "agent-1",
                "agent_transcript_path": str(transcript_path),
                "last_assistant_message": final,
            }
        )
        == {}
    )
    with SQLiteRRCRepository(database) as repository:
        attempt = AttemptRepository(repository).find_by_agent(
            owner_scope="owner", agent_id="agent-1"
        )
        assert attempt is not None
        assert attempt.attempt_id == attempt_id
        assert attempt.state == "submitted"
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert {row["event"] for row in rows}.issuperset(
        {
            "rrcv2_assignment_prepared",
            "rrcv2_spawn_bound",
            "rrcv2_subagent_started",
            "rrcv2_worker_submitted",
        }
    )


def test_active_hook_terminally_rejects_spoofed_worker_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextmesh.scripts import rrd_codex_hook

    target = tmp_path / "target"
    target.mkdir()
    assignment = _assignment(target)
    database = tmp_path / "hook.sqlite3"
    codex_home = tmp_path / "codex-home"
    transcript_path = codex_home / "sessions" / "worker.jsonl"
    transcript_path.parent.mkdir(parents=True)
    monkeypatch.setattr(rrd_codex_hook, "CodexModel", lambda **_kwargs: PrepareModel())
    for name, value in {
        "CODEX_HOME": str(codex_home),
        "RRC_DEMO_DATABASE": str(database),
        "RRC_DEMO_ROUND": "round-1",
        "RRCV2_ATTEMPTS_ROOT": str(tmp_path / "attempts"),
        "RRCV2_OWNER_SCOPE": "owner",
        "RRCV2_PARENT_HISTORY_SENTINEL": "parent-private",
        "RRCV2_ROOT_SENTINEL": "root-private",
        "RRD_ENABLE_CONTEXTMESH": "1",
        "RRD_ENABLE_RRC": "1",
        "RRD_HOOK_EVENTS": str(tmp_path / "hook-events.jsonl"),
        "RRD_TARGET_ROOT": str(target),
    }.items():
        monkeypatch.setenv(name, value)

    pre = rrd_codex_hook.handle(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_use_id": "tool-1",
            "tool_input": {"message": "Implement.\n" + assignment.marker()},
        }
    )
    assert pre is not None
    updated = pre["hookSpecificOutput"]["updatedInput"]  # type: ignore[index]
    attempt_id = json.loads(updated["message"].splitlines()[-1])["attempt_id"]
    assert (
        rrd_codex_hook.handle(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "spawn_agent",
                "tool_use_id": "tool-1",
                "tool_response": {"agent_id": "agent-1"},
            }
        )
        is None
    )
    final = canonical_json_bytes(
        {
            "artifact_path": "solution.py",
            "attempt_id": attempt_id,
            "source": "def value() -> int:\n    return 1",
            "v": 1,
        }
    ).decode()
    transcript_path.write_bytes(_rollout("agent-1", updated["message"], final))

    assert (
        rrd_codex_hook.handle(
            {
                "hook_event_name": "SubagentStop",
                "agent_id": "agent-1",
                "agent_transcript_path": str(transcript_path),
                "last_assistant_message": final + " ",
            }
        )
        == {}
    )
    with SQLiteRRCRepository(database) as repository:
        attempt = repository.load_attempt(attempt_id)
        assert attempt.state == "rejected"
        terminal = repository.load_terminal_intent(attempt_id)
        assert terminal is not None and terminal[0] == "rejected"
        intent = parse_rejected_commit(terminal[1])
        assert intent.rejected_outcome.phase == "implement"
        assert intent.rejected_outcome.reason == "evidence_invalid"
