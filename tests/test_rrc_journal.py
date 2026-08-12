from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from rrc.contract import canonical_json_bytes
from rrc.journal import (
    CallRecordV1,
    JournalConflict,
    JournalStateError,
    ParticipantStatementV1,
    SealedAttemptInputsV1,
    SQLiteRRCRepository,
    UsageRecordV1,
    benchmark_operation_key,
    contextmesh_operation_key,
)


def _inputs(**changes: object) -> SealedAttemptInputsV1:
    values: dict[str, object] = {
        "task_envelope_sha256": "1" * 64,
        "mode": "warm",
        "flow_kind": "spec_pipeline",
        "transport": "sync",
        "config_sha256": "2" * 64,
        "model_policy_sha256": "3" * 64,
        "verifier_policy_sha256": "4" * 64,
    }
    values.update(changes)
    return SealedAttemptInputsV1(**values)  # type: ignore[arg-type]


def _record(call_id: str = "a" * 64) -> CallRecordV1:
    return CallRecordV1(
        call_id=call_id,
        stage="spec",
        stage_ordinal=1,
        role="strong",
        model="gpt-5.5",
        settings_sha256="5" * 64,
        prompt_sha256="6" * 64,
        transcript_baseline_sha256="7" * 64,
    )


def test_operation_keys_have_exact_canonical_preimages_and_lengths() -> None:
    benchmark = benchmark_operation_key(
        run_id="run/one", replicate_id="r01", arm="warm", task_id="task:a"
    )
    contextmesh = contextmesh_operation_key(
        repository_id="a" * 64,
        route_id="rrcv2-coding-v1",
        round_id="round/1",
        tool_use_id="tool:1",
        task_id="task.a",
        assignment_sha256="b" * 64,
    )
    assert benchmark == "bench-af3334b36d4ca8ac57c3db4dac2d39b1bd4de256b83673372614686dfee36bb9"
    assert len(benchmark) == 70
    assert contextmesh == "cm-1e6aecdb345e285ccaf9f877132900d199a727eaa6ab3d91a9b55cbacfcafe70"
    assert len(contextmesh) == 67


def test_repository_is_mode_0600_and_begin_is_idempotent_but_binding_drift_conflicts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "journal.sqlite"
    repository = SQLiteRRCRepository(database)
    assert database.stat().st_mode & 0o777 == 0o600
    assert len(repository.database_uuid) == 64
    assert repository.authority_id.endswith(repository.database_uuid)
    assert repository.atomic_warm is True

    first = repository.begin_attempt("scope-1", "operation-1", _inputs())
    repeated = repository.begin_attempt("scope-1", "operation-1", _inputs())
    assert repeated == first
    assert first.state == "preparing"
    assert first.cursor == 0
    assert repository.list_recoverable("scope-1") == (first,)

    with pytest.raises(JournalConflict, match="bound"):
        repository.begin_attempt("scope-1", "operation-1", _inputs(config_sha256="f" * 64))
    repository.close()


def test_concurrent_identical_starts_converge_on_one_attempt(tmp_path: Path) -> None:
    database = tmp_path / "journal.sqlite"

    def begin() -> str:
        with SQLiteRRCRepository(database) as repository:
            return repository.begin_attempt("scope-1", "same-operation", _inputs()).attempt_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        attempt_ids = tuple(executor.map(lambda _: begin(), range(16)))
    assert len(set(attempt_ids)) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM rrcv2_attempts").fetchone() == (1,)


