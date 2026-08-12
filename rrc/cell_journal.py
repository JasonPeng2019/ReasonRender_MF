"""Durable cell-level journal for ContextMesh product dispatch.

The attempt journal owns model work performed by one RRC attempt.  A native
ContextMesh root session is different: it owns the root provider call and binds
one already-created attempt to the exact spawn tool event before any
attempt-scoped provider call may launch.  This adapter keeps that authority in
the same SQLite database while exposing only immutable, mode-0600 file
references to the fail-closed product-permit validator.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

from rrc.contextmesh import parse_receipt_record, parse_wait_envelope
from rrc.contract import CostEventV1, canonical_json_bytes
from rrc.dispatch_permit import (
    AuthorityRef,
    DispatchPermitV1,
    ProductCellAttemptBindingV1,
    ProductCellDispatchRequestV1,
    ProductCellJournalCursorV1,
    ProductRootedAttemptAuthorityV2,
    ProductRootedAttemptJournalViewV1,
    canonical_json,
    cell_attempt_binding_value,
    dispatch_permit_value,
    product_call_id,
    rooted_attempt_authority_value,
)
from rrc.journal import (
    JournalConflict,
    JournalStateError,
    SQLiteRRCRepository,
    parse_accepted_commit,
    parse_rejected_commit,
)

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_TOOL_ID = re.compile(r"[!-~]{1,256}\Z")
_MAX_AUTHORITY = 4 * 1024 * 1024


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex64(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} does not use the frozen identifier grammar")
    return value


def _tool_id(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or _TOOL_ID.fullmatch(value) is None
        or any(character in value for character in ("/", "\\", '"', "'"))
    ):
        raise ValueError(f"{name} does not use the frozen tool identifier grammar")
    return value


def _canonical_line(raw: bytes, *, name: str) -> bytes:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_AUTHORITY:
        raise ValueError(f"{name} is missing or exceeds its byte cap")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not strict JSON") from exc
    if canonical_json(value) != raw:
        raise ValueError(f"{name} is not canonical JSON")
    return raw


def _request_bytes(request: ProductCellDispatchRequestV1) -> bytes:
    if not isinstance(request, ProductCellDispatchRequestV1) or request.v != 1:
        raise TypeError("root request must be ProductCellDispatchRequestV1")
    if request.call_id != product_call_id(request):
        raise ValueError("root request call ID differs from its dispatch preimage")
    return canonical_json(asdict(request))


def _canonical_payload(raw: bytes, *, name: str) -> bytes:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_AUTHORITY:
        raise ValueError(f"{name} is missing or exceeds its byte cap")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not strict JSON") from exc
    if canonical_json_bytes(value) != raw:
        raise ValueError(f"{name} is not canonical compact JSON")
    return raw


def _atomic_authority(path: Path, raw: bytes) -> AuthorityRef:
    raw = _canonical_line(raw, name="cell authority")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    else:
        if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
            raise JournalConflict("existing cell authority is not a mode-0600 regular file")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        observed = os.fstat(descriptor)
        existing = os.read(descriptor, _MAX_AUTHORITY + 1)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) != 0o600
        or existing != raw
    ):
        raise JournalConflict("existing cell authority conflicts with the durable bytes")
    return AuthorityRef(path=path, sha256=_sha(raw), bytes=len(raw))


def _authority_ref(path: Path, raw: bytes) -> AuthorityRef:
    """Describe bytes before publication so DB validation cannot poison a fixed path."""

    raw = _canonical_line(raw, name="cell authority")
    return AuthorityRef(path=path, sha256=_sha(raw), bytes=len(raw))


@dataclass(frozen=True)
class CellHandleV1:
    cell_id: str
    root_call_id: str
    state: str
    generation: int
    request_sha256: str
    v: int = 1

    def __post_init__(self) -> None:
        _identifier(self.cell_id, name="cell_id")
        _hex64(self.root_call_id, name="root_call_id")
        _hex64(self.request_sha256, name="request_sha256")
        if self.state not in {
            "cell_created",
            "root_prepared",
            "root_started",
            "root_observed",
            "root_committed",
            "combined_committed",
        }:
            raise ValueError("unknown cell state")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise TypeError("cell generation must be an integer")
        if self.generation < 0 or self.v != 1:
            raise ValueError("cell generation/version is invalid")


@dataclass(frozen=True)
class RootStartedHandleV1:
    cell: CellHandleV1
    root_permit_ref: AuthorityRef
    root_started_ref: AuthorityRef
    root_launch_identity_ref: AuthorityRef
    v: int = 1


@dataclass(frozen=True)
class BoundAttemptV1:
    cell: CellHandleV1
    binding_ref: AuthorityRef
    journal_view: ProductRootedAttemptJournalViewV1
    v: int = 1


@dataclass(frozen=True)
class RootToolEventV1:
    """One descriptive, zero-usage ContextMesh root tool event."""

    kind: Literal["wait", "apply"]
    tool_use_id: str
    input_sha256: str
    output_sha256: str
    output: bytes
    v: int = 1

    def __post_init__(self) -> None:
        if self.kind not in {"wait", "apply"}:
            raise ValueError("root tool event kind is invalid")
        _tool_id(self.tool_use_id, name="root tool_use_id")
        _hex64(self.input_sha256, name="root tool input_sha256")
        _hex64(self.output_sha256, name="root tool output_sha256")
        _canonical_payload(self.output, name="root tool output")
        if _sha(self.output) != self.output_sha256:
            raise ValueError("root tool output hash differs")
        if self.v != 1:
            raise ValueError("root tool event version is invalid")

    def canonical_bytes(self) -> bytes:
        return canonical_json(
            {
                "input_sha256": self.input_sha256,
                "kind": self.kind,
                "output_sha256": self.output_sha256,
                "tool_use_id": self.tool_use_id,
                "v": 1,
            }
        )


@dataclass(frozen=True)
class CombinedSessionAttemptV1:
    attempt_id: str
    terminal_kind: Literal["accepted", "rejected"]
    terminal_outcome_sha256: str
    receipt: str | None
    apply_event_sha256: str | None
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.attempt_id, name="combined attempt_id")
        if self.terminal_kind not in {"accepted", "rejected"}:
            raise ValueError("combined terminal_kind is invalid")
        _hex64(self.terminal_outcome_sha256, name="combined terminal_outcome_sha256")
        if self.terminal_kind == "accepted":
            _hex64(self.receipt, name="combined receipt")
            _hex64(self.apply_event_sha256, name="combined apply_event_sha256")
        elif self.receipt is not None or self.apply_event_sha256 is not None:
            raise ValueError("rejected combined attempt cannot name receipt/apply")
        if self.v != 1:
            raise ValueError("combined attempt version is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "apply_event_sha256": self.apply_event_sha256,
            "attempt_id": self.attempt_id,
            "receipt": self.receipt,
            "terminal_kind": self.terminal_kind,
            "terminal_outcome_sha256": self.terminal_outcome_sha256,
        }


@dataclass(frozen=True)
class CombinedSessionRecordV1:
    cell_id: str
    round_id: str
    root_session_id: str
    root_cost_event_id: str
    root_transcript_sha256: str
    root_final_sha256: str
    wait_id: str
    wait_envelope_sha256: str
    tool_event_sha256s: tuple[str, ...]
    attempts: tuple[CombinedSessionAttemptV1, ...]
    all_cost_event_ids: tuple[str, ...]
    v: int = 1

    def __post_init__(self) -> None:
        _identifier(self.cell_id, name="combined cell_id")
        _identifier(self.round_id, name="combined round_id")
        _tool_id(self.root_session_id, name="combined root_session_id")
        for value, name in (
            (self.root_cost_event_id, "root_cost_event_id"),
            (self.root_transcript_sha256, "root_transcript_sha256"),
            (self.root_final_sha256, "root_final_sha256"),
            (self.wait_id, "wait_id"),
            (self.wait_envelope_sha256, "wait_envelope_sha256"),
        ):
            _hex64(value, name=name)
        if not self.tool_event_sha256s or len(set(self.tool_event_sha256s)) != len(
            self.tool_event_sha256s
        ):
            raise ValueError("combined tool event hashes must be nonempty and unique")
        for value in self.tool_event_sha256s:
            _hex64(value, name="tool_event_sha256")
        if (
            not self.attempts
            or tuple(sorted(self.attempts, key=lambda row: row.attempt_id)) != self.attempts
        ):
            raise ValueError("combined attempts must be nonempty and sorted")
        if len({row.attempt_id for row in self.attempts}) != len(self.attempts):
            raise ValueError("combined attempts must be unique")
        if (
            not self.all_cost_event_ids
            or tuple(sorted(set(self.all_cost_event_ids))) != self.all_cost_event_ids
        ):
            raise ValueError("combined cost IDs must be a sorted unique nonempty tuple")
        for value in self.all_cost_event_ids:
            _identifier(value, name="combined cost_event_id")
        if self.root_cost_event_id not in self.all_cost_event_ids or self.v != 1:
            raise ValueError("combined session root cost/version is invalid")

    def canonical_bytes(self) -> bytes:
        return canonical_json(
            {
                "all_cost_event_ids": list(self.all_cost_event_ids),
                "attempts": [row.as_json() for row in self.attempts],
                "cell_id": self.cell_id,
                "root_cost_event_id": self.root_cost_event_id,
                "root_final_sha256": self.root_final_sha256,
                "root_session_id": self.root_session_id,
                "root_transcript_sha256": self.root_transcript_sha256,
                "round_id": self.round_id,
                "tool_event_sha256s": list(self.tool_event_sha256s),
                "v": 1,
                "wait_envelope_sha256": self.wait_envelope_sha256,
                "wait_id": self.wait_id,
            }
        )


class SQLiteCellJournal:
    """Real ``CellJournalPort`` over the canonical repository connection."""

    def __init__(
        self, repository: SQLiteRRCRepository, *, authority_root: Path | None = None
    ) -> None:
        if not isinstance(repository, SQLiteRRCRepository):
            raise TypeError("SQLiteCellJournal requires SQLiteRRCRepository")
        self.repository = repository
        self._connection = cast(Any, repository)._connection
        database_path = cast(Path, getattr(repository, "_path"))
        self.authority_root = (
            authority_root.absolute()
            if authority_root is not None
            else database_path.with_suffix(database_path.suffix + ".cell-authority").absolute()
        )
        self.authority_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.authority_root, 0o700)
        self._install_schema()

    def _install_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS rrcv2p_cells (
                cell_id TEXT PRIMARY KEY,
                root_call_id TEXT NOT NULL UNIQUE,
                request BLOB NOT NULL,
                request_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                generation INTEGER NOT NULL,
                root_permit BLOB,
                root_started BLOB,
                root_launch_identity BLOB,
                root_transcript_ref BLOB,
                root_usage BLOB,
                root_cost_event BLOB,
                combined_session BLOB
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS rrcv2p_cell_attempts (
                cell_id TEXT NOT NULL REFERENCES rrcv2p_cells(cell_id),
                attempt_id TEXT NOT NULL REFERENCES rrcv2_attempts(attempt_id),
                tool_use_id TEXT NOT NULL,
                task_envelope_sha256 TEXT NOT NULL,
                binding BLOB NOT NULL,
                binding_sha256 TEXT NOT NULL,
                spawn_event BLOB NOT NULL,
                rooted_authority BLOB NOT NULL,
                agent_id TEXT,
                completed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(cell_id,attempt_id),
                UNIQUE(cell_id,tool_use_id)
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS rrcv2p_cell_tool_events (
                cell_id TEXT NOT NULL REFERENCES rrcv2p_cells(cell_id),
                sequence INTEGER NOT NULL,
                kind TEXT NOT NULL,
                tool_use_id TEXT NOT NULL,
                event BLOB NOT NULL,
                event_sha256 TEXT NOT NULL,
                output BLOB NOT NULL,
                PRIMARY KEY(cell_id,sequence),
                UNIQUE(cell_id,tool_use_id)
            ) STRICT
            """,
        )

        def install(_uow: object) -> None:
            for statement in statements:
                self._connection.execute(statement)

        self.repository.run_immediate(install)

    def _handle(self, cell_id: str) -> CellHandleV1:
        row = self._connection.execute(
            """
            SELECT root_call_id,state,generation,request_sha256
            FROM rrcv2p_cells WHERE cell_id=?
            """,
            (cell_id,),
        ).fetchone()
        if row is None:
            raise JournalStateError("product cell is missing")
        return CellHandleV1(
            cell_id, cast(str, row[0]), cast(str, row[1]), row[2], cast(str, row[3])
        )

    def load_cell(self, cell_id: str) -> CellHandleV1:
        """Reopen one exact cell cursor for restart-safe orchestration."""

        _identifier(cell_id, name="cell_id")
        return self._handle(cell_id)

    def load_root_started(self, cell_id: str) -> RootStartedHandleV1:
        """Reopen a started root and rematerialize its immutable authorities."""

        current = self.load_cell(cell_id)
        if current.state != "root_started":
            raise JournalStateError("product root is not in the started state")
        row = self._connection.execute(
            """
            SELECT root_permit,root_started,root_launch_identity
            FROM rrcv2p_cells WHERE cell_id=?
            """,
            (cell_id,),
        ).fetchone()
        if row is None or any(item is None for item in row):
            raise JournalConflict("started root is missing its immutable authorities")
        base = self.authority_root / cell_id
        return RootStartedHandleV1(
            current,
            _atomic_authority(base / "root-permit.json", cast(bytes, row[0])),
            _atomic_authority(base / "root-started.json", cast(bytes, row[1])),
            _atomic_authority(base / "root-launch.json", cast(bytes, row[2])),
        )

    @staticmethod
    def _exact(current: CellHandleV1, expected: CellHandleV1) -> None:
        if current != expected:
            raise JournalStateError("product cell cursor differs")

    def begin_cell(self, request: ProductCellDispatchRequestV1) -> CellHandleV1:
        raw = _request_bytes(request)
        digest = _sha(raw)

        def begin(_uow: object) -> None:
            conflict = self._connection.execute(
                "SELECT attempt_id FROM rrcv2_calls WHERE call_id=?", (request.call_id,)
            ).fetchone()
            if conflict is not None:
                raise JournalConflict("root call ID collides with an attempt call")
            existing = self._connection.execute(
                "SELECT request,request_sha256 FROM rrcv2p_cells WHERE cell_id=?",
                (request.cell_id,),
            ).fetchone()
            if existing is not None:
                if existing != (raw, digest):
                    raise JournalConflict("product cell already exists with different bytes")
                return
            self._connection.execute(
                """
                INSERT INTO rrcv2p_cells(
                    cell_id,root_call_id,request,request_sha256,state,generation
                ) VALUES(?,?,?,?,?,0)
                """,
                (request.cell_id, request.call_id, raw, digest, "cell_created"),
            )

        self.repository.run_immediate(begin)
        return self._handle(request.cell_id)

    def prepare_root_call(self, cell: CellHandleV1) -> ProductCellJournalCursorV1:
        def prepare(_uow: object) -> None:
            current = self._handle(cell.cell_id)
            self._exact(current, cell)
            if current.state == "root_prepared":
                return
            if current.state != "cell_created":
                raise JournalStateError("root call cannot be prepared from this cell state")
            changed = self._connection.execute(
                """
                UPDATE rrcv2p_cells SET state='root_prepared'
                WHERE cell_id=? AND state='cell_created' AND generation=?
                """,
                (cell.cell_id, cell.generation),
            )
            if changed.rowcount != 1:
                raise JournalStateError("root prepare compare-and-swap failed")

        self.repository.run_immediate(prepare)
        current = self._handle(cell.cell_id)
        return ProductCellJournalCursorV1(
            None, current.cell_id, current.root_call_id, 1, current.generation, "absent"
        )

    def mark_root_started(
        self,
        cell: CellHandleV1,
        *,
        permit: DispatchPermitV1,
        session_id: str,
        transcript_baseline_sha256: str,
    ) -> RootStartedHandleV1:
        _tool_id(session_id, name="root session_id")
        _hex64(transcript_baseline_sha256, name="root transcript baseline")
        permit_raw = canonical_json(dispatch_permit_value(permit))
        launch_raw = canonical_json(
            {
                "v": 1,
                "kind": "rrcv2_product_root_launch_identity",
                "cell_id": cell.cell_id,
                "root_call_id": cell.root_call_id,
                "session_id": session_id,
                "transcript_baseline_sha256": transcript_baseline_sha256,
            }
        )
        permit_ref = _authority_ref(
            self.authority_root / cell.cell_id / "root-permit.json", permit_raw
        )
        launch_ref = _authority_ref(
            self.authority_root / cell.cell_id / "root-launch.json", launch_raw
        )
        next_generation = cell.generation + 1
        started_raw = canonical_json(
            {
                "v": 1,
                "kind": "rrcv2_product_root_call_started",
                "cell_id": cell.cell_id,
                "root_call_id": cell.root_call_id,
                "state": "started",
                "generation": next_generation,
                "root_permit_sha256": permit_ref.sha256,
                "root_launch_identity_sha256": launch_ref.sha256,
            }
        )
        started_ref = _authority_ref(
            self.authority_root / cell.cell_id / "root-started.json", started_raw
        )
        if (
            permit.kind != "product"
            or permit.call_id != cell.root_call_id
            or permit.surface_id != "root_strong_medium_native"
        ):
            raise JournalConflict("root permit differs from the prepared root call")

        def start(_uow: object) -> None:
            current = self._handle(cell.cell_id)
            self._exact(current, cell)
            if current.state != "root_prepared":
                raise JournalStateError("root call is not prepared")
            changed = self._connection.execute(
                """
                UPDATE rrcv2p_cells
                SET state='root_started',generation=?,root_permit=?,root_started=?,
                    root_launch_identity=?
                WHERE cell_id=? AND state='root_prepared' AND generation=?
                """,
                (
                    next_generation,
                    permit_raw,
                    started_raw,
                    launch_raw,
                    cell.cell_id,
                    cell.generation,
                ),
            )
            if changed.rowcount != 1:
                raise JournalStateError("root start compare-and-swap failed")

        self.repository.run_immediate(start)
        permit_ref = _atomic_authority(permit_ref.path, permit_raw)
        launch_ref = _atomic_authority(launch_ref.path, launch_raw)
        started_ref = _atomic_authority(started_ref.path, started_raw)
        return RootStartedHandleV1(self._handle(cell.cell_id), permit_ref, started_ref, launch_ref)

    def bind_attempt(
        self,
        root: RootStartedHandleV1,
        *,
        tool_use_id: str,
        attempt_id: str,
        task_envelope_sha256: str,
        expected_generation: int,
    ) -> BoundAttemptV1:
        _tool_id(tool_use_id, name="tool_use_id")
        _hex64(attempt_id, name="attempt_id")
        _hex64(task_envelope_sha256, name="task_envelope_sha256")
        if expected_generation != root.cell.generation:
            raise JournalStateError("root binding generation differs")
        binding_generation = root.cell.generation - 1
        binding = ProductCellAttemptBindingV1(
            cell_id=root.cell.cell_id,
            root_call_id=root.cell.root_call_id,
            tool_use_id=tool_use_id,
            attempt_id=attempt_id,
            task_envelope_sha256=task_envelope_sha256,
            agent_id=None,
            generation=binding_generation,
        )
        binding_raw = canonical_json(cell_attempt_binding_value(binding))
        binding_ref = _authority_ref(
            self.authority_root / root.cell.cell_id / attempt_id / "binding.json", binding_raw
        )
        spawn_raw = canonical_json(
            {
                "v": 1,
                "kind": "rrcv2_contextmesh_spawn_observed",
                "cell_id": root.cell.cell_id,
                "root_call_id": root.cell.root_call_id,
                "root_call_generation": root.cell.generation,
                "tool_use_id": tool_use_id,
                "attempt_id": attempt_id,
                "task_envelope_sha256": task_envelope_sha256,
                "binding_generation": binding_generation,
                "state": "spawn_observed",
            }
        )
        spawn_ref = _authority_ref(
            self.authority_root / root.cell.cell_id / attempt_id / "spawn.json", spawn_raw
        )
        authority = ProductRootedAttemptAuthorityV2(
            cell_id=root.cell.cell_id,
            root_call_id=root.cell.root_call_id,
            root_call_state="started",
            root_call_generation=root.cell.generation,
            root_permit_sha256=root.root_permit_ref.sha256,
            root_started_sha256=root.root_started_ref.sha256,
            root_launch_identity_sha256=root.root_launch_identity_ref.sha256,
            tool_use_id=tool_use_id,
            tool_event_sha256=spawn_ref.sha256,
            tool_event_state="spawn_observed",
            attempt_id=attempt_id,
            task_envelope_sha256=task_envelope_sha256,
            binding_sha256=binding_ref.sha256,
            binding_generation=binding_generation,
            agent_id=None,
        )
        authority_raw = canonical_json(rooted_attempt_authority_value(authority))
        authority_ref = _authority_ref(
            self.authority_root / root.cell.cell_id / attempt_id / "authority.json",
            authority_raw,
        )

        def bind(_uow: object) -> None:
            current = self._handle(root.cell.cell_id)
            self._exact(current, root.cell)
            if current.state != "root_started":
                raise JournalStateError("attempt binding requires a started root")
            attempt = self._connection.execute(
                "SELECT sealed_inputs FROM rrcv2_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise JournalConflict("bound attempt does not exist")
            try:
                sealed_inputs = json.loads(cast(bytes, attempt[0]))
                root_request = json.loads(
                    cast(
                        bytes,
                        self._connection.execute(
                            "SELECT request FROM rrcv2p_cells WHERE cell_id=?",
                            (root.cell.cell_id,),
                        ).fetchone()[0],
                    )
                )
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise JournalConflict("cell or attempt input authority is malformed") from exc
            if (
                not isinstance(sealed_inputs, dict)
                or not isinstance(root_request, dict)
                or sealed_inputs.get("task_envelope_sha256") != task_envelope_sha256
                or root_request.get("task_envelope_sha256") != task_envelope_sha256
            ):
                raise JournalConflict("root, binding, and attempt task envelopes differ")
            existing = self._connection.execute(
                """
                SELECT tool_use_id,task_envelope_sha256,binding,binding_sha256,
                       spawn_event,rooted_authority
                FROM rrcv2p_cell_attempts WHERE cell_id=? AND attempt_id=?
                """,
                (root.cell.cell_id, attempt_id),
            ).fetchone()
            exact = (
                tool_use_id,
                task_envelope_sha256,
                binding_raw,
                binding_ref.sha256,
                spawn_raw,
                authority_raw,
            )
            if existing is not None:
                if existing != exact:
                    raise JournalConflict("attempt already has a conflicting cell binding")
                return
            self._connection.execute(
                """
                INSERT INTO rrcv2p_cell_attempts(
                    cell_id,attempt_id,tool_use_id,task_envelope_sha256,binding,
                    binding_sha256,spawn_event,rooted_authority
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    root.cell.cell_id,
                    attempt_id,
                    tool_use_id,
                    task_envelope_sha256,
                    binding_raw,
                    binding_ref.sha256,
                    spawn_raw,
                    authority_raw,
                ),
            )

        self.repository.run_immediate(bind)
        binding_ref = _atomic_authority(binding_ref.path, binding_raw)
        spawn_ref = _atomic_authority(spawn_ref.path, spawn_raw)
        authority_ref = _atomic_authority(authority_ref.path, authority_raw)
        view = ProductRootedAttemptJournalViewV1(
            authority_ref,
            root.root_permit_ref,
            root.root_started_ref,
            root.root_launch_identity_ref,
            spawn_ref,
        )
        return BoundAttemptV1(self._handle(root.cell.cell_id), binding_ref, view)

    def load_rooted_attempt_authority(
        self, *, cell_id: str, attempt_id: str
    ) -> ProductRootedAttemptJournalViewV1 | None:
        _identifier(cell_id, name="cell_id")
        _hex64(attempt_id, name="attempt_id")
        row = self._connection.execute(
            """
            SELECT binding,spawn_event,rooted_authority
            FROM rrcv2p_cell_attempts WHERE cell_id=? AND attempt_id=?
            """,
            (cell_id, attempt_id),
        ).fetchone()
        if row is None:
            return None
        root = self._connection.execute(
            """
            SELECT root_permit,root_started,root_launch_identity
            FROM rrcv2p_cells WHERE cell_id=?
            """,
            (cell_id,),
        ).fetchone()
        if root is None or any(item is None for item in root):
            raise JournalConflict("rooted attempt is missing root authorities")
        base = self.authority_root / cell_id
        binding_ref = _atomic_authority(base / attempt_id / "binding.json", cast(bytes, row[0]))
        del binding_ref
        return ProductRootedAttemptJournalViewV1(
            _atomic_authority(base / attempt_id / "authority.json", cast(bytes, row[2])),
            _atomic_authority(base / "root-permit.json", cast(bytes, root[0])),
            _atomic_authority(base / "root-started.json", cast(bytes, root[1])),
            _atomic_authority(base / "root-launch.json", cast(bytes, root[2])),
            _atomic_authority(base / attempt_id / "spawn.json", cast(bytes, row[1])),
        )

    def complete_bound_attempt(
        self,
        *,
        cell_id: str,
        attempt_id: str,
        tool_use_id: str,
        agent_id: str,
        expected_generation: int,
    ) -> None:
        _identifier(cell_id, name="cell_id")
        _hex64(attempt_id, name="attempt_id")
        _tool_id(tool_use_id, name="tool_use_id")
        _tool_id(agent_id, name="agent_id")

        def complete(_uow: object) -> None:
            current = self._handle(cell_id)
            if current.generation != expected_generation or current.state != "root_started":
                raise JournalStateError("cell generation differs while completing spawn")
            row = self._connection.execute(
                """
                SELECT tool_use_id,agent_id,completed FROM rrcv2p_cell_attempts
                WHERE cell_id=? AND attempt_id=?
                """,
                (cell_id, attempt_id),
            ).fetchone()
            if row is None or row[0] != tool_use_id:
                raise JournalConflict("completed spawn differs from its rooted binding")
            if row[2] == 1:
                if row[1] != agent_id:
                    raise JournalConflict("spawn completion agent conflicts")
                return
            if row[1] is not None:
                raise JournalConflict("spawn completion was partially written")
            changed = self._connection.execute(
                """
                UPDATE rrcv2p_cell_attempts SET agent_id=?,completed=1
                WHERE cell_id=? AND attempt_id=? AND agent_id IS NULL AND completed=0
                """,
                (agent_id, cell_id, attempt_id),
            )
            if changed.rowcount != 1:
                raise JournalStateError("spawn completion compare-and-swap failed")

        self.repository.run_immediate(complete)

    def binding_ref(self, *, cell_id: str, attempt_id: str) -> AuthorityRef:
        row = self._connection.execute(
            "SELECT binding FROM rrcv2p_cell_attempts WHERE cell_id=? AND attempt_id=?",
            (cell_id, attempt_id),
        ).fetchone()
        if row is None:
            raise JournalStateError("cell-attempt binding is missing")
        return _atomic_authority(
            self.authority_root / cell_id / attempt_id / "binding.json", cast(bytes, row[0])
        )

    def record_root_tool_event(
        self,
        *,
        cell_id: str,
        event: RootToolEventV1,
        expected_generation: int,
    ) -> str:
        """Persist one exact wait/apply projection before the root can finish."""

        _identifier(cell_id, name="cell_id")
        if not isinstance(event, RootToolEventV1):
            raise TypeError("event must be RootToolEventV1")
        event_raw = event.canonical_bytes()
        event_sha = _sha(event_raw)

        def record(_uow: object) -> None:
            current = self._handle(cell_id)
            if current.state != "root_started" or current.generation != expected_generation:
                raise JournalStateError("root tool event requires the current started root")
            existing = self._connection.execute(
                """
                SELECT event,event_sha256,output FROM rrcv2p_cell_tool_events
                WHERE cell_id=? AND tool_use_id=?
                """,
                (cell_id, event.tool_use_id),
            ).fetchone()
            exact = (event_raw, event_sha, event.output)
            if existing is not None:
                if existing != exact:
                    raise JournalConflict("root tool event replay differs")
                return
            sequence = self._connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM rrcv2p_cell_tool_events WHERE cell_id=?",
                (cell_id,),
            ).fetchone()[0]
            self._connection.execute(
                """
                INSERT INTO rrcv2p_cell_tool_events(
                    cell_id,sequence,kind,tool_use_id,event,event_sha256,output
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    cell_id,
                    sequence,
                    event.kind,
                    event.tool_use_id,
                    event_raw,
                    event_sha,
                    event.output,
                ),
            )

        self.repository.run_immediate(record)
        return event_sha

    def observe_root_call(
        self,
        *,
        cell_id: str,
        expected_generation: int,
        root_session_id: str,
        prompt_sha256: str,
        final_message_sha256: str,
        transcript_sha256: str,
        transcript_bytes: int,
        requested_provider: str,
        requested_model: str,
        requested_reasoning: str,
        requested_service_tier: str,
        identity_attestation: str,
        effective_provider: str,
        effective_model: str,
        effective_reasoning: str,
        effective_service_tier: str,
        usage: Mapping[str, int],
    ) -> CellHandleV1:
        """Observe the one cumulative root final without pricing tool events."""

        _identifier(cell_id, name="cell_id")
        _tool_id(root_session_id, name="root_session_id")
        for value, name in (
            (prompt_sha256, "root prompt_sha256"),
            (final_message_sha256, "root final_message_sha256"),
            (transcript_sha256, "root transcript_sha256"),
        ):
            _hex64(value, name=name)
        if isinstance(transcript_bytes, bool) or not isinstance(transcript_bytes, int):
            raise TypeError("root transcript_bytes must be an integer")
        if not 0 <= transcript_bytes <= 64 * 1024 * 1024:
            raise ValueError("root transcript_bytes exceeds its cap")
        usage_names = {
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "provider_total_tokens",
        }
        if set(usage) != usage_names:
            raise ValueError("root usage fields differ")
        cost_probe = CostEventV1(
            cost_event_id="0" * 64,
            cell_id=cell_id,
            attempt_id=None,
            task_id="root-probe",
            arm="rrc_cold",
            stage="contextmesh_root_session",
            stage_ordinal=1,
            prompt_sha256=prompt_sha256,
            final_message_sha256=final_message_sha256,
            transcript_sha256=transcript_sha256,
            requested_provider=requested_provider,
            requested_model=requested_model,
            requested_reasoning=requested_reasoning,
            requested_service_tier=requested_service_tier,
            identity_attestation=identity_attestation,
            effective_provider=effective_provider,
            effective_model=effective_model,
            effective_reasoning=effective_reasoning,
            effective_service_tier=effective_service_tier,
            input_tokens=usage["input_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            output_tokens=usage["output_tokens"],
            reasoning_output_tokens=usage["reasoning_output_tokens"],
            provider_total_tokens=usage["provider_total_tokens"],
        )
        transcript_raw = canonical_json(
            {"bytes": transcript_bytes, "sha256": transcript_sha256, "v": 1}
        )
        usage_raw = canonical_json(
            {
                **cost_probe.as_json(),
                "cost_event_id": None,
                "task_id": None,
                "arm": None,
                "stage": None,
                "stage_ordinal": None,
                "root_session_id": root_session_id,
            }
        )

        def observe(_uow: object) -> None:
            current = self._handle(cell_id)
            row = self._connection.execute(
                "SELECT root_transcript_ref,root_usage FROM rrcv2p_cells WHERE cell_id=?",
                (cell_id,),
            ).fetchone()
            if current.state in {"root_observed", "root_committed", "combined_committed"}:
                if row != (transcript_raw, usage_raw):
                    raise JournalConflict("root observation replay differs")
                return
            if current.state != "root_started" or current.generation != expected_generation:
                raise JournalStateError("root observation requires the current started root")
            completed = self._connection.execute(
                "SELECT COUNT(*) FROM rrcv2p_cell_attempts WHERE cell_id=? AND completed=1",
                (cell_id,),
            ).fetchone()[0]
            if completed != 1:
                raise JournalStateError("root final requires exactly one completed spawn")
            changed = self._connection.execute(
                """
                UPDATE rrcv2p_cells
                SET state='root_observed',generation=generation+1,
                    root_transcript_ref=?,root_usage=?
                WHERE cell_id=? AND state='root_started' AND generation=?
                """,
                (transcript_raw, usage_raw, cell_id, expected_generation),
            )
            if changed.rowcount != 1:
                raise JournalStateError("root observation compare-and-swap failed")

        self.repository.run_immediate(observe)
        return self._handle(cell_id)

    def commit_root_cost_event(self, *, cell_id: str, expected_generation: int) -> CostEventV1:
        """Commit the one whole-root CostEventV1 after cumulative usage observation."""

        _identifier(cell_id, name="cell_id")
        row = self._connection.execute(
            """
            SELECT request,root_usage,root_cost_event,state,generation
            FROM rrcv2p_cells WHERE cell_id=?
            """,
            (cell_id,),
        ).fetchone()
        if row is None:
            raise JournalStateError("product cell is missing")
        if row[2] is not None:
            existing = CostEventV1(**cast(dict[str, Any], json.loads(cast(bytes, row[2]))))
            if existing.canonical_bytes() != row[2]:
                raise JournalConflict("stored root cost event is not canonical")
            return existing
        if row[3] != "root_observed" or row[4] != expected_generation:
            raise JournalStateError("root cost commit requires the current observation")
        request = cast(dict[str, Any], json.loads(cast(bytes, row[0])))
        observed = cast(dict[str, Any], json.loads(cast(bytes, row[1])))
        event = CostEventV1(
            cost_event_id=cast(str, request["call_id"]),
            cell_id=cell_id,
            attempt_id=None,
            task_id=cast(str, request["task_id"]),
            arm=cast(str, request["arm"]),
            stage="contextmesh_root_session",
            stage_ordinal=1,
            prompt_sha256=cast(str, observed["prompt_sha256"]),
            final_message_sha256=cast(str, observed["final_message_sha256"]),
            transcript_sha256=cast(str, observed["transcript_sha256"]),
            requested_provider=cast(str, observed["requested_provider"]),
            requested_model=cast(str, observed["requested_model"]),
            requested_reasoning=cast(str, observed["requested_reasoning"]),
            requested_service_tier=cast(str, observed["requested_service_tier"]),
            identity_attestation=cast(str, observed["identity_attestation"]),
            effective_provider=cast(str, observed["effective_provider"]),
            effective_model=cast(str, observed["effective_model"]),
            effective_reasoning=cast(str, observed["effective_reasoning"]),
            effective_service_tier=cast(str, observed["effective_service_tier"]),
            input_tokens=cast(int, observed["input_tokens"]),
            cached_input_tokens=cast(int, observed["cached_input_tokens"]),
            output_tokens=cast(int, observed["output_tokens"]),
            reasoning_output_tokens=cast(int, observed["reasoning_output_tokens"]),
            provider_total_tokens=cast(int, observed["provider_total_tokens"]),
        )
        raw = event.canonical_bytes()

        def commit(_uow: object) -> None:
            current = self._handle(cell_id)
            if current.state != "root_observed" or current.generation != expected_generation:
                raise JournalStateError("root cost commit cursor differs")
            changed = self._connection.execute(
                """
                UPDATE rrcv2p_cells
                SET state='root_committed',generation=generation+1,root_cost_event=?
                WHERE cell_id=? AND state='root_observed' AND generation=?
                """,
                (raw, cell_id, expected_generation),
            )
            if changed.rowcount != 1:
                raise JournalStateError("root cost compare-and-swap failed")

        self.repository.run_immediate(commit)
        return event

    def build_combined_session(self, *, cell_id: str, round_id: str) -> CombinedSessionRecordV1:
        """Reopen all terminal/root/tool authority and construct the exact cell union."""

        _identifier(cell_id, name="cell_id")
        _identifier(round_id, name="round_id")
        cell = self._connection.execute(
            """
            SELECT root_call_id,root_usage,root_cost_event,state
            FROM rrcv2p_cells WHERE cell_id=?
            """,
            (cell_id,),
        ).fetchone()
        if cell is None or cell[3] not in {"root_committed", "combined_committed"}:
            raise JournalStateError("combined session requires a committed root")
        root_cost = CostEventV1(**cast(dict[str, Any], json.loads(cast(bytes, cell[2]))))
        if root_cost.canonical_bytes() != cell[2] or root_cost.attempt_id is not None:
            raise JournalConflict("combined root cost authority differs")
        root_usage = cast(dict[str, Any], json.loads(cast(bytes, cell[1])))
        tools = self._connection.execute(
            """
            SELECT kind,event,event_sha256,output FROM rrcv2p_cell_tool_events
            WHERE cell_id=? ORDER BY sequence
            """,
            (cell_id,),
        ).fetchall()
        wait_rows = [row for row in tools if row[0] == "wait"]
        apply_rows = [row for row in tools if row[0] == "apply"]
        if not wait_rows:
            raise JournalStateError("combined session has no observed wait")
        wait_output = cast(bytes, wait_rows[-1][3])
        wait = parse_wait_envelope(wait_output)
        attempt_rows = self._connection.execute(
            """
            SELECT attempt_id,spawn_event,agent_id,completed
            FROM rrcv2p_cell_attempts WHERE cell_id=? ORDER BY attempt_id
            """,
            (cell_id,),
        ).fetchall()
        if len(attempt_rows) != 1 or attempt_rows[0][3] != 1:
            raise JournalStateError("combined session requires one completed bound attempt")
        combined_attempts: list[CombinedSessionAttemptV1] = []
        cost_ids = {root_cost.cost_event_id}
        tool_hashes = [_sha(cast(bytes, attempt_rows[0][1]))]
        tool_hashes.extend(cast(str, row[2]) for row in tools)
        for attempt_id, _spawn, agent_id, _completed in attempt_rows:
            terminal = self.repository.load_terminal_intent(cast(str, attempt_id))
            if terminal is None:
                raise JournalStateError("combined attempt is not terminal")
            kind, terminal_raw = terminal
            wait_targets = [target for target in wait.targets if target.agent_id == agent_id]
            if len(wait_targets) != 1 or getattr(wait_targets[0], "attempt_id", None) != attempt_id:
                raise JournalConflict("combined wait target differs from its bound attempt")
            if kind == "accepted":
                accepted = parse_accepted_commit(terminal_raw)
                if accepted.receipt_record is None:
                    raise JournalConflict("accepted combined attempt has no receipt")
                receipt = parse_receipt_record(accepted.receipt_record).receipt
                target = cast(Any, wait_targets[0])
                if getattr(target, "receipt", None) != receipt:
                    raise JournalConflict("combined wait receipt differs")
                matches = []
                for apply_row in apply_rows:
                    value = json.loads(cast(bytes, apply_row[3]))
                    if (
                        isinstance(value, dict)
                        and value.get("attempt_id") == attempt_id
                        and value.get("receipt") == receipt
                    ):
                        matches.append(apply_row)
                if len(matches) != 1:
                    raise JournalConflict("accepted combined attempt lacks one exact apply event")
                outcome = accepted.outcome
                combined_attempts.append(
                    CombinedSessionAttemptV1(
                        cast(str, attempt_id),
                        "accepted",
                        _sha(outcome.canonical_bytes()),
                        receipt,
                        cast(str, matches[0][2]),
                    )
                )
                cost_ids.update(outcome.cost_event_ids)
            elif kind == "rejected":
                rejected = parse_rejected_commit(terminal_raw).rejected_outcome
                combined_attempts.append(
                    CombinedSessionAttemptV1(
                        cast(str, attempt_id),
                        "rejected",
                        _sha(rejected.canonical_bytes()),
                        None,
                        None,
                    )
                )
                cost_ids.update(rejected.cost_event_ids)
            else:
                raise JournalConflict("combined terminal kind is invalid")
            inventory = self.repository.load_call_inventory(cast(str, attempt_id))
            if any(state != "call_committed" or raw is None for _call, state, raw in inventory) or {
                call_id for call_id, _state, _raw in inventory
            } != cost_ids - {root_cost.cost_event_id}:
                raise JournalConflict("combined attempt cost inventory differs")
        return CombinedSessionRecordV1(
            cell_id=cell_id,
            round_id=round_id,
            root_session_id=cast(str, root_usage["root_session_id"]),
            root_cost_event_id=root_cost.cost_event_id,
            root_transcript_sha256=root_cost.transcript_sha256,
            root_final_sha256=root_cost.final_message_sha256,
            wait_id=wait.wait_id,
            wait_envelope_sha256=_sha(wait_output),
            tool_event_sha256s=tuple(tool_hashes),
            attempts=tuple(combined_attempts),
            all_cost_event_ids=tuple(sorted(cost_ids)),
        )

    def commit_combined_session(
        self, record: CombinedSessionRecordV1, *, expected_generation: int
    ) -> CombinedSessionRecordV1:
        """Atomically publish one exact cost union without mutating attempt outcomes."""

        if not isinstance(record, CombinedSessionRecordV1):
            raise TypeError("record must be CombinedSessionRecordV1")
        rebuilt = self.build_combined_session(cell_id=record.cell_id, round_id=record.round_id)
        raw = record.canonical_bytes()
        if rebuilt.canonical_bytes() != raw:
            raise JournalConflict("combined session differs from reopened authority")

        def commit(_uow: object) -> None:
            current = self._handle(record.cell_id)
            row = self._connection.execute(
                "SELECT combined_session FROM rrcv2p_cells WHERE cell_id=?",
                (record.cell_id,),
            ).fetchone()
            if current.state == "combined_committed":
                if row is None or row[0] != raw:
                    raise JournalConflict("combined session replay differs")
                return
            if current.state != "root_committed" or current.generation != expected_generation:
                raise JournalStateError("combined session cursor differs")
            changed = self._connection.execute(
                """
                UPDATE rrcv2p_cells
                SET state='combined_committed',generation=generation+1,combined_session=?
                WHERE cell_id=? AND state='root_committed' AND generation=?
                """,
                (raw, record.cell_id, expected_generation),
            )
            if changed.rowcount != 1:
                raise JournalStateError("combined session compare-and-swap failed")

        self.repository.run_immediate(commit)
        return record

    def load_combined_session(self, cell_id: str) -> CombinedSessionRecordV1 | None:
        _identifier(cell_id, name="cell_id")
        row = self._connection.execute(
            "SELECT combined_session FROM rrcv2p_cells WHERE cell_id=?", (cell_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        value = cast(dict[str, Any], json.loads(cast(bytes, row[0])))
        attempts = tuple(
            CombinedSessionAttemptV1(**cast(dict[str, Any], item))
            for item in cast(list[object], value.pop("attempts"))
        )
        value["tool_event_sha256s"] = tuple(value["tool_event_sha256s"])
        value["all_cost_event_ids"] = tuple(value["all_cost_event_ids"])
        record = CombinedSessionRecordV1(attempts=attempts, **value)
        if record.canonical_bytes() != row[0]:
            raise JournalConflict("stored combined session is not canonical")
        return record

    def load_root_cost_event(self, cell_id: str) -> CostEventV1 | None:
        _identifier(cell_id, name="cell_id")
        row = self._connection.execute(
            "SELECT root_cost_event FROM rrcv2p_cells WHERE cell_id=?", (cell_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        event = CostEventV1(**cast(dict[str, Any], json.loads(cast(bytes, row[0]))))
        if event.canonical_bytes() != row[0] or event.attempt_id is not None:
            raise JournalConflict("stored root cost event is not canonical cell usage")
        return event


__all__ = [
    "BoundAttemptV1",
    "CellHandleV1",
    "CombinedSessionAttemptV1",
    "CombinedSessionRecordV1",
    "RootToolEventV1",
    "RootStartedHandleV1",
    "SQLiteCellJournal",
]
