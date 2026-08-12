from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from rrc.contract import (
    ArmMode,
    Config,
    InlineTaskInputV1,
    StructuralShapeV1,
    TargetPreimageV1,
    Task,
    seal_task_input,
)
from rrc.everos import (
    EverOSHybridRetrieval,
    EverOSIsolationV1,
    EverOSOutboxDispatcher,
    EverOSTargetV1,
    build_dispatch,
    parse_dispatch,
    parse_outbox,
    parse_target,
    validate_route_tuple,
)
from rrc.journal import JournalConflict, SQLiteRRCRepository
from rrc.pipeline.solve import solve
from rrc.pipeline.template import templatize
from rrc.retrieval import case_document

from tests.test_rrc_engine import SPEC, Retrieval, ScriptedModel, _code, _verification


def _target(owner: str = "owner") -> EverOSTargetV1:
    return EverOSTargetV1(
        "http://127.0.0.1:8000/api/v2/memory",
        "http://127.0.0.1:8000/health",
        "rrcv2-sync-fixture",
        EverOSIsolationV1("instance-fixture", "d" * 64, owner),
    )


@pytest.mark.parametrize(
    ("lexeme", "expected"),
    [
        ("0", (0, 1)),
        ("1", (1, 1)),
        ("0.6298899054527283", (125_977_981, 200_000_000)),
        ("5e-10", (0, 1)),
        ("1.5e-9", (1, 500_000_000)),
        ("1.499999999e-9", (1, 1_000_000_000)),
        ("1.500000001e-9", (1, 500_000_000)),
    ],
)
def test_everos_score_uses_raw_decimal_half_even_quantization(
    lexeme: str, expected: tuple[int, int]
) -> None:
    import rrc.everos as everos_module

    score = everos_module._remote_score(everos_module._RawJSONNumber(lexeme))
    assert score is not None
    assert (score.numerator, score.denominator) == expected


def test_everos_score_rejects_strings_nonfinite_range_and_oversize_lexemes() -> None:
    import rrc.everos as everos_module

    assert everos_module._remote_score("0.9") is None
    for lexeme in ("-0.1", "1.1", "NaN", "Infinity", "01", "0." + "0" * 127):
        assert everos_module._remote_score(everos_module._RawJSONNumber(lexeme)) is None


