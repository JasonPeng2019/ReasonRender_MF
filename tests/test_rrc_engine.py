from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

import pytest
from rrc.contract import (
    ArmMode,
    BranchDecision,
    Candidate,
    Completion,
    Config,
    InlineTaskInputV1,
    ModelRole,
    RunContext,
    ScoreV1,
    Slots,
    Spec,
    StoreFailure,
    StructuralShapeV1,
    TargetPreimageV1,
    Task,
    Usage,
    canonical_json_bytes,
    seal_task_input,
)
from rrc.journal import AttemptHandle, SQLiteRRCRepository
from rrc.pipeline.solve import prepare, solve
from rrc.pipeline.template import templatize
from rrc.pipeline.verify import (
    CodeArtifactV1,
    RepairEvidenceV1,
    VerificationResultV1,
    VerificationRunV1,
    VerificationTierRowV1,
    code_artifact_bytes,
    verification_result_bytes,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


SPEC = Spec(
    "Return the supplied integer.",
    "def f(x: int) -> int",
    "Return x unchanged.",
    ("def test_spec():\n    assert f(3) == 3",),
    Slots(),
)
SPEC_JSON = json.dumps(SPEC.as_json(), sort_keys=True, separators=(",", ":"))
INDEPENDENT = json.dumps(
    {"tests": ["def test_independent():\n    assert f(4) == 4"], "v": 1},
    sort_keys=True,
    separators=(",", ":"),
)


class ScriptedModel:
    provider = "fake"

    def __init__(self, responses: dict[str, list[str | Callable[[str], str]]]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[tuple[ModelRole, str, str]] = []

    def complete(self, role: ModelRole, prompt: str, ctx, stage: str) -> Completion:
        self.calls.append((role, stage, prompt))
        response = self.responses[stage].pop(0)
        text = response(prompt) if callable(response) else response
        return Completion(
            text, Usage(1, 1, 2), "strong-fake" if role is ModelRole.STRONG else "small-fake"
        )


class DeniedProductModel(ScriptedModel):
    def product_call_id(
        self,
        *,
        attempt: AttemptHandle,
        ctx: RunContext,
        branch: str,
        stage: str,
        stage_ordinal: int,
    ) -> str:
        del attempt, ctx, branch, stage, stage_ordinal
        return "a" * 64

    def authorize_product_call(
        self,
        *,
        attempt: AttemptHandle,
        ctx: RunContext,
        branch: str,
        stage: str,
        stage_ordinal: int,
        call_id: str,
    ) -> None:
        del attempt, ctx, branch, stage, stage_ordinal, call_id
        raise ValueError("product permit denied")


def _code(source: str) -> Callable[[str], str]:
    def response(prompt: str) -> str:
        match = re.search(
            r"attempt_id and artifact_path must be exactly '([0-9a-f]{64})' and '([^']+)'", prompt
        )
        assert match is not None
        return json.dumps(
            {
                "artifact_path": match.group(2),
                "attempt_id": match.group(1),
                "source": source,
                "v": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    return response


class Retrieval:
    def __init__(self, repo: SQLiteRRCRepository, template=None) -> None:
        self.authority_id = repo.authority_id
        self.database_uuid = repo.database_uuid
        self.template = template

    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
        if self.template is None:
            return []
        return [Candidate(self.template.external_ref, ScoreV1(1, 1))]

    def get_template(self, external_ref: str):
        if self.template is not None and self.template.external_ref == external_ref:
            return self.template
        return None


def _envelope(tmp_path: Path):
    return seal_task_input(
        InlineTaskInputV1(
            Task(
                "task-1",
                "Implement f so it returns the supplied integer.",
                public_tests=("def test_public():\n    assert f(2) == 2",),
                primary="f",
                shape=StructuralShapeV1(("int",), 1, ()),
                slot_values=(),
            ),
            "def f(x: int) -> int:\n    raise NotImplementedError",
            TargetPreimageV1.none(),
        ),
        input_root=(tmp_path / "input").resolve(),
    )


def _unstructured_envelope(
    tmp_path: Path,
    *,
    task_id: str = "task-general",
    root_name: str = "input-general",
):
    return seal_task_input(
        InlineTaskInputV1(
            Task(
                task_id,
                "Implement f so it returns the supplied integer.",
                public_tests=("def test_public():\n    assert f(2) == 2",),
            ),
            "def f(x: int) -> int:\n    raise NotImplementedError",
            TargetPreimageV1.none(),
        ),
        input_root=(tmp_path / root_name).resolve(),
    )


def _verification(
    *,
    attempt_id: str,
    task: Task,
    source: str,
    tests,
    specification: Spec | None,
):
    del tests
    artifact = CodeArtifactV1(attempt_id, task.artifact_path, source)
    artifact_sha = _sha(code_artifact_bytes(artifact))
    accepted = "BAD" not in source
    names = ["assembly", "ruff"]
    if specification is not None and task.verification_profile == "rrcv2_synthetic_v1":
        names.append("signature_conformance")
    names.extend(("pyright", "pytest"))
    if not accepted:
        names = names[:3]
    rows = tuple(
        VerificationTierRowV1(
            name,  # type: ignore[arg-type]
            "passed" if accepted or index < len(names) - 1 else "failed",
            artifact_sha,
            _sha(b"passed\n" if accepted or index < len(names) - 1 else b"failed\n"),
            _sha(
                b""
                if accepted or index < len(names) - 1
                else canonical_json_bytes({"kind": "verification_failure", "tier": name, "v": 1})
            ),
        )
        for index, name in enumerate(names)
    )
    result = VerificationResultV1(
        attempt_id,
        task.verification_profile,
        artifact_sha,
        rows,
        accepted,
    )
    result_sha = _sha(verification_result_bytes(result))
    return VerificationRunV1(
        result,
        artifact,
        (),
        repair_evidence=(
            None
            if accepted
            else RepairEvidenceV1(
                attempt_id,
                result_sha,
                names[-1],  # type: ignore[arg-type]
                _sha(b"failed"),
                "failed",
            )
        ),
    )


@pytest.fixture(autouse=True)
def fake_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    import rrc.pipeline.solve as solve_module

    monkeypatch.setattr(solve_module, "_run_verifier", _verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: None)


@pytest.mark.parametrize(
    ("mode", "responses", "expected"),
    [
        (
            ArmMode.BASELINE,
            {"baseline": [_code("def f(x: int) -> int:\n    return x")]},
            ["baseline"],
        ),
        (
            ArmMode.CHEAP_ALONE,
            {
                "cheap_alone": [
                    _code("def f(x: int) -> int:\n    # BAD\n    return 0"),
                    _code("def f(x: int) -> int:\n    return x"),
                ],
            },
            ["cheap_alone", "cheap_alone"],
        ),
        (
            ArmMode.CASCADE,
            {
                "cascade_cheap": [
                    _code("def f(x: int) -> int:\n    # BAD\n    return 0"),
                    _code("def f(x: int) -> int:\n    # BAD one\n    return 0"),
                    _code("def f(x: int) -> int:\n    # BAD two\n    return 0"),
                ],
                "cascade_strong": [_code("def f(x: int) -> int:\n    return x")],
            },
            ["cascade_cheap", "cascade_cheap", "cascade_cheap", "cascade_strong"],
        ),
        (
            ArmMode.COLD,
            {
                "spec": [SPEC_JSON],
                "independent_tests": [INDEPENDENT],
                "implement": [_code("def f(x: int) -> int:\n    return x")],
            },
            ["spec", "independent_tests", "implement"],
        ),
        (
            ArmMode.WARM,
            {
                "spec": [SPEC_JSON],
                "independent_tests": [INDEPENDENT],
                "implement": [_code("def f(x: int) -> int:\n    return x")],
            },
            ["spec", "independent_tests", "implement"],
        ),
    ],
)
def test_five_arm_call_matrix(
    tmp_path: Path,
    mode: ArmMode,
    responses: dict[str, list[str | Callable[[str], str]]],
    expected: list[str],
) -> None:
    model = ScriptedModel(responses)
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        outcome = solve(
            _envelope(tmp_path),
            mode=mode,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-" + mode.value,
        )
        assert outcome.passed is True
        assert [event.stage for event in outcome.cost_events] == expected
        ordinal_tables = {
            ArmMode.BASELINE: [1],
            ArmMode.CHEAP_ALONE: [1, 2],
            ArmMode.CASCADE: [1, 2, 3, 4],
            ArmMode.COLD: [1, 2, 3],
            ArmMode.WARM: [2, 3, 4],
        }
        assert [event.stage_ordinal for event in outcome.cost_events] == ordinal_tables[mode]
        for event in outcome.cost_events:
            row = json.loads(event.canonical_bytes())
            assert set(row) == {
                "arm",
                "attempt_id",
                "cached_input_tokens",
                "cell_id",
                "cost_event_id",
                "effective_model",
                "effective_provider",
                "effective_reasoning",
                "effective_service_tier",
                "final_message_sha256",
                "identity_attestation",
                "input_tokens",
                "output_tokens",
                "prompt_sha256",
                "provider_total_tokens",
                "reasoning_output_tokens",
                "requested_model",
                "requested_provider",
                "requested_reasoning",
                "requested_service_tier",
                "stage",
                "stage_ordinal",
                "task_id",
                "transcript_sha256",
                "v",
            }
            assert row["cost_event_id"].startswith("call-")
            assert row["provider_total_tokens"] == row["input_tokens"] + row["output_tokens"]
        assert [stage for _, stage, _ in model.calls] == expected
        assert repo.list_recoverable("owner") == ()


def test_product_permit_denial_precedes_call_journal_and_provider(tmp_path: Path) -> None:
    model = DeniedProductModel({"baseline": [_code("def f(x: int) -> int:\n    return x")]})
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        with pytest.raises(ValueError, match="product permit denied"):
            solve(
                _envelope(tmp_path),
                mode=ArmMode.BASELINE,
                model=model,
                retrieval=Retrieval(repo),
                cfg=Config("owner"),
                journal=repo,
                acceptance=repo,
                operation_key="op-denied-product-permit",
            )
        recoverable = repo.list_recoverable("owner")
        assert len(recoverable) == 1
        assert repo.load_call_inventory(recoverable[0].attempt_id) == ()
        assert model.calls == []


def test_precommit_store_failure_raises_with_completed_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(SQLiteRRCRepository, "commit_accepted", fail_commit)
    model = ScriptedModel({"baseline": [_code("def f(x: int) -> int:\n    return x")]})
    database = tmp_path / "rrc.sqlite3"
    envelope = _envelope(tmp_path)
    with SQLiteRRCRepository(database) as repo:
        with pytest.raises(StoreFailure) as raised:
            solve(
                envelope,
                mode=ArmMode.BASELINE,
                model=model,
                retrieval=Retrieval(repo),
                cfg=Config("owner"),
                journal=repo,
                acceptance=repo,
                operation_key="op-store-failure",
            )
        assert raised.value.outcome.passed is True
        assert raised.value.outcome.code.endswith("return x")
        assert isinstance(raised.value.__cause__, OSError)
        assert repo.list_recoverable("owner") == ()
    with SQLiteRRCRepository(database) as repo:
        retry = solve(
            envelope,
            mode=ArmMode.BASELINE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-store-failure",
        )
        assert retry.passed is False
        assert len(model.calls) == 1


def test_raised_after_durable_acceptance_reconciles_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = SQLiteRRCRepository.commit_accepted

    def commit_then_raise(self: SQLiteRRCRepository, *args: object, **kwargs: object):
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(SQLiteRRCRepository, "commit_accepted", commit_then_raise)
    model = ScriptedModel({"baseline": [_code("def f(x: int) -> int:\n    return x")]})
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        outcome = solve(
            _envelope(tmp_path),
            mode=ArmMode.BASELINE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-raised-after-commit",
        )
        assert outcome.passed is True
        assert [stage for _, stage, _ in model.calls] == ["baseline"]
        assert repo.list_recoverable("owner") == ()


def test_raised_after_durable_rejection_reconciles_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = SQLiteRRCRepository.commit_rejected

    def commit_then_raise(self: SQLiteRRCRepository, *args: object, **kwargs: object):
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        raise OSError("lost rejection acknowledgement")

    monkeypatch.setattr(SQLiteRRCRepository, "commit_rejected", commit_then_raise)
    model = ScriptedModel({"spec": ["not-json"]})
    envelope = _envelope(tmp_path)
    database = tmp_path / "rrc.sqlite3"
    with SQLiteRRCRepository(database) as repo:
        first = solve(
            envelope,
            mode=ArmMode.COLD,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-raised-after-rejection",
        )
        assert first.passed is False
        assert [stage for _, stage, _ in model.calls] == ["spec"]
        assert repo.list_recoverable("owner") == ()

    with SQLiteRRCRepository(database) as repo:
        replay = solve(
            envelope,
            mode=ArmMode.COLD,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-raised-after-rejection",
        )
        assert replay.passed is False
        assert [stage for _, stage, _ in model.calls] == ["spec"]


def test_provider_error_after_launch_commits_ambiguous_rejection_without_replay(
    tmp_path: Path,
) -> None:
    def provider_error(_prompt: str) -> str:
        raise OSError("connection dropped after request launch")

    model = ScriptedModel({"baseline": [provider_error]})
    database = tmp_path / "rrc.sqlite3"
    envelope = _envelope(tmp_path)
    with SQLiteRRCRepository(database) as repo:
        first = solve(
            envelope,
            mode=ArmMode.BASELINE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-ambiguous-provider",
        )
        assert first.passed is False
        assert len(model.calls) == 1
        assert repo.list_recoverable("owner") == ()
    with SQLiteRRCRepository(database) as repo:
        retry = solve(
            envelope,
            mode=ArmMode.BASELINE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-ambiguous-provider",
        )
        assert retry.passed is False
        assert len(model.calls) == 1


def test_warm_exact_reuse_has_no_spec_or_independent_test_call(tmp_path: Path) -> None:
    template = templatize(SPEC, ("def test_independent():\n    assert f(4) == 4",))
    model = ScriptedModel({"implement": [_code("def f(x: int) -> int:\n    return x")]})
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        outcome = solve(
            _envelope(tmp_path),
            mode=ArmMode.WARM,
            model=model,
            retrieval=Retrieval(repo, template),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-warm-reuse",
        )
        assert outcome.passed is True
        assert outcome.branch.value == "reuse"
        assert [stage for _, stage, _ in model.calls] == ["implement"]


def test_warm_rejects_authority_mismatch_before_model_or_retrieval(tmp_path: Path) -> None:
    model = ScriptedModel({})
    with (
        SQLiteRRCRepository(tmp_path / "a.sqlite3") as first,
        SQLiteRRCRepository(tmp_path / "b.sqlite3") as second,
    ):
        retrieval = Retrieval(second)
        with pytest.raises(ValueError, match="authority mismatch"):
            solve(
                _envelope(tmp_path),
                mode=ArmMode.WARM,
                model=model,
                retrieval=retrieval,
                cfg=Config("owner"),
                journal=first,
                acceptance=first,
                operation_key="op-mismatch",
            )
        assert model.calls == []


def test_exhausted_direct_verification_commits_rejection_and_replays_without_model(
    tmp_path: Path,
) -> None:
    responses: dict[str, list[str | Callable[[str], str]]] = {
        "cheap_alone": [
            _code("def f(x: int) -> int:\n    # BAD initial\n    return 0"),
            _code("def f(x: int) -> int:\n    # BAD one\n    return 0"),
            _code("def f(x: int) -> int:\n    # BAD two\n    return 0"),
        ],
    }
    model = ScriptedModel(responses)
    envelope = _envelope(tmp_path)
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        first = solve(
            envelope,
            mode=ArmMode.CHEAP_ALONE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-direct-rejected",
        )
        calls_after_first = list(model.calls)
        second = solve(
            envelope,
            mode=ArmMode.CHEAP_ALONE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-direct-rejected",
        )
        assert first.passed is second.passed is False
        assert first.code == second.code
        assert first.repairs == second.repairs == 2
        assert model.calls == calls_after_first
        assert repo.list_recoverable("owner") == ()


def test_invalid_spec_commits_prepare_rejection_and_replays_without_model(tmp_path: Path) -> None:
    model = ScriptedModel({"spec": ["not-json"]})
    envelope = _envelope(tmp_path)
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        first = solve(
            envelope,
            mode=ArmMode.COLD,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-invalid-spec",
        )
        second = solve(
            envelope,
            mode=ArmMode.COLD,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-invalid-spec",
        )
        assert first.passed is second.passed is False
        assert [stage for _, stage, _ in model.calls] == ["spec"]
        assert [event.stage for event in second.cost_events] == ["spec"]
        assert repo.list_recoverable("owner") == ()


def test_accepted_operation_rehydrates_without_provider_replay(tmp_path: Path) -> None:
    model = ScriptedModel({"baseline": [_code("def f(x: int) -> int:\n    return x")]})
    envelope = _envelope(tmp_path)
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        first = solve(
            envelope,
            mode=ArmMode.BASELINE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-accepted-replay",
        )
        second = solve(
            envelope,
            mode=ArmMode.BASELINE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-accepted-replay",
        )
        assert first.passed is second.passed is True
        assert first.code == second.code
        assert [stage for _, stage, _ in model.calls] == ["baseline"]
        assert [event.stage for event in second.cost_events] == ["baseline"]


@pytest.mark.parametrize(
    ("oracle_value", "expected_score", "expected_status"),
    [
        (True, True, "passed"),
        (False, False, "failed"),
        (RuntimeError("oracle unavailable"), None, "infrastructure_failure"),
    ],
)
def test_oracle_result_never_changes_acceptance_and_rehydrates_exact_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oracle_value: bool | Exception,
    expected_score: bool | None,
    expected_status: str,
) -> None:
    import rrc.pipeline.solve as solve_module

    envelope = seal_task_input(
        InlineTaskInputV1(
            Task(
                "oracle-task",
                "Implement f so it returns the supplied integer.",
                public_tests=("def test_public():\n    assert f(2) == 2",),
                oracle_tests=("def test_oracle():\n    assert f(7) == 7",),
                primary="f",
                shape=StructuralShapeV1(("int",), 1, ()),
                slot_values=(),
            ),
            "def f(x: int) -> int:\n    raise NotImplementedError",
            TargetPreimageV1.none(),
        ),
        input_root=(tmp_path / "oracle-input").resolve(),
    )

    def score(**_kwargs):
        if isinstance(oracle_value, Exception):
            raise oracle_value
        return oracle_value

    monkeypatch.setattr(solve_module, "_score_hidden_oracle", score)
    model = ScriptedModel({"baseline": [_code("def f(x: int) -> int:\n    return x")]})
    with SQLiteRRCRepository(tmp_path / "oracle.sqlite3") as repo:
        first = solve(
            envelope,
            mode=ArmMode.BASELINE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="oracle-op",
        )
        second = solve(
            envelope,
            mode=ArmMode.BASELINE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="oracle-op",
        )
        assert first.passed is second.passed is True
        assert first.pass_at_1 is second.pass_at_1 is expected_score
        assert first.oracle_status == second.oracle_status == expected_status
        assert [stage for _, stage, _ in model.calls] == ["baseline"]
        attempt_id = first.cost_events[0].attempt_id
        assert attempt_id is not None
        assert repo.load_attempt(attempt_id).state == "accepted"


def test_prepared_attempt_rehydrates_without_repeating_spec_or_tests(tmp_path: Path) -> None:
    import rrc.pipeline.solve as solve_module

    envelope = _envelope(tmp_path)
    cfg = Config("owner")
    prepare_model = ScriptedModel({"spec": [SPEC_JSON], "independent_tests": [INDEPENDENT]})
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        attempt = solve_module._begin(  # noqa: SLF001 - exercises crash/restart seam
            envelope,
            mode=ArmMode.COLD,
            cfg=cfg,
            journal=repo,
            operation_key="op-prepared-resume",
        )
        prepared = prepare(
            envelope,
            mode=ArmMode.COLD,
            model=prepare_model,
            retrieval=Retrieval(repo),
            cfg=cfg,
            attempt=attempt,
            journal=repo,
        )
        assert prepared.attempt.state == "prepared"
        resume_model = ScriptedModel({"implement": [_code("def f(x: int) -> int:\n    return x")]})
        outcome = solve(
            envelope,
            mode=ArmMode.COLD,
            model=resume_model,
            retrieval=Retrieval(repo),
            cfg=cfg,
            journal=repo,
            acceptance=repo,
            operation_key="op-prepared-resume",
        )
        assert outcome.passed is True
        assert [stage for _, stage, _ in prepare_model.calls] == ["spec", "independent_tests"]
        assert [stage for _, stage, _ in resume_model.calls] == ["implement"]


def test_warm_general_metadata_fill_is_measured_before_retrieval(tmp_path: Path) -> None:
    metadata = json.dumps(
        {
            "authority": "small_model",
            "family": "identity",
            "primary": "f",
            "shape": {"arg_types": ["int"], "arity": 1, "fields": []},
            "slot_values": {},
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    model = ScriptedModel(
        {
            "metadata_fill": [metadata],
            "spec": [SPEC_JSON],
            "independent_tests": [INDEPENDENT],
            "implement": [_code("def f(x: int) -> int:\n    return x")],
        }
    )
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        outcome = solve(
            _unstructured_envelope(tmp_path),
            mode=ArmMode.WARM,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-metadata-fill",
        )
        assert outcome.passed is True
        assert [stage for _, stage, _ in model.calls] == [
            "metadata_fill",
            "spec",
            "independent_tests",
            "implement",
        ]
        assert "STARTER_SOURCE" not in model.calls[0][2]


@pytest.mark.parametrize("mode", [ArmMode.COLD, ArmMode.WARM])
def test_miss_exhaustion_runs_one_fresh_spec_fallback(mode: ArmMode, tmp_path: Path) -> None:
    model = ScriptedModel(
        {
            "spec": [SPEC_JSON],
            "independent_tests": [INDEPENDENT],
            "implement": [_code("def f(x: int) -> int:\n    # BAD initial\n    return 0")],
            "repair_1": [_code("def f(x: int) -> int:\n    # BAD one\n    return 0")],
            "repair_2": [_code("def f(x: int) -> int:\n    # BAD two\n    return 0")],
            "fallback_spec": [SPEC_JSON],
            "fallback_independent_tests": [INDEPENDENT],
            "fallback_implement": [_code("def f(x: int) -> int:\n    return x")],
        }
    )
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        outcome = solve(
            _envelope(tmp_path),
            mode=mode,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-fallback-" + mode.value,
        )
        assert outcome.passed is True
        assert outcome.escalated is True
        assert [stage for _, stage, _ in model.calls] == [
            "spec",
            "independent_tests",
            "implement",
            "repair_1",
            "repair_2",
            "fallback_spec",
            "fallback_independent_tests",
            "fallback_implement",
        ]


def test_failed_fallback_terminal_evidence_binds_fallback_candidate_and_result(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        {
            "spec": [SPEC_JSON],
            "independent_tests": [INDEPENDENT],
            "implement": [_code("def f(x: int) -> int:\n    # BAD initial\n    return 0")],
            "repair_1": [_code("def f(x: int) -> int:\n    # BAD one\n    return 0")],
            "repair_2": [_code("def f(x: int) -> int:\n    # BAD two\n    return 0")],
            "fallback_spec": [SPEC_JSON],
            "fallback_independent_tests": [INDEPENDENT],
            "fallback_implement": [
                _code("def f(x: int) -> int:\n    # BAD fallback\n    return -1")
            ],
        }
    )
    envelope = _envelope(tmp_path)
    database = tmp_path / "fallback-rejected.sqlite3"
    with SQLiteRRCRepository(database) as repo:
        first = solve(
            envelope,
            mode=ArmMode.COLD,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-fallback-rejected",
        )
        assert first.passed is False
        assert "BAD fallback" in first.code
        assert "BAD two" not in first.code
        assert [event.stage for event in first.cost_events] == [
            "spec",
            "independent_tests",
            "implement",
            "repair_1",
            "repair_2",
            "fallback_spec",
            "fallback_independent_tests",
            "fallback_implement",
        ]
        attempt_id = first.cost_events[0].attempt_id
        assert attempt_id is not None
        terminal = repo.load_terminal_intent(attempt_id)
        assert terminal is not None and terminal[0] == "rejected"
        row = json.loads(terminal[1])
        rejected = row["rejected_outcome"]
        assert rejected["reason"] == "fallback_failed"
        assert rejected["valid_candidate_sha256"] is not None
        assert rejected["verification_result_sha256"] is not None

    with SQLiteRRCRepository(database) as repo:
        replay = solve(
            envelope,
            mode=ArmMode.COLD,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-fallback-rejected",
        )
        assert replay.passed is False
        assert replay.code == first.code
        assert [event.cost_event_id for event in replay.cost_events] == [
            event.cost_event_id for event in first.cost_events
        ]
        assert len(model.calls) == 8


def test_invalid_repair_supersedes_old_verification_evidence_in_next_prompt(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        {
            "cheap_alone": [
                _code("def f(x: int) -> int:\n    # BAD initial\n    return 0"),
                "not-json",
                _code("def f(x: int) -> int:\n    return x"),
            ],
        }
    )
    with SQLiteRRCRepository(tmp_path / "invalid-repair.sqlite3") as repo:
        outcome = solve(
            _envelope(tmp_path),
            mode=ArmMode.CHEAP_ALONE,
            model=model,
            retrieval=Retrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-invalid-repair",
        )
    assert outcome.passed is True
    repair_two_prompt = model.calls[2][2]
    assert "PUBLIC_FAILURE:\ncandidate_parse:malformed_json" in repair_two_prompt
    assert "CURRENT_CODE:" not in repair_two_prompt


def test_local_sqlite_warm_miss_then_exact_reuse(tmp_path: Path) -> None:
    from rrc.retrieval import SQLiteHybridRetrieval

    metadata = json.dumps(
        {
            "authority": "small_model",
            "family": "identity",
            "primary": "f",
            "shape": {"arg_types": ["int"], "arity": 1, "fields": []},
            "slot_values": {},
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    first_model = ScriptedModel(
        {
            "metadata_fill": [metadata],
            "spec": [SPEC_JSON],
            "independent_tests": [INDEPENDENT],
            "implement": [_code("def f(x: int) -> int:\n    return x")],
        }
    )
    second_model = ScriptedModel(
        {
            "metadata_fill": [metadata],
            "implement": [_code("def f(x: int) -> int:\n    return x")],
        }
    )
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        retrieval = SQLiteHybridRetrieval(repo)
        first = solve(
            _unstructured_envelope(tmp_path),
            mode=ArmMode.WARM,
            model=first_model,
            retrieval=retrieval,
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-local-miss",
        )
        second = solve(
            _unstructured_envelope(
                tmp_path,
                task_id="task-general-2",
                root_name="input-general-2",
            ),
            mode=ArmMode.WARM,
            model=second_model,
            retrieval=retrieval,
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-local-reuse",
        )
        assert first.branch is BranchDecision.MISS
        assert second.branch is BranchDecision.REUSE
        assert [stage for _, stage, _ in second_model.calls] == ["metadata_fill", "implement"]


def test_local_sqlite_near_shape_uses_prime_without_strong_spec(tmp_path: Path) -> None:
    from rrc.retrieval import SQLiteHybridRetrieval

    metadata_one = json.dumps(
        {
            "authority": "small_model",
            "family": "identity",
            "primary": "f",
            "shape": {"arg_types": ["int"], "arity": 1, "fields": []},
            "slot_values": {},
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    metadata_two = json.dumps(
        {
            "authority": "small_model",
            "family": "identity",
            "primary": "f",
            "shape": {"arg_types": ["int", "int"], "arity": 2, "fields": []},
            "slot_values": {},
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    near_spec = Spec(
        "Return the first integer.",
        "def f(x: int, y: int) -> int",
        "Return x unchanged.",
        ("def test_spec():\n    assert f(3, 4) == 3",),
        Slots(),
    )
    near_spec_json = json.dumps(
        near_spec.as_json(),
        sort_keys=True,
        separators=(",", ":"),
    )
    first_model = ScriptedModel(
        {
            "metadata_fill": [metadata_one],
            "spec": [SPEC_JSON],
            "independent_tests": [INDEPENDENT],
            "implement": [_code("def f(x: int) -> int:\n    return x")],
        }
    )
    second_model = ScriptedModel(
        {
            "metadata_fill": [metadata_two],
            "prime": [near_spec_json],
            "independent_tests": [
                json.dumps(
                    {"tests": ["def test_independent():\n    assert f(4, 5) == 4"], "v": 1},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ],
            "implement": [_code("def f(x: int, y: int) -> int:\n    return x")],
        }
    )
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        retrieval = SQLiteHybridRetrieval(repo)
        first = solve(
            _unstructured_envelope(tmp_path),
            mode=ArmMode.WARM,
            model=first_model,
            retrieval=retrieval,
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-near-seed",
        )
        second = solve(
            _unstructured_envelope(
                tmp_path,
                task_id="task-near",
                root_name="input-near",
            ),
            mode=ArmMode.WARM,
            model=second_model,
            retrieval=retrieval,
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-near-prime",
        )
        assert first.branch is BranchDecision.MISS
        assert second.branch is BranchDecision.PRIME
        assert [stage for _, stage, _ in second_model.calls] == [
            "metadata_fill",
            "prime",
            "independent_tests",
            "implement",
        ]


def test_local_sqlite_prime_unfit_pays_prime_then_runs_fresh_miss(tmp_path: Path) -> None:
    from rrc.retrieval import SQLiteHybridRetrieval

    metadata_one = json.dumps(
        {
            "authority": "small_model",
            "family": "identity",
            "primary": "f",
            "shape": {"arg_types": ["int"], "arity": 1, "fields": []},
            "slot_values": {},
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    metadata_two = json.dumps(
        {
            "authority": "small_model",
            "family": "identity",
            "primary": "f",
            "shape": {"arg_types": ["int", "int"], "arity": 2, "fields": []},
            "slot_values": {},
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    miss_spec = Spec(
        "Return the first integer.",
        "def f(x: int, y: int) -> int",
        "Return x unchanged.",
        ("def test_spec():\n    assert f(3, 4) == 3",),
        Slots(),
    )
    first_model = ScriptedModel(
        {
            "metadata_fill": [metadata_one],
            "spec": [SPEC_JSON],
            "independent_tests": [INDEPENDENT],
            "implement": [_code("def f(x: int) -> int:\n    return x")],
        }
    )
    second_model = ScriptedModel(
        {
            "metadata_fill": [metadata_two],
            "prime": ['{"unfit":true}'],
            "spec": [json.dumps(miss_spec.as_json(), sort_keys=True, separators=(",", ":"))],
            "independent_tests": [
                json.dumps(
                    {"tests": ["def test_independent():\n    assert f(4, 5) == 4"], "v": 1},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ],
            "implement": [_code("def f(x: int, y: int) -> int:\n    return x")],
        }
    )
    with SQLiteRRCRepository(tmp_path / "rrc.sqlite3") as repo:
        retrieval = SQLiteHybridRetrieval(repo)
        seed = solve(
            _unstructured_envelope(tmp_path),
            mode=ArmMode.WARM,
            model=first_model,
            retrieval=retrieval,
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-prime-unfit-seed",
        )
        outcome = solve(
            _unstructured_envelope(
                tmp_path,
                task_id="task-prime-unfit",
                root_name="input-prime-unfit",
            ),
            mode=ArmMode.WARM,
            model=second_model,
            retrieval=retrieval,
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-prime-unfit",
        )
        assert seed.branch is BranchDecision.MISS
        assert outcome.branch is BranchDecision.MISS
        assert outcome.passed is True
        assert [stage for _, stage, _ in second_model.calls] == [
            "metadata_fill",
            "prime",
            "spec",
            "independent_tests",
            "implement",
        ]
        assert [event.stage for event in outcome.cost_events] == [
            "metadata_fill",
            "prime",
            "spec",
            "independent_tests",
            "implement",
        ]
        identities = [event.cost_event_id for event in outcome.cost_events]
        assert len(identities) == len(set(identities)) == 5
        attempt_id = outcome.cost_events[0].attempt_id
        assert attempt_id is not None
        assert [state for _call_id, state, _event in repo.load_call_inventory(attempt_id)] == [
            "call_committed"
        ] * 5


def test_projection_unavailable_warm_acceptance_stores_bundle_and_reason_only(
    tmp_path: Path,
) -> None:
    from rrc.retrieval import SQLiteHybridRetrieval, parse_projection_unavailable

    specification = Spec(
        "Use SecretX.",
        "def f(SecretX: int) -> int",
        "Return SecretX.",
        ("def test_spec():\n    assert f(SecretX=1) == 1",),
        Slots(identifiers=("SecretX",)),
    )
    independent = json.dumps(
        {"tests": ["def test_independent():\n    assert f(SecretX=2) == 2"], "v": 1},
        sort_keys=True,
        separators=(",", ":"),
    )
    task = Task(
        "task-projection",
        "slot_secret SecretX",
        public_tests=("def test_public():\n    assert f(2) == 2",),
        family="lookup",
        primary="f",
        shape=StructuralShapeV1(("int",), 1, ()),
        slot_values=(("secret", "SecretX"),),
    )
    envelope = seal_task_input(
        InlineTaskInputV1(
            task,
            "def f(SecretX: int) -> int:\n    raise NotImplementedError",
            TargetPreimageV1.none(),
        ),
        input_root=(tmp_path / "projection-input").resolve(),
    )
    model = ScriptedModel(
        {
            "spec": [json.dumps(specification.as_json(), sort_keys=True, separators=(",", ":"))],
            "independent_tests": [independent],
            "implement": [_code("def f(SecretX: int) -> int:\n    return SecretX")],
        }
    )
    with SQLiteRRCRepository(tmp_path / "projection.sqlite3") as repo:
        outcome = solve(
            envelope,
            mode=ArmMode.WARM,
            model=model,
            retrieval=SQLiteHybridRetrieval(repo),
            cfg=Config("owner"),
            journal=repo,
            acceptance=repo,
            operation_key="op-projection-unavailable",
        )
        assert outcome.passed is True
        assert outcome.template is not None
        assert repo.get_bundle(outcome.template.external_ref) is not None
        assert repo.retrieval_observations("owner") == ()
        assert repo.pending_everos_outbox("owner") == ()
        attempt_id = outcome.cost_events[0].attempt_id
        assert attempt_id is not None
        attempt = repo.load_attempt(attempt_id)
        raw = repo.load_projection_unavailable(attempt.attempt_id, "store")
        assert raw is not None
        reason = parse_projection_unavailable(raw)
        assert reason.code == "reserved_token"
        assert reason.input_sha256 == _sha(task.text.encode())
        query_raw = repo.load_projection_unavailable(attempt.attempt_id, "query")
        assert query_raw is not None
        assert parse_projection_unavailable(query_raw).code == "reserved_token"