def test_call_write_ahead_state_machine_is_exact_and_restart_recoverable(tmp_path: Path) -> None:
    database = tmp_path / "journal.sqlite"
    repository = SQLiteRRCRepository(database)
    handle = repository.begin_attempt("scope-1", "operation-1", _inputs())
    record = _record()

    deterministic = repository.record_deterministic(
        handle,
        "sealed_inputs",
        "0" * 64,
        "1" * 64,
        expected_state="preparing",
        expected_generation=0,
        expected_cursor=0,
    )
    assert deterministic.cursor == 1

    prepared = repository.prepare_call(
        deterministic,
        record,
        expected_state="preparing",
        expected_generation=0,
        expected_cursor=1,
    )
    assert prepared.cursor == 2
    with pytest.raises(JournalStateError, match="cursor"):
        repository.prepare_call(
            handle,
            _record("b" * 64),
            expected_state="preparing",
            expected_generation=0,
            expected_cursor=0,
        )

    started = repository.mark_call_started(
        prepared,
        record.call_id,
        canonical_json_bytes({"pid": 123, "v": 1}),
        expected_state="preparing",
        expected_generation=0,
        expected_cursor=2,
    )
    repository.close()

    repository = SQLiteRRCRepository(database)
    recovered = repository.load_attempt(handle.attempt_id)
    assert recovered == started
    assert repository.call_state(handle.attempt_id, record.call_id) == "call_started"

    observed = repository.observe_call(
        recovered,
        record.call_id,
        canonical_json_bytes({"bytes": 10, "path": "calls/a/transcript.jsonl", "sha256": "8" * 64}),
        UsageRecordV1(100, 5, 105),
        expected_state="preparing",
        expected_generation=0,
        expected_cursor=3,
    )
    committed = repository.commit_call(
        observed,
        record.call_id,
        outcome=canonical_json_bytes({"kind": "spec", "sha256": "9" * 64, "v": 1}),
        cost_event=canonical_json_bytes(
            {"cached_input_tokens": 0, "input_tokens": 100, "output_tokens": 5, "v": 1}
        ),
        expected_state="preparing",
        expected_generation=0,
        expected_cursor=4,
    )
    assert committed.cursor == 5
    assert repository.call_state(handle.attempt_id, record.call_id) == "call_committed"

    final = repository.commit_prepared(
        committed,
        canonical_json_bytes({"attempt_id": handle.attempt_id, "branch": "miss", "v": 1}),
        expected_state="preparing",
        expected_generation=0,
        expected_cursor=5,
    )
    assert final.state == "prepared"
    assert final.cursor == 6
    prepared_raw = repository.load_prepared(handle.attempt_id)
    assert prepared_raw == canonical_json_bytes(
        {"attempt_id": handle.attempt_id, "branch": "miss", "v": 1}
    )
    repository.close()


def test_exact_duplicate_call_transitions_are_noops_but_payload_reuse_conflicts(
    tmp_path: Path,
) -> None:
    repository = SQLiteRRCRepository(tmp_path / "journal.sqlite")
    handle = repository.begin_attempt("scope-1", "operation-1", _inputs())
    record = _record()
    next_handle = repository.prepare_call(
        handle,
        record,
        expected_state="preparing",
        expected_generation=0,
        expected_cursor=0,
    )
    assert (
        repository.prepare_call(
            handle,
            record,
            expected_state="preparing",
            expected_generation=0,
            expected_cursor=0,
        )
        == next_handle
    )
    with pytest.raises(JournalConflict, match="event identity"):
        repository.prepare_call(
            handle,
            CallRecordV1(
                **{
                    **record.__dict__,
                    "stage": "fallback_spec",
                }
            ),
            expected_state="preparing",
            expected_generation=0,
            expected_cursor=0,
        )


def test_run_immediate_rolls_back_callback_errors_and_rejects_nesting(tmp_path: Path) -> None:
    repository = SQLiteRRCRepository(tmp_path / "journal.sqlite")
    bundle = canonical_json_bytes({"spec_template": {}, "v": 1})
    external_ref = hashlib.sha256(bundle).hexdigest()

    def fail(uow: object) -> None:
        assert getattr(uow, "transaction_identity")
        getattr(uow, "put_bundle")(external_ref, bundle)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        repository.run_immediate(fail)
    assert repository.get_bundle(external_ref) is None

    with pytest.raises(JournalStateError, match="nested"):
        repository.run_immediate(lambda _uow: repository.run_immediate(lambda _inner: None))


def test_open_rejects_symlink_or_wrong_mode_database(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"")
    os.chmod(target, 0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        SQLiteRRCRepository(target)

    link = tmp_path / "link.sqlite"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="regular"):
        SQLiteRRCRepository(link)


def test_sealed_attempt_inputs_are_exact_canonical_bytes() -> None:
    raw = _inputs().canonical_bytes()
    assert json.loads(raw) == {
        "config_sha256": "2" * 64,
        "flow_kind": "spec_pipeline",
        "mode": "warm",
        "model_policy_sha256": "3" * 64,
        "task_envelope_sha256": "1" * 64,
        "transport": "sync",
        "v": 1,
        "verifier_policy_sha256": "4" * 64,
    }