def test_target_dispatch_and_outbox_are_exactly_hash_bound() -> None:
    target = _target()
    task = Task(
        "task-1",
        "Lookup one integer and return its value.",
        searchable_public=True,
        family="lookup",
        primary="f",
        shape=StructuralShapeV1(("int",), 1, ()),
        slot_values=(),
    )
    bundle = templatize(SPEC, ())
    document = case_document(task, Config("owner"), bundle)
    dispatch, outbox = build_dispatch(
        target=target,
        task=task,
        external_ref=bundle.external_ref,
        document_sha256=document.document_sha256,
        accept_commit_id="a" * 64,
        owner_scope="owner",
        timestamp_ms=1,
    )
    assert parse_target(target.canonical_bytes()) == target
    assert parse_dispatch(dispatch.canonical_bytes()) == dispatch
    assert parse_outbox(outbox.canonical_bytes()) == outbox
    validate_route_tuple(target, dispatch, outbox)
    changed = json.loads(dispatch.canonical_bytes())
    changed["task_text_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="inconsistent"):
        validate_route_tuple(
            target,
            parse_dispatch(json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()),
            outbox,
        )


def test_warm_everos_acceptance_atomically_creates_one_retryable_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rrc.pipeline.solve as solve_module

    monkeypatch.setattr(solve_module, "_run_verifier", _verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: None)
    task = Task(
        "task-1",
        "Implement f so it returns the supplied integer.",
        public_tests=("def test_public():\n    assert f(2) == 2",),
        searchable_public=True,
        family="identity",
        primary="f",
        shape=StructuralShapeV1(("int",), 1, ()),
        slot_values=(),
    )
    envelope = seal_task_input(
        InlineTaskInputV1(
            task,
            "def f(x: int) -> int:\n    raise NotImplementedError",
            TargetPreimageV1.none(),
        ),
        input_root=(tmp_path / "input").resolve(),
    )
    template = templatize(SPEC, ("def test_independent():\n    assert f(4) == 4",))
    target = _target()
    cfg = Config("owner", memory_backend="everos", everos_target=target.canonical_bytes())
    model = ScriptedModel({"implement": [_code("def f(x: int) -> int:\n    return x")]})
    with SQLiteRRCRepository(tmp_path / "everos.sqlite3") as repository:
        outcome = solve(
            envelope,
            mode=ArmMode.WARM,
            model=model,
            retrieval=Retrieval(repository, template),
            cfg=cfg,
            journal=repository,
            acceptance=repository,
            operation_key="everos-op",
        )
        assert outcome.passed is True
        pending = repository.pending_everos_outbox("owner")
        assert len(pending) == 1
        observation_id, target_sha = pending[0]
        generation, target_raw, dispatch_raw = repository.claim_everos_outbox(
            "owner", observation_id, target_sha
        )
        assert parse_target(target_raw) == target
        assert parse_dispatch(dispatch_raw).observation_id == observation_id
        repository.finish_everos_outbox(
            "owner",
            observation_id,
            target_sha,
            generation=generation,
            success=False,
            error="timeout",
        )
        assert repository.pending_everos_outbox("owner") == pending
        generation, _, _ = repository.claim_everos_outbox("owner", observation_id, target_sha)
        repository.finish_everos_outbox(
            "owner",
            observation_id,
            target_sha,
            generation=generation,
            success=True,
        )
        assert repository.pending_everos_outbox("owner") == ()
        state = repository.everos_outbox_state("owner", observation_id, target_sha)
        assert state is not None
        assert state[:3] == (
            "acknowledged",
            2,
            2,
        )


def test_dispatcher_requires_exact_acknowledgements_and_two_consecutive_zeroes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rrc.everos as everos_module
    import rrc.pipeline.solve as solve_module

    monkeypatch.setattr(solve_module, "_run_verifier", _verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: None)
    monkeypatch.setattr(everos_module.time, "sleep", lambda _seconds: None)
    task = Task(
        "task-1",
        "Implement f so it returns the supplied integer.",
        public_tests=("def test_public():\n    assert f(2) == 2",),
        searchable_public=True,
        family="identity",
        primary="f",
        shape=StructuralShapeV1(("int",), 1, ()),
        slot_values=(),
    )
    envelope = seal_task_input(
        InlineTaskInputV1(
            task,
            "def f(x: int) -> int:\n    raise NotImplementedError",
            TargetPreimageV1.none(),
        ),
        input_root=(tmp_path / "input").resolve(),
    )
    template = templatize(SPEC, ("def test_independent():\n    assert f(4) == 4",))
    target = _target()
    cfg = Config("owner", memory_backend="everos", everos_target=target.canonical_bytes())

    with SQLiteRRCRepository(tmp_path / "dispatcher.sqlite3") as repository:
        outcome = solve(
            envelope,
            mode=ArmMode.WARM,
            model=ScriptedModel({"implement": [_code("def f(x: int) -> int:\n    return x")]}),
            retrieval=Retrieval(repository, template),
            cfg=cfg,
            journal=repository,
            acceptance=repository,
            operation_key="everos-dispatch-good",
        )
        assert outcome.passed is True
        pending = repository.pending_everos_outbox("owner")
        assert len(pending) == 1
        _, _, dispatch_raw = repository.claim_everos_outbox("owner", *pending[0])
        dispatch = parse_dispatch(dispatch_raw)
        repository.finish_everos_outbox(
            "owner", *pending[0], generation=1, success=False, error="release fixture claim"
        )

        health_pending = iter((1, 0, 1, 0, 0))
        requests: list[tuple[str, dict[str, object] | None]] = []

        def exact_request(
            url: str, *, payload: dict[str, object] | None, deadline: float
        ) -> dict[str, object]:
            assert deadline > everos_module.time.monotonic()
            requests.append((url, payload))
            if url.endswith("/add"):
                return {"data": {"status": "accumulated"}}
            if url.endswith("/flush"):
                return {"data": {"status": "no_extraction"}}
            return {"cascade": {"healthy": True, "pending": next(health_pending)}}

        monkeypatch.setattr(EverOSOutboxDispatcher, "_request", staticmethod(exact_request))
        assert EverOSOutboxDispatcher(repository).dispatch_pending("owner") == 1
        assert requests == [
            (target.base_url + "/add", dispatch.add_body),
            (target.base_url + "/flush", dispatch.flush_body),
            *[(target.health_url, None)] * 5,
        ]
        assert repository.everos_outbox_state("owner", *pending[0])[:3] == (  # type: ignore[index]
            "acknowledged",
            2,
            2,
        )

        second = solve(
            envelope,
            mode=ArmMode.WARM,
            model=ScriptedModel({"implement": [_code("def f(x: int) -> int:\n    return x")]}),
            retrieval=Retrieval(repository, template),
            cfg=cfg,
            journal=repository,
            acceptance=repository,
            operation_key="everos-dispatch-invalid",
        )
        assert second.passed is True

        def invalid_add(
            _url: str, *, payload: dict[str, object] | None, deadline: float
        ) -> dict[str, object]:
            assert payload is not None
            assert deadline > everos_module.time.monotonic()
            return {"data": {"status": "unknown"}}

        monkeypatch.setattr(EverOSOutboxDispatcher, "_request", staticmethod(invalid_add))
        assert EverOSOutboxDispatcher(repository).dispatch_pending("owner") == 0
        remaining = repository.pending_everos_outbox("owner")
        assert len(remaining) == 1
        state = repository.everos_outbox_state("owner", *remaining[0])
        assert state is not None
        assert state[0] == "pending"
        assert state[2] == 1
        assert state[3] is not None and "acknowledgement is invalid" in state[3]


def test_expired_draining_row_is_retryable_but_invalidates_remote_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rrc.pipeline.solve as solve_module

    monkeypatch.setattr(solve_module, "_run_verifier", _verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: None)
    task = Task(
        "task-1",
        "Implement f so it returns the supplied integer.",
        public_tests=("def test_public():\n    assert f(2) == 2",),
        searchable_public=True,
        family="identity",
        primary="f",
        shape=StructuralShapeV1(("int",), 1, ()),
        slot_values=(),
    )
    envelope = seal_task_input(
        InlineTaskInputV1(
            task,
            "def f(x: int) -> int:\n    raise NotImplementedError",
            TargetPreimageV1.none(),
        ),
        input_root=(tmp_path / "input").resolve(),
    )
    template = templatize(SPEC, ("def test_independent():\n    assert f(4) == 4",))
    target = _target()
    cfg = Config("owner", memory_backend="everos", everos_target=target.canonical_bytes())
    database = tmp_path / "draining.sqlite3"
    with SQLiteRRCRepository(database) as repository:
        outcome = solve(
            envelope,
            mode=ArmMode.WARM,
            model=ScriptedModel({"implement": [_code("def f(x: int) -> int:\n    return x")]}),
            retrieval=Retrieval(repository, template),
            cfg=cfg,
            journal=repository,
            acceptance=repository,
            operation_key="everos-draining",
        )
        assert outcome.passed is True
        identity = repository.pending_everos_outbox("owner")[0]
        generation, _, _ = repository.claim_everos_outbox("owner", *identity)
        repository.mark_everos_draining("owner", *identity, generation=generation)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE rrcv2_everos_outbox_v1 SET lease_expires_ms=0 WHERE observation_id=?",
            (identity[0],),
        )
        connection.commit()

    with SQLiteRRCRepository(database) as repository:
        assert repository.pending_everos_outbox("owner") == (identity,)
        generation, _, _ = repository.claim_everos_outbox("owner", *identity)
        assert generation == 2
        repository.finish_everos_outbox("owner", *identity, generation=generation, success=True)
        assert repository.everos_route_valid("owner", target.sha256) is False

        retrieval = EverOSHybridRetrieval(
            repository,
            request=lambda *_args, **_kwargs: pytest.fail(
                "ambiguous generation must not issue measured search"
            ),
        )
        candidates = retrieval.retrieve(task, cfg)
        assert [candidate.external_ref for candidate in candidates] == [template.external_ref]
        assert retrieval.backend_valid is False


