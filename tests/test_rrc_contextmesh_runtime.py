from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from rrc.attempts import AttemptRepository
from rrc.contextmesh import CodingAssignmentV1
from rrc.contextmesh_runtime import (
    ContextMeshController,
    derive_native_worker_result,
    load_memory_runtime,
    subagent_start_context,
)
from rrc.contract import (
    Candidate,
    Completion,
    Config,
    ModelRole,
    ScoreV1,
    Slots,
    Spec,
    StructuralShapeV1,
    TargetPreimageV1,
    Task,
    Usage,
    canonical_test_artifact_bytes,
)
from rrc.everos import EverOSHybridRetrieval, EverOSIsolationV1, EverOSTargetV1
from rrc.journal import SQLiteRRCRepository
from rrc.pipeline.template import templatize
from rrc.retrieval import SQLiteHybridRetrieval


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Model:
    provider = "openai"

    def __init__(self) -> None:
        self.calls: list[tuple[ModelRole, str, str]] = []

    def complete(self, role: ModelRole, prompt: str, ctx, stage: str) -> Completion:
        del ctx
        self.calls.append((role, stage, prompt))
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
            raise AssertionError(f"unexpected stage {stage}")
        return Completion(text, Usage(10, 5, 15), "test-model")


class PrimeUnfitModel(Model):
    def complete(self, role: ModelRole, prompt: str, ctx, stage: str) -> Completion:
        del ctx
        self.calls.append((role, stage, prompt))
        if stage == "prime":
            text = '{"unfit":true}'
        elif stage == "spec":
            text = json.dumps(
                {
                    "contract": "Return the first integer.",
                    "plan": "Implement value with two integer arguments.",
                    "signature": "def value(x: int, y: int) -> int",
                    "slots": {
                        "constants": [],
                        "edge_values": [],
                        "entity": None,
                        "fields": [],
                        "identifiers": [],
                        "types": [],
                    },
                    "tests": ["def test_spec():\n    assert value(3, 4) == 3"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        elif stage == "independent_tests":
            text = json.dumps(
                {"tests": ["def test_independent():\n    assert value(5, 6) == 5"], "v": 1},
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            raise AssertionError(f"unexpected stage {stage}")
        return Completion(text, Usage(10, 5, 15), "test-model")


class NearRetrieval:
    def __init__(self, repository: SQLiteRRCRepository) -> None:
        self.authority_id = repository.authority_id
        self.database_uuid = repository.database_uuid
        self.template = templatize(
            Spec(
                "Return the integer.",
                "def value(x: int) -> int",
                "Return x unchanged.",
                ("def test_spec():\n    assert value(3) == 3",),
                Slots(),
            ),
            ("def test_independent():\n    assert value(5) == 5",),
            primary="value",
        )

    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
        del task, cfg
        return [Candidate(self.template.external_ref, ScoreV1(1, 1))]

    def get_template(self, external_ref: str):
        return self.template if external_ref == self.template.external_ref else None

    def classify(self, task: Task, external_ref: str) -> str:
        del task, external_ref
        return "near"


def _assignment(target: Path) -> CodingAssignmentV1:
    source = b"def value() -> int:\n    raise NotImplementedError"
    (target / "solution.py").write_bytes(source)
    (target / "solution.py").chmod(0o644)
    (target / "tests").mkdir()
    public = canonical_test_artifact_bytes(("def test_public():\n    assert value() == 1",))
    (target / "tests/public.v1.json").write_bytes(public)
    (target / "tests/public.v1.json").chmod(0o600)
    hidden = canonical_test_artifact_bytes(("def test_hidden():\n    assert value() == 1",))
    (target / "tests/hidden.v1.json").write_bytes(hidden)
    (target / "tests/hidden.v1.json").chmod(0o600)
    return CodingAssignmentV1(
        mode="cold",
        task=Task("task-1", "Implement value()."),
        source_path="solution.py",
        public_test_path="tests/public.v1.json",
        oracle_test_path="tests/hidden.v1.json",
        owned_paths=("solution.py",),
        target_preimage=TargetPreimageV1.regular("solution.py", _sha(source), len(source), 0o644),
    )


def _prime_unfit_assignment(target: Path) -> CodingAssignmentV1:
    source = b"def value(x: int, y: int) -> int:\n    raise NotImplementedError"
    (target / "solution.py").write_bytes(source)
    (target / "solution.py").chmod(0o644)
    (target / "tests").mkdir()
    public = canonical_test_artifact_bytes(("def test_public():\n    assert value(2, 3) == 2",))
    (target / "tests/public.v1.json").write_bytes(public)
    (target / "tests/public.v1.json").chmod(0o600)
    return CodingAssignmentV1(
        mode="warm",
        task=Task(
            "task-prime-unfit",
            "Implement value(x, y) so it returns x.",
            family="identity",
            primary="value",
            shape=StructuralShapeV1(("int", "int"), 2, ()),
            slot_values=(),
        ),
        source_path="solution.py",
        public_test_path="tests/public.v1.json",
        oracle_test_path=None,
        owned_paths=("solution.py",),
        target_preimage=TargetPreimageV1.regular("solution.py", _sha(source), len(source), 0o644),
    )


def test_controller_prepares_once_and_delivers_no_source_to_worker(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    assignment = _assignment(target)
    model = Model()
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repository:
        attempts = AttemptRepository(repository)
        controller = ContextMeshController(
            attempts=attempts,
            model=model,
            retrieval=SQLiteHybridRetrieval(repository),
            config=Config("owner"),
            target_root=target,
            attempts_root=tmp_path / "attempts",
            route_id="route-1",
            round_id="round-1",
        )
        first = controller.prepare(assignment, tool_use_id="tool-1")
        second = controller.prepare(assignment, tool_use_id="tool-1")

        assert first.prepared.attempt.state == "prepared"
        assert second.prepared.attempt.attempt_id == first.prepared.attempt.attempt_id
        assert [stage for _role, stage, _prompt in model.calls] == [
            "spec",
            "independent_tests",
        ]
        assert "raise NotImplementedError" not in first.worker_prompt
        assert "test_hidden" not in first.worker_prompt
        assert "test_public" not in first.worker_prompt
        assert first.worker_prompt.count(first.prepared.attempt.attempt_id) == 1
        assert '"specification"' in first.worker_prompt
        registered = attempts.load_registered_input(first.prepared.attempt.attempt_id)
        assert registered.task_envelope.target_preimage == assignment.target_preimage
        assert registered.expected_tool_use_id == "tool-1"


def test_contextmesh_prime_unfit_is_journalled_then_delivers_fresh_miss(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    assignment = _prime_unfit_assignment(target)
    model = PrimeUnfitModel()
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repository:
        controller = ContextMeshController(
            attempts=AttemptRepository(repository),
            model=model,
            retrieval=NearRetrieval(repository),
            config=Config("owner"),
            target_root=target,
            attempts_root=tmp_path / "attempts",
            route_id="route-prime-unfit",
            round_id="round-prime-unfit",
        )
        prepared = controller.prepare(assignment, tool_use_id="tool-prime-unfit")

        assert prepared.prepared.branch.value == "miss"
        assert [stage for _role, stage, _prompt in model.calls] == [
            "prime",
            "spec",
            "independent_tests",
        ]
        assert [event.stage for event in prepared.prepared.cost_events] == [
            "prime",
            "spec",
            "independent_tests",
        ]
        assert len(set(prepared.prepared.call_ids)) == len(prepared.prepared.call_ids) == 3
        assert '"branch":"miss"' in prepared.worker_prompt
        assert "raise NotImplementedError" not in prepared.worker_prompt
        assert [
            state
            for _call_id, state, _event in repository.load_call_inventory(
                prepared.prepared.attempt.attempt_id
            )
        ] == ["call_committed", "call_committed", "call_committed", "call_prepared"]


def test_controller_rejects_changed_source_before_any_model_call(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    assignment = _assignment(target)
    (target / "solution.py").write_text("changed")
    model = Model()
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repository:
        controller = ContextMeshController(
            attempts=AttemptRepository(repository),
            model=model,
            retrieval=SQLiteHybridRetrieval(repository),
            config=Config("owner"),
            target_root=target,
            attempts_root=tmp_path / "attempts",
            route_id="route-1",
            round_id="round-1",
        )
        with pytest.raises(ValueError, match="target preimage"):
            controller.prepare(assignment, tool_use_id="tool-1")
        assert model.calls == []


def _message(role: str, text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _worker_rollout(*, agent_id: str, worker_prompt: str, final: str) -> bytes:
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
        {"type": "event_msg", "payload": {"type": "task_started"}},
        _message("developer", "base developer"),
        _message("user", "environment"),
        {"type": "world_state", "payload": {}},
        {
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-luna", "effort": "low"},
        },
        _message("developer", subagent_start_context()),
        _message("user", worker_prompt),
        {"type": "event_msg", "payload": {"type": "user_message", "message": worker_prompt}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": final}},
        _message("assistant", final),
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 130,
                    }
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    return b"".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n" for row in rows
    )


def test_native_worker_result_is_bound_to_source_blind_transcript(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    assignment = _assignment(target)
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repository:
        controller = ContextMeshController(
            attempts=AttemptRepository(repository),
            model=Model(),
            retrieval=SQLiteHybridRetrieval(repository),
            config=Config("owner"),
            target_root=target,
            attempts_root=tmp_path / "attempts",
            route_id="route-1",
            round_id="round-1",
        )
        prepared = controller.prepare(assignment, tool_use_id="tool-1")
        final = json.dumps(
            {
                "artifact_path": "solution.py",
                "attempt_id": prepared.prepared.attempt.attempt_id,
                "source": "def value() -> int:\n    return 1",
                "v": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        raw = _worker_rollout(agent_id="agent-1", worker_prompt=prepared.worker_prompt, final=final)

        result = derive_native_worker_result(
            registered_assignment=prepared,
            tool_use_id="tool-1",
            agent_id="agent-1",
            final_message=final,
            transcript_raw=raw,
            root_sentinel="root-private-sentinel",
            parent_history_sentinel="parent-history-sentinel",
        )

        assert result.candidate.kind == "code"
        assert result.worker_evidence.provider_total_tokens == 130
        assert result.worker_evidence.effective_model == "gpt-5.6-luna"
        assert result.worker_evidence.context_attestation_sha256 == _sha(
            result.context_attestation.canonical_bytes()
        )
        assert tuple(row.role for row in result.context_attestation.initial_messages) == (
            "developer",
            "user",
            "developer",
            "user",
        )


def test_native_worker_result_rejects_inherited_root_sentinel(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    assignment = _assignment(target)
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repository:
        controller = ContextMeshController(
            attempts=AttemptRepository(repository),
            model=Model(),
            retrieval=SQLiteHybridRetrieval(repository),
            config=Config("owner"),
            target_root=target,
            attempts_root=tmp_path / "attempts",
            route_id="route-1",
            round_id="round-1",
        )
        prepared = controller.prepare(assignment, tool_use_id="tool-1")
        final = json.dumps(
            {
                "artifact_path": "solution.py",
                "attempt_id": prepared.prepared.attempt.attempt_id,
                "source": "def value() -> int:\n    return 1",
                "v": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        raw = _worker_rollout(
            agent_id="agent-1",
            worker_prompt=prepared.worker_prompt,
            final=final,
        ).replace(b"base developer", b"base root-private-sentinel developer")
        with pytest.raises(ValueError, match="forbidden root context"):
            derive_native_worker_result(
                registered_assignment=prepared,
                tool_use_id="tool-1",
                agent_id="agent-1",
                final_message=final,
                transcript_raw=raw,
                root_sentinel="root-private-sentinel",
                parent_history_sentinel="parent-history-sentinel",
            )


def test_memory_runtime_selects_sqlite_without_everos(tmp_path: Path) -> None:
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repository:
        config, retrieval = load_memory_runtime(repository, owner_scope="owner", backend="sqlite")
    assert config.memory_backend == "sqlite"
    assert isinstance(retrieval, SQLiteHybridRetrieval)


def test_memory_runtime_reopens_sealed_everos_target(tmp_path: Path) -> None:
    target = EverOSTargetV1(
        "http://127.0.0.1:8000/api/v2/memory",
        "http://127.0.0.1:8000/health",
        "rrcv2-cm-fixture",
        EverOSIsolationV1("instance-fixture", "d" * 64, "owner"),
    )
    target_path = tmp_path / "everos-target.json"
    target_path.write_bytes(target.canonical_bytes())
    target_path.chmod(0o600)
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repository:
        config, retrieval = load_memory_runtime(
            repository,
            owner_scope="owner",
            backend="everos",
            everos_target_path=target_path,
        )
    assert config.everos_target == target.canonical_bytes()
    assert isinstance(retrieval, EverOSHybridRetrieval)


def test_memory_runtime_rejects_unsealed_or_mismatched_everos_target(tmp_path: Path) -> None:
    target = EverOSTargetV1(
        "http://127.0.0.1:8000/api/v2/memory",
        "http://127.0.0.1:8000/health",
        "rrcv2-cm-fixture",
        EverOSIsolationV1("instance-fixture", "d" * 64, "other-owner"),
    )
    target_path = tmp_path / "everos-target.json"
    target_path.write_bytes(target.canonical_bytes())
    target_path.chmod(0o644)
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repository:
        with pytest.raises(ValueError, match="mode 0600"):
            load_memory_runtime(
                repository,
                owner_scope="owner",
                backend="everos",
                everos_target_path=target_path,
            )
        target_path.chmod(0o600)
        with pytest.raises(ValueError, match="owner differs"):
            load_memory_runtime(
                repository,
                owner_scope="owner",
                backend="everos",
                everos_target_path=target_path,
            )
