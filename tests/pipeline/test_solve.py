from __future__ import annotations

import json

from rrc.pipeline.solve import WorkerCandidateV1, parse_worker_candidate


def test_worker_candidate_accepts_only_the_exact_attempt_and_path() -> None:
    attempt = "1" * 64
    raw = json.dumps(
        {
            "artifact_path": "solution.py",
            "attempt_id": attempt,
            "source": "def f() -> int:\n    return 1",
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    candidate = parse_worker_candidate(raw, attempt_id=attempt, artifact_path="solution.py")
    assert isinstance(candidate, WorkerCandidateV1)
    assert candidate.kind == "code"
    assert candidate.artifact is not None


def test_worker_candidate_path_substitution_is_typed_invalid() -> None:
    attempt = "1" * 64
    raw = json.dumps(
        {
            "artifact_path": "other.py",
            "attempt_id": attempt,
            "source": "pass",
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    candidate = parse_worker_candidate(raw, attempt_id=attempt, artifact_path="solution.py")
    assert candidate.kind == "invalid_candidate"
    assert candidate.reason == "artifact_path_mismatch"


def test_worker_candidate_malformed_and_oversize_are_bounded_invalids() -> None:
    attempt = "1" * 64
    malformed = parse_worker_candidate("not json", attempt_id=attempt, artifact_path="solution.py")
    oversized = parse_worker_candidate(
        b"x" * (2 * 1024 * 1024 + 1),
        attempt_id=attempt,
        artifact_path="solution.py",
    )
    assert (malformed.kind, malformed.reason, malformed.observed_utf8_bytes) == (
        "invalid_candidate",
        "malformed_json",
        8,
    )
    assert (oversized.kind, oversized.reason, oversized.observed_utf8_bytes) == (
        "invalid_candidate",
        "oversize",
        2 * 1024 * 1024 + 1,
    )


def test_warm_sequence_preserves_exact_render_rejection_before_fresh_miss(
    tmp_path, monkeypatch
) -> None:
    """The M6 credibility sequence is MISS -> REUSE -> exact reject -> fresh MISS."""

    import hashlib
    import re
    from collections.abc import Callable

    import rrc.pipeline.solve as solve_module
    from rrc.contract import (
        ArmMode,
        BranchDecision,
        Completion,
        Config,
        InlineTaskInputV1,
        ModelRole,
        Slots,
        Spec,
        StructuralShapeV1,
        TargetPreimageV1,
        Task,
        Usage,
        seal_task_input,
    )
    from rrc.journal import SQLiteRRCRepository
    from rrc.pipeline.solve import solve
    from rrc.pipeline.verify import (
        CodeArtifactV1,
        VerificationResultV1,
        VerificationRunV1,
        VerificationTierRowV1,
        code_artifact_bytes,
    )
    from rrc.retrieval import SQLiteHybridRetrieval

    def verification(*, attempt_id, task, source, tests, specification):
        del tests, specification
        artifact = CodeArtifactV1(attempt_id, task.artifact_path, source)
        artifact_sha = hashlib.sha256(code_artifact_bytes(artifact)).hexdigest()
        rows = tuple(
            VerificationTierRowV1(
                name,
                "passed",
                artifact_sha,
                hashlib.sha256(b"passed\n").hexdigest(),
                hashlib.sha256(b"").hexdigest(),
            )
            for name in ("assembly", "ruff", "pyright", "pytest")
        )
        return VerificationRunV1(
            VerificationResultV1(attempt_id, task.verification_profile, artifact_sha, rows, True),
            artifact,
            (),
        )

    monkeypatch.setattr(solve_module, "_run_verifier", verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: True)

    class Model:
        provider = "fake"

        def __init__(self, responses: dict[str, list[str | Callable[[str], str]]]) -> None:
            self.responses = {key: list(value) for key, value in responses.items()}
            self.calls: list[tuple[ModelRole, str]] = []

        def complete(self, role, prompt, ctx, stage):
            del ctx
            self.calls.append((role, stage))
            response = self.responses[stage].pop(0)
            text = response(prompt) if callable(response) else response
            return Completion(text, Usage(1, 1, 2), "fake-model")

    def code(source: str) -> Callable[[str], str]:
        def response(prompt: str) -> str:
            match = re.search(
                r"attempt_id and artifact_path must be exactly '([0-9a-f]{64})' and '([^']+)'",
                prompt,
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

    def envelope(name: str, primary: str, entity: str, starter: str, public: str, oracle: str):
        task = Task(
            f"rrcv2-cli-{name}-001",
            (
                f'Implement {primary} so it returns the entity label "{entity}". '
                + "Preserve the zero-argument public API."
            ),
            family="entity_label",
            artifact_path=f"rrcv2_demo/{name}.py",
            public_tests=(public,),
            oracle_tests=(oracle,),
            verification_profile="rrcv2_general_v1",
            primary=primary,
            shape=StructuralShapeV1((), 0, ()),
            slot_values=(("entity", entity),),
        )
        return seal_task_input(
            InlineTaskInputV1(task, starter, TargetPreimageV1.none()),
            input_root=(tmp_path / f"input-{name}").resolve(),
        )

    miss_spec = Spec(
        "Implement the User label method.",
        "class User:\n    def label(self) -> str: ...",
        "User.label returns the string User.",
        ('def test_label():\n    assert User().label() == "User"',),
        Slots(entity="User"),
    )
    near_spec = Spec(
        "Implement label_entity for order item.",
        "def label_entity() -> str: ...",
        "label_entity returns the string order item.",
        ('def test_label_entity():\n    assert label_entity() == "order item"',),
        Slots(entity="order item"),
    )
    independent_miss = json.dumps(
        {"tests": ['def test_stable():\n    assert User().label() == "User"'], "v": 1},
        sort_keys=True,
        separators=(",", ":"),
    )
    independent_near = json.dumps(
        {"tests": ['def test_stable():\n    assert label_entity() == "order item"'], "v": 1},
        sort_keys=True,
        separators=(",", ":"),
    )
    models = (
        Model(
            {
                "spec": [json.dumps(miss_spec.as_json(), sort_keys=True, separators=(",", ":"))],
                "independent_tests": [independent_miss],
                "implement": [
                    code('class User:\n    def label(self) -> str:\n        return "User"')
                ],
            }
        ),
        Model(
            {
                "implement": [
                    code('class Product:\n    def label(self) -> str:\n        return "Product"')
                ]
            }
        ),
        Model(
            {
                "spec": [json.dumps(near_spec.as_json(), sort_keys=True, separators=(",", ":"))],
                "independent_tests": [independent_near],
                "implement": [code('def label_entity() -> str:\n    return "order item"')],
            }
        ),
    )
    envelopes = (
        envelope(
            "miss",
            "User.label",
            "User",
            "class User:\n    def label(self) -> str:\n        raise NotImplementedError",
            'def test_user_label():\n    assert User().label() == "User"',
            'def test_user_label_stable():\n    assert User().label() == "User"',
        ),
        envelope(
            "hit",
            "Product.label",
            "Product",
            "class Product:\n    def label(self) -> str:\n        raise NotImplementedError",
            'def test_product_label():\n    assert Product().label() == "Product"',
            'def test_product_label_stable():\n    assert Product().label() == "Product"',
        ),
        envelope(
            "near",
            "label_entity",
            "order item",
            "def label_entity() -> str:\n    raise NotImplementedError",
            'def test_label_entity():\n    assert label_entity() == "order item"',
            'def test_label_entity_stable():\n    assert label_entity() == "order item"',
        ),
    )

    with SQLiteRRCRepository(tmp_path / "rrcv2.sqlite3") as repository:
        retrieval = SQLiteHybridRetrieval(repository)
        outcomes = [
            solve(
                task_input,
                mode=ArmMode.WARM,
                model=model,
                retrieval=retrieval,
                cfg=Config("owner"),
                journal=repository,
                acceptance=repository,
                operation_key=f"m6-{index}",
            )
            for index, (task_input, model) in enumerate(zip(envelopes, models, strict=True))
        ]
        assert [outcome.branch for outcome in outcomes] == [
            BranchDecision.MISS,
            BranchDecision.REUSE,
            BranchDecision.MISS,
        ]
        assert all(outcome.passed for outcome in outcomes)
        assert [[stage for _role, stage in model.calls] for model in models] == [
            ["spec", "independent_tests", "implement"],
            ["implement"],
            ["spec", "independent_tests", "implement"],
        ]
        third_attempt = outcomes[2].cost_events[0].attempt_id
        assert third_attempt is not None
        stages = {
            row[0]
            for row in repository._connection.execute(  # noqa: SLF001
                "SELECT stage FROM rrcv2_deterministic_steps WHERE attempt_id=?",
                (third_attempt,),
            )
        }
        assert {"retrieval", "cache_render_rejection", "tier_minus_one"} <= stages
        assert len([event for outcome in outcomes for event in outcome.cost_events]) == 7
        assert all(
            event.effective_provider == "unattested"
            and event.effective_model == "unattested"
            and event.effective_reasoning == "unattested"
            and event.effective_service_tier == "unattested"
            for outcome in outcomes
            for event in outcome.cost_events
        )