def test_warm_everos_nonpublic_acceptance_indexes_locally_without_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rrc.pipeline.solve as solve_module

    monkeypatch.setattr(solve_module, "_run_verifier", _verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: None)
    task = Task(
        "task-1",
        "Implement f so it returns the supplied integer.",
        public_tests=("def test_public():\n    assert f(2) == 2",),
        searchable_public=False,
        family="identity",
        primary="f",
        shape=StructuralShapeV1(("int",), 1, ()),
        slot_values=(),
    )
    envelope = seal_task_input(
        InlineTaskInputV1(
            task,
            "def f(x: int) -> int:\n    raise NotImplementedError",
            TargetPreimageV1.none(),
        ),
        input_root=(tmp_path / "input").resolve(),
    )
    template = templatize(SPEC, ("def test_independent():\n    assert f(4) == 4",))
    target = _target()
    cfg = Config("owner", memory_backend="everos", everos_target=target.canonical_bytes())
    model = ScriptedModel({"implement": [_code("def f(x: int) -> int:\n    return x")]})
    with SQLiteRRCRepository(tmp_path / "everos.sqlite3") as repository:
        outcome = solve(
            envelope,
            mode=ArmMode.WARM,
            model=model,
            retrieval=Retrieval(repository, template),
            cfg=cfg,
            journal=repository,
            acceptance=repository,
            operation_key="everos-nonpublic-op",
        )
        assert outcome.passed is True
        assert repository.pending_everos_outbox("owner") == ()
        observations = repository.retrieval_observations("owner")
        assert len(observations) == 1
        assert observations[0].external_ref == template.external_ref