def _participant_statements() -> dict[str, ParticipantStatementV1]:
    create = (
        "CREATE TABLE IF NOT EXISTS rrcv2p_attempts_items "
        "(item_id TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
    )
    insert = "INSERT INTO rrcv2p_attempts_items(item_id,value) VALUES(?,?)"
    select = "SELECT value FROM rrcv2p_attempts_items WHERE item_id=?"
    return {
        "create_items": ParticipantStatementV1(
            create, hashlib.sha256(create.encode()).hexdigest(), "create_table"
        ),
        "insert_item": ParticipantStatementV1(
            insert, hashlib.sha256(insert.encode()).hexdigest(), "insert"
        ),
        "select_item": ParticipantStatementV1(
            select, hashlib.sha256(select.encode()).hexdigest(), "select"
        ),
    }


def _statement_map_sha(rows: dict[str, ParticipantStatementV1]) -> str:
    raw = canonical_json_bytes(
        {
            name: {"kind": row.kind, "sha256": row.sha256, "sql": row.sql}
            for name, row in rows.items()
        }
    )
    return hashlib.sha256(raw).hexdigest()


def test_participant_migration_and_runtime_statements_share_exact_transaction(
    tmp_path: Path,
) -> None:
    repository = SQLiteRRCRepository(tmp_path / "journal.sqlite")
    statements = _participant_statements()
    definition_sha = _statement_map_sha(statements)
    repository.apply_participant_migration("attempts", 1, definition_sha, statements)
    repository.apply_participant_migration("attempts", 1, definition_sha, statements)

    identities: list[tuple[int, str]] = []

    def write(uow: object) -> None:
        participant = getattr(uow, "participant_cursor")("attempts")
        identities.append(
            (getattr(uow, "connection_identity"), getattr(participant, "transaction_identity"))
        )
        assert participant.execute("insert_item", ("one", "value")) == 1
        assert participant.fetch_one("select_item", ("one",)) == ("value",)

    repository.run_immediate(write)
    assert identities[0][1]

    captured: object | None = None

    def capture(uow: object) -> None:
        nonlocal captured
        captured = getattr(uow, "participant_cursor")("attempts")

    repository.run_immediate(capture)
    assert captured is not None
    with pytest.raises(JournalStateError, match="active"):
        getattr(captured, "fetch_one")("select_item", ("one",))


@pytest.mark.parametrize(
    ("statement_id", "sql", "kind"),
    [
        ("outside", "SELECT value FROM rrcv2_attempts", "select"),
        ("cte", "WITH x AS (SELECT 1) SELECT * FROM x", "select"),
        ("pragma", "PRAGMA user_version", "select"),
        (
            "trigger",
            "CREATE TRIGGER rrcv2p_attempts_t AFTER INSERT ON rrcv2p_attempts_items BEGIN SELECT 1; END",
            "create_table",
        ),
        ("multi", "SELECT 1; SELECT 2", "select"),
    ],
)
def test_participant_registration_rejects_unsafe_or_cross_namespace_sql(
    tmp_path: Path, statement_id: str, sql: str, kind: str
) -> None:
    repository = SQLiteRRCRepository(tmp_path / f"{statement_id}.sqlite")
    with pytest.raises((ValueError, JournalConflict)):
        statement = ParticipantStatementV1(
            sql,
            hashlib.sha256(sql.encode()).hexdigest(),
            kind,  # type: ignore[arg-type]
        )
        rows = {statement_id: statement}
        repository.apply_participant_migration("attempts", 1, _statement_map_sha(rows), rows)


def test_participant_registration_is_immutable_and_callback_rolls_back(tmp_path: Path) -> None:
    repository = SQLiteRRCRepository(tmp_path / "journal.sqlite")
    statements = _participant_statements()
    repository.apply_participant_migration(
        "attempts", 1, _statement_map_sha(statements), statements
    )
    mutated_sql = "SELECT item_id FROM rrcv2p_attempts_items WHERE item_id=?"
    mutated = {
        **statements,
        "select_item": ParticipantStatementV1(
            mutated_sql, hashlib.sha256(mutated_sql.encode()).hexdigest(), "select"
        ),
    }
    with pytest.raises(JournalConflict, match="immutable"):
        repository.apply_participant_migration("attempts", 1, _statement_map_sha(mutated), mutated)

    def fail(uow: object) -> None:
        participant = getattr(uow, "participant_cursor")("attempts")
        participant.execute("insert_item", ("rollback", "value"))
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        repository.run_immediate(fail)
    assert (
        repository.run_immediate(
            lambda uow: uow.participant_cursor("attempts").fetch_one("select_item", ("rollback",))
        )
        is None
    )