def test_terminal_retry_reopens_and_rejects_mutated_everos_route_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rrc.pipeline.solve as solve_module

    monkeypatch.setattr(solve_module, "_run_verifier", _verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: None)
    task = Task(
        "task-1",
        "Implement f so it returns the supplied integer.",
        public_tests=("def test_public():\n    assert f(2) == 2",),
        searchable_public=True,
        family="identity",
        primary="f",
        shape=StructuralShapeV1(("int",), 1, ()),
        slot_values=(),
    )
    envelope = seal_task_input(
        InlineTaskInputV1(
            task,
            "def f(x: int) -> int:\n    raise NotImplementedError",
            TargetPreimageV1.none(),
        ),
        input_root=(tmp_path / "input").resolve(),
    )
    template = templatize(SPEC, ("def test_independent():\n    assert f(4) == 4",))
    target = _target()
    cfg = Config("owner", memory_backend="everos", everos_target=target.canonical_bytes())
    database = tmp_path / "everos.sqlite3"
    with SQLiteRRCRepository(database) as repository:
        outcome = solve(
            envelope,
            mode=ArmMode.WARM,
            model=ScriptedModel({"implement": [_code("def f(x: int) -> int:\n    return x")]}),
            retrieval=Retrieval(repository, template),
            cfg=cfg,
            journal=repository,
            acceptance=repository,
            operation_key="everos-reconcile-op",
        )
        assert outcome.passed is True

    poisoned = replace(
        target,
        isolation=replace(target.isolation, instance_id="other-instance"),
    ).canonical_bytes()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE rrcv2_everos_targets SET target=?", (poisoned,))
        connection.commit()

    with SQLiteRRCRepository(database) as repository:
        with pytest.raises(JournalConflict, match="EverOS route rows"):
            solve(
                envelope,
                mode=ArmMode.WARM,
                model=ScriptedModel({}),
                retrieval=Retrieval(repository, template),
                cfg=cfg,
                journal=repository,
                acceptance=repository,
                operation_key="everos-reconcile-op",
            )


def test_optional_everos_search_uses_exact_task_wire_and_local_sha_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rrc.pipeline.solve as solve_module

    monkeypatch.setattr(solve_module, "_run_verifier", _verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: None)
    task = Task(
        "task-1",
        "Implement f so it returns the supplied integer.",
        public_tests=("def test_public():\n    assert f(2) == 2",),
        searchable_public=True,
        family="identity",
        primary="f",
        shape=StructuralShapeV1(("int",), 1, ()),
        slot_values=(),
    )
    envelope = seal_task_input(
        InlineTaskInputV1(
            task,
            "def f(x: int) -> int:\n    raise NotImplementedError",
            TargetPreimageV1.none(),
        ),
        input_root=(tmp_path / "input").resolve(),
    )
    template = templatize(SPEC, ("def test_independent():\n    assert f(4) == 4",))
    target = _target()
    cfg = Config("owner", memory_backend="everos", everos_target=target.canonical_bytes())
    observed: list[tuple[str, bytes]] = []

    def request(url: str, body: bytes, *, deadline: float) -> bytes:
        assert deadline > 0
        observed.append((url, body))
        return json.dumps(
            {
                "data": {
                    "episodes": [
                        {"external_ref": template.external_ref, "score": 0.9},
                        {"external_ref": template.external_ref, "score": 0.8},
                    ]
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    with SQLiteRRCRepository(tmp_path / "everos-search.sqlite3") as repository:
        outcome = solve(
            envelope,
            mode=ArmMode.WARM,
            model=ScriptedModel({"implement": [_code("def f(x: int) -> int:\n    return x")]}),
            retrieval=Retrieval(repository, template),
            cfg=cfg,
            journal=repository,
            acceptance=repository,
            operation_key="everos-search-seed",
        )
        assert outcome.passed is True
        pending = repository.pending_everos_outbox("owner")
        assert len(pending) == 1
        generation, _, _ = repository.claim_everos_outbox("owner", *pending[0])
        repository.finish_everos_outbox("owner", *pending[0], generation=generation, success=True)

        retrieval = EverOSHybridRetrieval(repository, request=request)
        candidates = retrieval.retrieve(task, cfg)
        assert [candidate.external_ref for candidate in candidates] == [template.external_ref]
        assert retrieval.classify(task, template.external_ref) == "exact"
        assert retrieval.get_template(template.external_ref) == template
        assert retrieval.backend_valid is True
        assert observed == [
            (
                target.base_url + "/search",
                (
                    '{"app_id":"default","method":"hybrid","min_score":0.35,'
                    '"project_id":"default","query":"Implement f so it returns the supplied '
                    'integer.","top_k":3,"user_id":"rrc"}'
                ).encode(),
            )
        ]

        def unavailable(_url: str, _body: bytes, *, deadline: float) -> bytes:
            assert deadline > 0
            raise TimeoutError("remote unavailable")

        fallback = EverOSHybridRetrieval(repository, request=unavailable)
        fallback_candidates = fallback.retrieve(task, cfg)
        assert [candidate.external_ref for candidate in fallback_candidates] == [
            template.external_ref
        ]
        assert fallback.classify(task, template.external_ref) == "exact"
        assert fallback.backend_valid is False
