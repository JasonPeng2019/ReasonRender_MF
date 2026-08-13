"""Arm-local, cross-process broker for plan-bound ContextMesh file briefs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from contextmesh.mcp.file_brief import (
    BriefValidationError,
    ValidatedFileBrief,
    brief_contract,
    peer_brief_payload,
    peer_brief_size,
    validate_file_brief,
)
from harness.four_worker_plan import OverlapLedgerEntry

# A plan-scoped excerpt stays comfortably below this limit.  The larger
# fallback protects a source whose required facts cannot be located without
# restoring the old nine-turn, repeatedly-contextualized delivery path.
MAX_SOURCE_CHUNK_BYTES = 160_000
MAX_PLAN_SCOPED_SOURCE_VIEW_BYTES = 12_000
# Preserve enough module framing for an owner to interpret a selected record,
# without letting an unrelated generated-registry tail evict an otherwise
# complete plan-scoped excerpt from the cap.
PLAN_SCOPED_HEADER_BYTES = 1_024
PLAN_SCOPED_TAIL_BYTES = 512
# A generated PolicyProfile record is below this range.  Keeping the raw
# selector tight prevents many adjacent required records from merging into a
# full-file fallback as the append-only workload grows.
PLAN_SCOPED_RECORD_BEFORE_BYTES = 512
PLAN_SCOPED_RECORD_AFTER_BYTES = 512


class BrokerError(RuntimeError):
    """A visible broker outcome; its code is retained in run evidence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SourceClaim:
    """The owner-only input for a first raw claim or later diff refresh."""

    brief_id: str
    source_hash: str
    source_content: str
    brief_template: dict[str, Any]
    source_chunk_index: int
    source_chunk_count: int
    claim_kind: Literal["raw", "diff_refresh", "invalidated_raw", "unchanged_reuse"] = "raw"
    prior_summary: dict[str, str] | None = None
    reused_summary: dict[str, str] | None = None
    prior_source_hash: str | None = None
    git_diff: str | None = None
    invalidation_reason: str | None = None


@dataclass(slots=True)
class _BriefState:
    entry: OverlapLedgerEntry
    status: Literal["unclaimed", "owner_raw", "owner_diff", "brief_published", "brief_missing"] = "unclaimed"
    source_hash: str | None = None
    source_content: str | None = None
    source_chunks: tuple[str, ...] = ()
    delivered_chunk_count: int = 0
    brief: ValidatedFileBrief | None = None
    refresh_kind: Literal["raw", "unchanged_reuse", "diff_refresh", "invalidated_raw"] = "raw"
    parent_source_hash: str | None = None
    prior_summary: dict[str, str] | None = None
    git_diff: str | None = None
    invalidation_reason: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SharedBriefBroker:
    """One durable broker for every ContextMesh worker in a non-raw arm."""

    def __init__(
        self,
        source_root: str | Path,
        entries: tuple[OverlapLedgerEntry, ...] | list[OverlapLedgerEntry],
        *,
        state_dir: str | Path | None = None,
        log_path: str | Path | None = None,
        wait_timeout_ms: int = 600_000,
        workflow_id: str | None = None,
        stage_id: str | None = None,
        stage_commit: str | None = None,
        parent_stage_commit: str | None = None,
        branch_lineage: str = "main",
        git_root: str | Path | None = None,
        max_diff_bytes: int = 48_000,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        if not self.source_root.is_dir():
            raise ValueError("source_root must be an existing directory")
        self.state_dir = Path(state_dir).resolve() if state_dir else None
        self.log_path = Path(log_path) if log_path else None
        self.wait_timeout_ms = wait_timeout_ms
        if wait_timeout_ms <= 0:
            raise ValueError("wait_timeout_ms must be positive")
        staged_fields = (workflow_id, stage_id, stage_commit)
        if any(value is not None for value in staged_fields) and not all(
            isinstance(value, str) and value for value in staged_fields
        ):
            raise ValueError("workflow_id, stage_id, and stage_commit are required together")
        if not isinstance(branch_lineage, str) or not branch_lineage:
            raise ValueError("branch_lineage must be a non-empty string")
        if max_diff_bytes <= 0:
            raise ValueError("max_diff_bytes must be positive")
        self.workflow_id = workflow_id
        self.stage_id = stage_id
        self.stage_commit = stage_commit
        self.parent_stage_commit = parent_stage_commit
        self.branch_lineage = branch_lineage
        self.git_root = Path(git_root).resolve() if git_root else self.source_root
        self.max_diff_bytes = max_diff_bytes
        self._states: dict[str, _BriefState] = {}
        self._owner_claim_locks: dict[str, asyncio.Lock] = {}
        for entry in entries:
            if not isinstance(entry, OverlapLedgerEntry):
                raise ValueError("entries must contain OverlapLedgerEntry values")
            if entry.brief_id in self._states:
                raise ValueError(f"duplicate brief_id {entry.brief_id}")
            self._states[entry.brief_id] = _BriefState(entry=entry)
        self.events: list[dict[str, object]] = []
        if self.workflow_id is None:
            self._rehydrate_published_briefs()
        else:
            self._prepare_staged_revisions()
            self._rehydrate_current_staged_briefs()

    def _event(self, event: str, **fields: object) -> None:
        row = {"event": event, "ts": int(time.time() * 1000), **fields}
        self.events.append(row)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\n")

    def _state(self, brief_id: str) -> _BriefState:
        try:
            return self._states[brief_id]
        except KeyError as error:
            raise BrokerError("unknown_brief", f"unknown brief_id {brief_id}") from error

    def bridge_identity(self, worker_id: str, bridge_pid: int, endpoint: str) -> dict[str, str | int]:
        """Record that one independent stdio bridge joined this broker.

        The value is diagnostic-only and deliberately excludes broker control
        credentials.  It makes the multi-process topology auditable without
        placing a second state store in any worker process.
        """

        if not isinstance(worker_id, str) or not worker_id or not isinstance(bridge_pid, int) or bridge_pid < 1:
            raise BrokerError("bad_request", "bridge identity requires a worker id and positive pid")
        if not isinstance(endpoint, str) or not endpoint:
            raise BrokerError("bad_request", "bridge identity requires a broker endpoint")
        authorized = any(
            worker_id == state.entry.source_owner or worker_id in state.entry.peer_workers
            for state in self._states.values()
        )
        if not authorized:
            raise BrokerError("not_worker", f"{worker_id} is not a broker worker")
        identity = hashlib.sha256(
            json.dumps(
                {
                    "workflow_id": self.workflow_id or "standalone",
                    "stage_id": self.stage_id or "standalone",
                    "source_root": str(self.source_root),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._event(
            "bridge_started",
            worker_id=worker_id,
            bridge_pid=bridge_pid,
            broker_endpoint=endpoint,
            broker_identity=identity,
        )
        return {"broker_identity": identity, "bridge_pid": bridge_pid}

    def _rehydrate_published_briefs(self) -> None:
        """Restore durable owner briefs when a harness interruption restarts the broker.

        The persisted payload was already validated against the owner-only raw
        source before it was written.  Resume must not reread that raw source
        merely to recreate in-memory state: doing so would turn a dispatcher
        interruption into a second reader.  We therefore accept only a payload
        whose immutable ledger binding exactly matches the current entry.
        """

        if self.state_dir is None or not self.state_dir.is_dir():
            return
        for state in self._states.values():
            matches = sorted(self.state_dir.glob(f"*-{state.entry.requirements_hash}-file-brief_v1.json"))
            for path in matches:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    brief_id = payload["brief_id"]
                    source_hash = payload["source_hash"]
                    brief = payload["brief"]
                    binding = brief["binding"]
                    source = brief["source"]
                except (OSError, KeyError, TypeError, json.JSONDecodeError):
                    continue
                if not (
                    isinstance(brief_id, str)
                    and brief_id == state.entry.brief_id
                    and isinstance(source_hash, str)
                    and isinstance(brief, dict)
                    and isinstance(binding, dict)
                    and isinstance(source, dict)
                    and binding.get("brief_id") == state.entry.brief_id
                    and binding.get("requirements_hash") == state.entry.requirements_hash
                    and binding.get("source_owner") == state.entry.source_owner
                    and tuple(binding.get("peer_workers", ())) == state.entry.peer_workers
                    and source.get("canonical_path") == state.entry.canonical_path
                    and source.get("content_sha256") == source_hash
                ):
                    continue
                state.source_hash = source_hash
                state.brief = cast(ValidatedFileBrief, brief)
                state.status = "brief_published"
                state.done.set()
                self._event(
                    "brief_rehydrated",
                    brief_id=state.entry.brief_id,
                    source_hash=source_hash,
                    state_path=str(path),
                )
                break

    def _lineage_id(self, entry: OverlapLedgerEntry) -> str:
        return f"{self.branch_lineage}:{entry.canonical_path}"

    @staticmethod
    def _brief_facts_hash(entry: OverlapLedgerEntry) -> str:
        return hashlib.sha256(
            json.dumps(entry.required_facts, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()

    def _lineage_path(self, state: _BriefState, source_hash: str) -> Path:
        assert self.state_dir is not None
        assert self.workflow_id is not None and self.stage_id is not None
        identity = {
            "workflow_id": self.workflow_id,
            "stage_id": self.stage_id,
            "lineage_id": self._lineage_id(state.entry),
            "brief_id": state.entry.brief_id,
            "source_hash": source_hash,
            "requirements_hash": state.entry.requirements_hash,
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return self.state_dir / "lineage" / f"{digest}.json"

    def _lineage_payload(
        self, state: _BriefState, source_hash: str, brief: ValidatedFileBrief
    ) -> dict[str, object]:
        assert self.workflow_id is not None and self.stage_id is not None and self.stage_commit is not None
        return {
            "brief_id": state.entry.brief_id,
            "source_hash": source_hash,
            "brief": brief,
            "lineage": {
                "schema_version": "contextmesh-lineage/v1",
                "workflow_id": self.workflow_id,
                "lineage_id": self._lineage_id(state.entry),
                "branch_lineage": self.branch_lineage,
                "stage_id": self.stage_id,
                "stage_commit": self.stage_commit,
                "parent_stage_commit": self.parent_stage_commit,
                "canonical_path": state.entry.canonical_path,
                "content_sha256": source_hash,
                "parent_content_sha256": state.parent_source_hash,
                "requirements_hash": state.entry.requirements_hash,
                "brief_facts_hash": self._brief_facts_hash(state.entry),
                "brief_schema_version": str(brief["schema_version"]),
                "refresh_kind": state.refresh_kind,
                "invalidation_reason": state.invalidation_reason,
            },
        }

    def _persist_lineage_sync(self, state: _BriefState, source_hash: str, brief: ValidatedFileBrief) -> None:
        if self.state_dir is None or self.workflow_id is None:
            return
        target = self._lineage_path(state, source_hash)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._lineage_payload(state, source_hash, brief), sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, target)

    def _prior_lineage_revision(self, state: _BriefState, current_source_hash: str) -> dict[str, object] | None:
        if self.state_dir is None or self.workflow_id is None or not self.state_dir.is_dir():
            return None
        candidates: list[tuple[int, dict[str, object]]] = []
        for path in self.state_dir.glob("lineage/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                lineage = payload["lineage"]
                brief = payload["brief"]
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if not isinstance(lineage, dict) or not isinstance(brief, dict):
                continue
            if not (
                lineage.get("workflow_id") == self.workflow_id
                and lineage.get("lineage_id") == self._lineage_id(state.entry)
                and lineage.get("canonical_path") == state.entry.canonical_path
                and lineage.get("brief_schema_version") == "file-brief/v1"
                and isinstance(payload.get("source_hash"), str)
            ):
                continue
            if self.parent_stage_commit is not None and lineage.get("stage_commit") != self.parent_stage_commit:
                continue
            # A retained product-error attempt may have persisted a newer
            # summary under an incompatible requirements contract.  For an
            # unchanged source, never let it eclipse an older valid brief just
            # because it has a later mtime.  A changed source is different:
            # its Git diff can supply newly required facts, so keep the prior
            # record for the diff-refresh check in _prepare_staged_revisions.
            summary = self._stored_summary(payload)
            if summary is None:
                continue
            if payload.get("source_hash") == current_source_hash and not self._source_fact_coverage(
                "\n".join(summary.values()), state.entry
            ):
                continue
            candidates.append((path.stat().st_mtime_ns, payload))
        return max(candidates, default=(0, None), key=lambda item: item[0])[1]

    @staticmethod
    def _source_fact_coverage(text: str, entry: OverlapLedgerEntry) -> bool:
        """True when a predecessor brief plus diff proves every next-stage fact."""

        normalized = "".join(character for character in text.casefold() if character.isalnum())
        return all(
            "".join(character for character in fact.casefold() if character.isalnum()) in normalized
            for fact in entry.required_facts
        )

    @staticmethod
    def _stored_summary(payload: Mapping[str, object]) -> dict[str, str] | None:
        brief = payload.get("brief")
        if not isinstance(brief, Mapping):
            return None
        summary = brief.get("summary")
        if not isinstance(summary, Mapping):
            return None
        result = {str(key): value for key, value in summary.items() if isinstance(value, str)}
        return result if len(result) == 5 else None

    def _git_diff(self, entry: OverlapLedgerEntry) -> tuple[str | None, str | None]:
        if self.parent_stage_commit is None or not self.git_root.is_dir():
            return None, "git_diff_unavailable"
        completed = subprocess.run(
            [
                "git",
                "diff",
                "--no-ext-diff",
                f"{self.parent_stage_commit}..{self.stage_commit}",
                "--",
                entry.canonical_path,
            ],
            cwd=self.git_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return None, "git_diff_unavailable"
        diff = completed.stdout
        if not diff:
            return None, "git_diff_missing"
        if len(diff.encode("utf-8")) > self.max_diff_bytes:
            return None, "git_diff_exceeds_budget"
        return diff, None

    def _prepare_staged_revisions(self) -> None:
        """Bind prior valid briefs to the next stage without exposing source text.

        The broker reads a file only to compare hashes and validate a submitted
        brief.  It never sends that body to a later peer; a changed descendant
        gives its sole refresh owner the prior compact brief and Git diff.
        """

        assert self.workflow_id is not None
        for state in self._states.values():
            try:
                source = self._source_path(state.entry).read_text(encoding="utf-8")
            except OSError:
                continue
            source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
            previous = self._prior_lineage_revision(state, source_hash)
            if previous is None:
                continue
            previous_hash = previous.get("source_hash")
            summary = self._stored_summary(previous)
            lineage = previous.get("lineage")
            if not isinstance(previous_hash, str) or summary is None or not isinstance(lineage, Mapping):
                state.refresh_kind = "invalidated_raw"
                state.invalidation_reason = "missing_valid_ancestor"
                continue
            state.parent_source_hash = previous_hash
            # A later worker may need a strict subset of an earlier complete
            # brief.  Plan changes must not force a raw owner merely because
            # the earlier brief retained additional, still-source-bound facts.
            same_brief_facts = self._source_fact_coverage("\n".join(summary.values()), state.entry)
            if source_hash == previous_hash:
                if not same_brief_facts:
                    state.refresh_kind = "invalidated_raw"
                    state.invalidation_reason = "ancestor_requirements_incomplete"
                    continue
                rebound = validate_file_brief(summary, state.entry, source)
                state.source_hash = source_hash
                state.brief = rebound
                state.refresh_kind = "unchanged_reuse"
                state.status = "brief_published"
                state.done.set()
                self._persist_lineage_sync(state, source_hash, rebound)
                self._event(
                    "brief_reused_unchanged",
                    brief_id=state.entry.brief_id,
                    workflow_id=self.workflow_id,
                    stage_id=self.stage_id,
                    source_hash=source_hash,
                    parent_source_hash=previous_hash,
                )
                continue
            diff, reason = self._git_diff(state.entry)
            if diff is None:
                state.refresh_kind = "invalidated_raw"
                state.invalidation_reason = reason
                continue
            if not self._source_fact_coverage("\n".join(summary.values()) + "\n" + diff, state.entry):
                state.refresh_kind = "invalidated_raw"
                state.invalidation_reason = "diff_cannot_cover_required_facts"
                continue
            state.source_content = source
            state.source_hash = source_hash
            state.prior_summary = summary
            state.git_diff = diff
            state.refresh_kind = "diff_refresh"

    def _rehydrate_current_staged_briefs(self) -> None:
        """Restore an interrupted current stage without authorizing another raw read.

        The normal staged lookup above deliberately starts from the parent
        commit.  A harness restart during that stage also needs to recognize a
        brief already persisted at the *current* commit; otherwise a completed
        owner would be asked to claim the file again.  The only accepted record
        is bound to this workflow, stage, commit, ledger requirements, and
        current source hash.
        """

        if self.state_dir is None or self.workflow_id is None or self.stage_id is None or self.stage_commit is None:
            return
        for state in self._states.values():
            if state.status == "brief_published":
                continue
            candidates: list[tuple[int, dict[str, object]]] = []
            for path in self.state_dir.glob("lineage/*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    lineage = payload["lineage"]
                    brief = payload["brief"]
                except (OSError, KeyError, TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(lineage, Mapping) or not isinstance(brief, Mapping):
                    continue
                if not (
                    lineage.get("workflow_id") == self.workflow_id
                    and lineage.get("stage_id") == self.stage_id
                    and lineage.get("stage_commit") == self.stage_commit
                    and lineage.get("lineage_id") == self._lineage_id(state.entry)
                    and lineage.get("requirements_hash") == state.entry.requirements_hash
                    and lineage.get("brief_schema_version") == "file-brief/v1"
                    and isinstance(payload.get("source_hash"), str)
                ):
                    continue
                candidates.append((path.stat().st_mtime_ns, payload))
            payload = max(candidates, default=(0, None), key=lambda item: item[0])[1]
            if not isinstance(payload, Mapping):
                continue
            summary = self._stored_summary(payload)
            source_hash = payload.get("source_hash")
            if summary is None or not isinstance(source_hash, str):
                continue
            try:
                source = self._source_path(state.entry).read_text(encoding="utf-8")
            except OSError:
                continue
            if hashlib.sha256(source.encode("utf-8")).hexdigest() != source_hash:
                continue
            try:
                state.brief = validate_file_brief(summary, state.entry, source)
            except BriefValidationError:
                continue
            state.source_hash = source_hash
            state.status = "brief_published"
            state.done.set()
            self._event(
                "brief_rehydrated_stage",
                brief_id=state.entry.brief_id,
                workflow_id=self.workflow_id,
                stage_id=self.stage_id,
                source_hash=source_hash,
            )

    def advance_stage(
        self,
        entries: tuple[OverlapLedgerEntry, ...] | list[OverlapLedgerEntry],
        *,
        stage_id: str,
        stage_commit: str,
        parent_stage_commit: str,
        branch_lineage: str | None = None,
    ) -> None:
        """Install the next immutable ledger on this arm's live broker.

        This is a harness-only boundary operation.  Worker bridge operations
        cannot invoke it, and each incoming brief id stays a distinct immutable
        revision while lineage lookup binds it to a prior path revision.
        """

        if self.workflow_id is None or self.stage_commit is None:
            raise BrokerError("stage_unavailable", "broker was not started for a staged workflow")
        if parent_stage_commit != self.stage_commit:
            raise BrokerError("stage_parent_mismatch", "next stage is not based on the live broker commit")
        if not isinstance(stage_id, str) or not stage_id or not isinstance(stage_commit, str) or not stage_commit:
            raise BrokerError("bad_request", "stage_id and stage_commit must be non-empty strings")
        if stage_id == self.stage_id:
            raise BrokerError("bad_request", "stage_id is already installed")
        if any(state.status in {"unclaimed", "owner_raw", "owner_diff"} for state in self._states.values()):
            raise BrokerError("stage_incomplete", "cannot advance while the current stage has unpublished briefs")
        if branch_lineage is not None and (not isinstance(branch_lineage, str) or not branch_lineage):
            raise BrokerError("bad_request", "branch_lineage must be a non-empty string")

        added: list[_BriefState] = []
        for entry in entries:
            if not isinstance(entry, OverlapLedgerEntry):
                raise BrokerError("bad_request", "entries must contain overlap-ledger entries")
            if entry.brief_id in self._states:
                raise BrokerError("bad_request", f"duplicate brief_id {entry.brief_id}")
            state = _BriefState(entry=entry)
            self._states[entry.brief_id] = state
            added.append(state)
        if not added:
            raise BrokerError("bad_request", "next stage ledger must not be empty")

        self.stage_id = stage_id
        self.parent_stage_commit = parent_stage_commit
        self.stage_commit = stage_commit
        if branch_lineage is not None:
            self.branch_lineage = branch_lineage
        self._prepare_staged_revisions_for(added)
        self._event(
            "stage_installed",
            workflow_id=self.workflow_id,
            stage_id=stage_id,
            stage_commit=stage_commit,
            parent_stage_commit=parent_stage_commit,
            brief_count=len(added),
        )

    def _prepare_staged_revisions_for(self, states: list[_BriefState]) -> None:
        """Run staged binding only for newly installed stage entries."""

        original = self._states
        try:
            self._states = {state.entry.brief_id: state for state in states}
            self._prepare_staged_revisions()
        finally:
            self._states = original

    @staticmethod
    def _one_id_edit_apart(supplied: str, canonical: str) -> bool:
        """Allow only a single insertion, deletion, or substitution in a long id."""

        if abs(len(supplied) - len(canonical)) > 1:
            return False
        index = other = edits = 0
        while index < len(supplied) and other < len(canonical):
            if supplied[index] == canonical[other]:
                index += 1
                other += 1
                continue
            edits += 1
            if edits > 1:
                return False
            if len(supplied) > len(canonical):
                index += 1
            elif len(supplied) < len(canonical):
                other += 1
            else:
                index += 1
                other += 1
        return edits + (len(supplied) - index) + (len(canonical) - other) == 1

    def _state_for(
        self,
        brief_id: str,
        worker_id: str,
        *,
        candidates: list[_BriefState],
        event: str,
        actor_field: str,
    ) -> _BriefState:
        """Resolve an authorized one-typo ID without widening source access."""

        try:
            return self._state(brief_id)
        except BrokerError as error:
            if error.code != "unknown_brief":
                raise
        matches = [state for state in candidates if self._one_id_edit_apart(brief_id, state.entry.brief_id)]
        if len(matches) != 1:
            raise BrokerError("unknown_brief", f"unknown brief_id {brief_id}")
        state = matches[0]
        self._event(
            event,
            brief_id=state.entry.brief_id,
            supplied_brief_id=brief_id,
            **{actor_field: worker_id},
        )
        return state

    def _claim_state(self, brief_id: str, worker_id: str) -> _BriefState:
        """Resolve a one-typo claim id only for the owner's unclaimed source."""

        return self._state_for(
            brief_id,
            worker_id,
            candidates=[
                state
                for state in self._states.values()
                if state.entry.source_owner == worker_id and state.status in {"unclaimed", "brief_published"}
            ],
            event="owner_claim_id_reconciled",
            actor_field="owner_id",
        )

    def _owner_claim_state(self, brief_id: str, worker_id: str, event: str) -> _BriefState:
        return self._state_for(
            brief_id,
            worker_id,
            candidates=[
                state
                for state in self._states.values()
                if state.entry.source_owner == worker_id and state.status in {"owner_raw", "owner_diff"}
            ],
            event=event,
            actor_field="owner_id",
        )

    def _peer_state(self, brief_id: str, worker_id: str) -> _BriefState:
        return self._state_for(
            brief_id,
            worker_id,
            candidates=[
                state
                for state in self._states.values()
                if worker_id in state.entry.peer_workers or worker_id == state.entry.source_owner
            ],
            event="peer_brief_id_reconciled",
            actor_field="peer_id",
        )

    def _source_path(self, entry: OverlapLedgerEntry) -> Path:
        candidate = (self.source_root / entry.canonical_path).resolve()
        if self.source_root != candidate and self.source_root not in candidate.parents:
            raise BrokerError("brief_missing", "ledger path escapes source_root")
        return candidate

    @staticmethod
    def _source_chunks(source: str) -> tuple[str, ...]:
        """Split a large owner-only source response below the MCP payload limit."""

        chunks: list[str] = []
        remaining = source
        while remaining:
            encoded = remaining.encode("utf-8")
            if len(encoded) <= MAX_SOURCE_CHUNK_BYTES:
                chunks.append(remaining)
                break
            candidate = encoded[:MAX_SOURCE_CHUNK_BYTES]
            while candidate:
                try:
                    text = candidate.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    candidate = candidate[:-1]
            else:  # pragma: no cover - valid UTF-8 input always leaves one character.
                raise ValueError("could not split source content")
            line_break = text.rfind("\n")
            if line_break >= len(text) // 2:
                text = text[: line_break + 1]
            chunks.append(text)
            remaining = remaining[len(text) :]
        return tuple(chunks)

    @staticmethod
    def _fact_anchor(fact: str) -> str:
        """Return the source selector carried by a compact ``a -> b`` fact."""

        anchor = fact.split(" -> ", 1)[0].strip()
        return anchor if anchor else ""

    @classmethod
    def _owner_source_view(cls, entry: OverlapLedgerEntry, source: str) -> tuple[str, str]:
        """Return a complete-for-plan raw view, or conservatively retain all source.

        Generated registries can be enormous even though a plan needs only a
        few named records.  The owner still receives raw source, but receives
        header and tail API context plus the raw record windows that prove all
        literal required facts.  Missing selectors fall back to the whole file
        rather than inventing a lossy projection.
        """

        if len(source.encode("utf-8")) <= MAX_PLAN_SCOPED_SOURCE_VIEW_BYTES:
            return source, "full_file"
        anchors = tuple(cls._fact_anchor(fact) for fact in entry.required_facts)
        if not anchors:
            return source, "full_file"
        positions = [source.find(anchor) for anchor in anchors]
        if any(position < 0 for position in positions):
            return source, "full_file"

        # Preserve module declarations and tail helpers, then include a small
        # raw window around each plan-selected record.  This is source text,
        # not a generated digest and it is delivered only to the sole owner.
        spans: list[tuple[int, int]] = [
            (0, min(len(source), PLAN_SCOPED_HEADER_BYTES)),
            (max(0, len(source) - PLAN_SCOPED_TAIL_BYTES), len(source)),
        ]
        for position in positions:
            start = source.rfind("\n", 0, max(0, position - PLAN_SCOPED_RECORD_BEFORE_BYTES)) + 1
            end_break = source.find("\n", min(len(source) - 1, position + PLAN_SCOPED_RECORD_AFTER_BYTES))
            end = len(source) if end_break < 0 else end_break + 1
            spans.append((start, end))
        spans.sort()
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        view = "\n# ContextMesh: plan-irrelevant raw sections omitted.\n".join(source[start:end] for start, end in merged)
        if len(view.encode("utf-8")) <= MAX_PLAN_SCOPED_SOURCE_VIEW_BYTES and all(anchor in view for anchor in anchors):
            return view, "plan_scoped_excerpt"
        return source, "full_file"

    async def claim_source(self, brief_id: str, worker_id: str) -> SourceClaim:
        """Award a raw/diff owner claim or return a completed owner reuse directly."""

        claim_lock = self._owner_claim_locks.setdefault(worker_id, asyncio.Lock())
        async with claim_lock:
            active = [
                state.entry.brief_id
                for state in self._states.values()
                if state.entry.source_owner == worker_id and state.status in {"owner_raw", "owner_diff"}
            ]
            if active:
                if active[0] == brief_id:
                    raise BrokerError("already_claimed", f"raw source was already claimed for {brief_id}")
                raise BrokerError(
                    "owner_prior_brief_unpublished",
                    f"{worker_id} must publish {active[0]} before claiming another source",
                )
            return await self._claim_source(brief_id, worker_id)

    async def _claim_source(self, brief_id: str, worker_id: str) -> SourceClaim:
        """Claim one source while the caller holds its serial owner lock."""

        state = self._claim_state(brief_id, worker_id)
        brief_id = state.entry.brief_id
        async with state.lock:
            if worker_id != state.entry.source_owner:
                raise BrokerError("not_source_owner", f"{worker_id} cannot claim raw source for {brief_id}")
            if state.status == "brief_published":
                assert state.brief is not None and state.source_hash is not None
                summary = state.brief.get("summary")
                if not isinstance(summary, Mapping) or not all(isinstance(value, str) for value in summary.values()):
                    raise BrokerError("brief_missing", f"published brief is malformed for {brief_id}")
                self._event("brief_served_owner_reuse", brief_id=brief_id, owner_id=worker_id, waited_ms=0)
                return SourceClaim(
                    brief_id=brief_id,
                    source_hash=state.source_hash,
                    source_content="",
                    brief_template={},
                    source_chunk_index=0,
                    source_chunk_count=0,
                    claim_kind="unchanged_reuse",
                    reused_summary={str(field): value for field, value in summary.items()},
                )
            if state.status != "unclaimed":
                raise BrokerError("already_claimed", f"raw source was already claimed for {brief_id}")
            if state.refresh_kind == "diff_refresh":
                state.status = "owner_diff"
            else:
                state.status = "owner_raw"  # Set before the first await; peers cannot become owners.

        if state.status == "owner_diff":
            assert state.source_hash is not None and state.source_content is not None
            assert state.prior_summary is not None and state.git_diff is not None
            self._event(
                "brief_refresh_diff",
                brief_id=brief_id,
                owner_id=worker_id,
                workflow_id=self.workflow_id,
                stage_id=self.stage_id,
                source_hash=state.source_hash,
                parent_source_hash=state.parent_source_hash,
                diff_size=len(state.git_diff.encode("utf-8")),
            )
            return SourceClaim(
                brief_id=brief_id,
                source_hash=state.source_hash,
                source_content="",
                brief_template=brief_contract(state.entry, state.source_content),
                source_chunk_index=0,
                source_chunk_count=0,
                claim_kind="diff_refresh",
                prior_summary=state.prior_summary,
                prior_source_hash=state.parent_source_hash,
                git_diff=state.git_diff,
            )

        try:
            source = await asyncio.to_thread(self._source_path(state.entry).read_text, encoding="utf-8")
        except OSError as error:
            async with state.lock:
                state.status = "brief_missing"
                state.done.set()
            self._event("brief_missing", brief_id=brief_id, owner_id=worker_id, error=str(error))
            raise BrokerError("brief_missing", f"owner source is unavailable for {brief_id}") from error

        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        owner_view, owner_view_kind = self._owner_source_view(state.entry, source)
        chunks = self._source_chunks(owner_view)
        async with state.lock:
            state.source_content = source
            state.source_hash = source_hash
            state.source_chunks = chunks
            state.delivered_chunk_count = 1
        event = "brief_refresh_invalidated" if state.refresh_kind == "invalidated_raw" else "source_claim_raw"
        self._event(
            event,
            brief_id=brief_id,
            owner_id=worker_id,
            source_hash=source_hash,
            raw_size=len(source.encode("utf-8")),
            owner_view_size=len(owner_view.encode("utf-8")),
            owner_view_kind=owner_view_kind,
            source_chunk_count=len(chunks),
            workflow_id=self.workflow_id,
            stage_id=self.stage_id,
            parent_source_hash=state.parent_source_hash,
            invalidation_reason=state.invalidation_reason,
        )
        if event != "source_claim_raw":
            self._event(
                "source_claim_raw",
                brief_id=brief_id,
                owner_id=worker_id,
                source_hash=source_hash,
                raw_size=len(source.encode("utf-8")),
                owner_view_size=len(owner_view.encode("utf-8")),
                owner_view_kind=owner_view_kind,
                source_chunk_count=len(chunks),
                workflow_id=self.workflow_id,
                stage_id=self.stage_id,
                invalidation_reason=state.invalidation_reason,
            )
        return SourceClaim(
            brief_id=brief_id,
            source_hash=source_hash,
            source_content=chunks[0],
            brief_template=brief_contract(state.entry, source),
            source_chunk_index=0,
            source_chunk_count=len(chunks),
            claim_kind=state.refresh_kind if state.refresh_kind == "invalidated_raw" else "raw",
            prior_source_hash=state.parent_source_hash,
            invalidation_reason=state.invalidation_reason,
        )

    async def read_source_chunk(self, brief_id: str, worker_id: str, chunk_index: int) -> dict[str, Any]:
        """Deliver the next owner-only raw chunk from an already claimed source."""

        state = self._owner_claim_state(brief_id, worker_id, "owner_chunk_id_reconciled")
        brief_id = state.entry.brief_id
        async with state.lock:
            if worker_id != state.entry.source_owner:
                raise BrokerError("not_source_owner", f"{worker_id} cannot read raw source for {brief_id}")
            if state.status != "owner_raw" or not state.source_chunks:
                raise BrokerError("owner_not_ready", f"source owner has not claimed {brief_id}")
            if not isinstance(chunk_index, int) or isinstance(chunk_index, bool):
                raise BrokerError("bad_request", "chunk_index must be an integer")
            if chunk_index != state.delivered_chunk_count or chunk_index >= len(state.source_chunks):
                raise BrokerError("chunk_order", f"next required chunk for {brief_id} is {state.delivered_chunk_count}")
            chunk = state.source_chunks[chunk_index]
            state.delivered_chunk_count += 1
            source_hash = state.source_hash
        self._event(
            "source_chunk_raw",
            brief_id=brief_id,
            owner_id=worker_id,
            source_hash=source_hash,
            chunk_index=chunk_index,
            chunk_count=len(state.source_chunks),
            chunk_size=len(chunk.encode("utf-8")),
        )
        return {
            "brief_id": brief_id,
            "source_hash": source_hash,
            "source_content": chunk,
            "source_chunk_index": chunk_index,
            "source_chunk_count": len(state.source_chunks),
        }

    async def _persist(self, state: _BriefState, source_hash: str, brief: ValidatedFileBrief) -> None:
        if self.state_dir is None:
            return
        if self.workflow_id is not None:
            await asyncio.to_thread(self._persist_lineage_sync, state, source_hash, brief)
            return
        entry = state.entry
        schema_version = str(brief["schema_version"])
        schema_key = schema_version.replace("/", "_")
        target = self.state_dir / f"{source_hash}-{entry.requirements_hash}-{schema_key}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        payload = {"brief_id": entry.brief_id, "source_hash": source_hash, "brief": brief}
        await asyncio.to_thread(temporary.write_text, json.dumps(payload, sort_keys=True), encoding="utf-8")
        await asyncio.to_thread(os.replace, temporary, target)

    @staticmethod
    def _peer_brief(brief_id: str, brief: ValidatedFileBrief) -> dict[str, Any]:
        """Return only the concise source summary to a peer, never audit metadata."""

        return peer_brief_payload(brief_id, cast(Mapping[str, object], brief["summary"]))

    async def publish_file_brief(
        self, brief_id: str, worker_id: str, source_hash: str, brief: object
    ) -> None:
        """Validate, persist, and release a source-owner Luna's complete brief."""

        state = self._owner_claim_state(brief_id, worker_id, "owner_publish_id_reconciled")
        brief_id = state.entry.brief_id
        async with state.lock:
            if worker_id != state.entry.source_owner:
                raise BrokerError("not_source_owner", f"{worker_id} cannot publish {brief_id}")
            if state.status not in {"owner_raw", "owner_diff"} or state.source_content is None or state.source_hash is None:
                raise BrokerError("owner_not_ready", f"source owner has not claimed {brief_id}")
            if state.status == "owner_raw" and state.delivered_chunk_count != len(state.source_chunks):
                self._event(
                    "brief_repair_requested",
                    brief_id=brief_id,
                    owner_id=worker_id,
                    reason="source_chunks_incomplete",
                )
                raise BrokerError("brief_incomplete", f"owner must read all source chunks for {brief_id}")
            if source_hash != state.source_hash:
                # The broker owns the claimed revision already.  Requiring a
                # Luna to retype its opaque hash makes a valid one-brief repair
                # fail for transport noise, while accepting the canonical
                # broker value cannot widen source access or weaken binding.
                self._event("source_hash_normalized", brief_id=brief_id, owner_id=worker_id)
                source_hash = state.source_hash
            source = state.source_content

        try:
            validated = validate_file_brief(brief, state.entry, source)
            await self._persist(state, source_hash, validated)
        except BriefValidationError as error:
            # The owner still holds the sole raw body and can repair its first
            # schema attempt. Peers keep waiting rather than falling back raw.
            # Preserve the exact validation reason at the MCP-visible surface
            # so the owner repairs the actual defect (a missing literal fact or
            # an over-budget summary) instead of guessing through repeated
            # speculative publish turns.
            self._event("brief_repair_requested", brief_id=brief_id, owner_id=worker_id, reason=str(error))
            raise BrokerError("brief_incomplete", str(error)) from error
        except OSError as error:
            self._event("brief_repair_requested", brief_id=brief_id, owner_id=worker_id, reason=str(error))
            raise BrokerError("brief_incomplete", f"brief validation failed for {brief_id}") from error

        async with state.lock:
            state.brief = validated
            state.status = "brief_published"
            state.done.set()
        self._event(
            "brief_published",
            brief_id=brief_id,
            owner_id=worker_id,
            source_hash=source_hash,
            raw_size=len(source.encode("utf-8")),
            owner_view_size=sum(len(chunk.encode("utf-8")) for chunk in state.source_chunks),
            brief_size=peer_brief_size(brief_id, cast(Mapping[str, object], validated["summary"])),
            max_peer_payload_bytes=brief_contract(state.entry, source)["max_peer_payload_bytes"],
            requirements_hash=state.entry.requirements_hash,
            refresh_kind=state.refresh_kind,
            workflow_id=self.workflow_id,
            stage_id=self.stage_id,
            parent_source_hash=state.parent_source_hash,
        )

    async def get_file_brief(self, brief_id: str, worker_id: str) -> dict[str, Any]:
        """Return a complete brief to a declared reader, never raw source.

        A later stage's deterministic owner may itself be a consumer of an
        already-published unchanged revision.  It must consume that existing
        brief rather than treating the deliberate no-second-raw-claim rule as
        a failure.  The response remains the same compact peer payload.
        """

        state = self._peer_state(brief_id, worker_id)
        brief_id = state.entry.brief_id
        if worker_id not in state.entry.peer_workers and worker_id != state.entry.source_owner:
            raise BrokerError("not_brief_reader", f"{worker_id} is not an allowed reader for {brief_id}")
        started = int(time.time() * 1000)
        async with state.lock:
            if state.status == "brief_published":
                assert state.brief is not None
                self._event(
                    "brief_served_owner_reuse" if worker_id == state.entry.source_owner else "brief_served",
                    brief_id=brief_id,
                    **({"owner_id": worker_id} if worker_id == state.entry.source_owner else {"peer_id": worker_id}),
                    waited_ms=0,
                )
                return self._peer_brief(brief_id, state.brief)
            if state.status == "brief_missing":
                raise BrokerError(state.status, f"{state.status} for {brief_id}")
        self._event("brief_wait", brief_id=brief_id, peer_id=worker_id)
        try:
            await asyncio.wait_for(state.done.wait(), timeout=self.wait_timeout_ms / 1000)
        except TimeoutError as error:
            self._event("brief_missing", brief_id=brief_id, peer_id=worker_id, reason="timeout")
            raise BrokerError("brief_missing", f"timed out waiting for {brief_id}") from error
        async with state.lock:
            if state.status != "brief_published" or state.brief is None:
                raise BrokerError(state.status, f"{state.status} for {brief_id}")
            self._event(
                "brief_served_owner_reuse" if worker_id == state.entry.source_owner else "brief_served",
                brief_id=brief_id,
                **({"owner_id": worker_id} if worker_id == state.entry.source_owner else {"peer_id": worker_id}),
                waited_ms=int(time.time() * 1000) - started,
            )
            return self._peer_brief(brief_id, state.brief)

    async def get_file_briefs(self, brief_ids: list[str], worker_id: str) -> dict[str, dict[str, Any]]:
        """Return every declared peer brief in one model-visible retrieval call.

        The broker waits independently per source; batching only removes repeated
        Luna tool ceremony and never widens raw-source access.
        """

        if not brief_ids or any(not isinstance(brief_id, str) or not brief_id for brief_id in brief_ids):
            raise BrokerError("bad_request", "brief_ids must be a non-empty list of strings")
        if len(set(brief_ids)) != len(brief_ids):
            raise BrokerError("bad_request", "brief_ids must not contain duplicates")
        replies = await asyncio.gather(*(self.get_file_brief(brief_id, worker_id) for brief_id in brief_ids))
        return {reply["brief_id"]: reply for reply in replies}

    async def get_ready_file_briefs(self, brief_ids: list[str], worker_id: str) -> dict[str, dict[str, Any]]:
        """Return only already-published authorized briefs without waiting.

        The harness uses this at dispatch time to attach immutable, broker-made
        source briefs to a worker packet. A new owner claim is simply omitted,
        so this operation cannot turn a peer into a raw reader or delay launch.
        """

        if not brief_ids or any(not isinstance(brief_id, str) or not brief_id for brief_id in brief_ids):
            raise BrokerError("bad_request", "brief_ids must be a non-empty list of strings")
        if len(set(brief_ids)) != len(brief_ids):
            raise BrokerError("bad_request", "brief_ids must not contain duplicates")
        replies: dict[str, dict[str, Any]] = {}
        for supplied_id in brief_ids:
            state = self._peer_state(supplied_id, worker_id)
            brief_id = state.entry.brief_id
            if worker_id not in state.entry.peer_workers and worker_id != state.entry.source_owner:
                raise BrokerError("not_brief_reader", f"{worker_id} is not an allowed reader for {brief_id}")
            async with state.lock:
                if state.status != "brief_published" or state.brief is None:
                    continue
                replies[brief_id] = self._peer_brief(brief_id, state.brief)
            self._event(
                "brief_prefetched_dispatch",
                brief_id=brief_id,
                **({"owner_id": worker_id} if worker_id == state.entry.source_owner else {"peer_id": worker_id}),
            )
        return replies

    async def get_ready_worker_briefs(
        self, requests: list[dict[str, object]], worker_id: str
    ) -> dict[str, dict[str, Any]]:
        """Return plan-scoped facts from already validated owner briefs.

        The complete five-field brief remains the retained broker artifact.
        A sealed worker needs only the source facts its particular plan uses;
        the broker checks those facts are present in the owner's validated
        brief before returning this compact projection.  This is neither a
        controller digest nor a peer raw-source route.
        """

        if not requests:
            raise BrokerError("bad_request", "requests must be a non-empty list")
        replies: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for request in requests:
            if not isinstance(request, dict):
                raise BrokerError("bad_request", "each worker brief request must be an object")
            supplied_id = request.get("brief_id")
            facts = request.get("required_facts")
            if not isinstance(supplied_id, str) or not supplied_id:
                raise BrokerError("bad_request", "brief_id must be a non-empty string")
            if supplied_id in seen:
                raise BrokerError("bad_request", "worker brief requests must not repeat brief_id")
            seen.add(supplied_id)
            if not isinstance(facts, list) or not facts or not all(isinstance(fact, str) and fact for fact in facts):
                raise BrokerError("bad_request", "required_facts must be a non-empty list of strings")
            state = self._peer_state(supplied_id, worker_id)
            brief_id = state.entry.brief_id
            if worker_id not in state.entry.peer_workers and worker_id != state.entry.source_owner:
                raise BrokerError("not_brief_reader", f"{worker_id} is not an allowed reader for {brief_id}")
            async with state.lock:
                if state.status != "brief_published" or state.brief is None:
                    continue
                summary = cast(Mapping[str, str], state.brief["summary"])
                fact_entry = replace(state.entry, required_facts=tuple(facts))
                if not self._source_fact_coverage("\n".join(summary.values()), fact_entry):
                    raise BrokerError("brief_incomplete", f"validated brief does not cover requested facts for {brief_id}")
            replies[brief_id] = {"brief_id": brief_id, "facts": facts}
            self._event(
                "brief_prefetched_worker_projection",
                brief_id=brief_id,
                worker_id=worker_id,
                fact_count=len(facts),
            )
        return replies


class SharedBrokerServer:
    """Small JSON-lines server shared by the four independent worker bridges."""

    def __init__(self, broker: SharedBriefBroker, *, control_token: str | None = None) -> None:
        self.broker = broker
        self.control_token = control_token
        self._server: asyncio.AbstractServer | None = None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._handle, host, port)
        socket = self._server.sockets[0]
        return socket.getsockname()[0], socket.getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                request_id: object = None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise BrokerError("bad_request", "request must be an object")
                    request_id = request.get("id")
                    params = request.get("params", {})
                    if not isinstance(params, dict):
                        raise BrokerError("bad_request", "params must be an object")
                    operation = request.get("operation")
                    if operation == "bridge_identity":
                        result = self.broker.bridge_identity(
                            params["worker_id"], params["bridge_pid"], params["broker_endpoint"]
                        )
                    elif operation == "claim_source":
                        claim = await self.broker.claim_source(params["brief_id"], params["worker_id"])
                        result: object = {
                            "brief_id": claim.brief_id,
                            "source_hash": claim.source_hash,
                            "source_content": claim.source_content,
                            "brief_template": claim.brief_template,
                            "source_chunk_index": claim.source_chunk_index,
                            "source_chunk_count": claim.source_chunk_count,
                            "claim_kind": claim.claim_kind,
                            "prior_summary": claim.prior_summary,
                            "reused_summary": claim.reused_summary,
                            "prior_source_hash": claim.prior_source_hash,
                            "git_diff": claim.git_diff,
                            "invalidation_reason": claim.invalidation_reason,
                        }
                    elif operation == "read_source_chunk":
                        result = await self.broker.read_source_chunk(
                            params["brief_id"], params["worker_id"], params["chunk_index"]
                        )
                    elif operation == "publish_file_brief":
                        await self.broker.publish_file_brief(
                            params["brief_id"], params["worker_id"], params["source_hash"], params["brief"]
                        )
                        result = {"published": True}
                    elif operation == "get_file_brief":
                        result = await self.broker.get_file_brief(params["brief_id"], params["worker_id"])
                    elif operation == "get_file_briefs":
                        result = await self.broker.get_file_briefs(params["brief_ids"], params["worker_id"])
                    elif operation == "get_ready_file_briefs":
                        result = await self.broker.get_ready_file_briefs(params["brief_ids"], params["worker_id"])
                    elif operation == "get_ready_worker_briefs":
                        result = await self.broker.get_ready_worker_briefs(params["requests"], params["worker_id"])
                    elif operation == "install_stage":
                        if not self.control_token or params.get("control_token") != self.control_token:
                            raise BrokerError("control_denied", "stage installation requires the harness control token")
                        from contextmesh.mcp.broker_service import load_ledger

                        ledger_path = params.get("ledger_path")
                        if not isinstance(ledger_path, str) or not ledger_path:
                            raise BrokerError("bad_request", "ledger_path must be a non-empty string")
                        self.broker.advance_stage(
                            load_ledger(ledger_path),
                            stage_id=params["stage_id"],
                            stage_commit=params["stage_commit"],
                            parent_stage_commit=params["parent_stage_commit"],
                            branch_lineage=params.get("branch_lineage"),
                        )
                        result = {"installed": True, "stage_id": params["stage_id"]}
                    else:
                        raise BrokerError("bad_request", f"unknown operation {operation}")
                    response = {"id": request_id, "ok": True, "result": result}
                except (BrokerError, KeyError, TypeError, json.JSONDecodeError) as error:
                    code = error.code if isinstance(error, BrokerError) else "bad_request"
                    response = {"id": request_id, "ok": False, "error": {"code": code, "message": str(error)}}
                writer.write(json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@dataclass(frozen=True, slots=True)
class SharedBrokerClient:
    """A worker-local bridge client; each call uses the one arm-local broker."""

    host: str
    port: int

    async def _request(self, operation: str, **params: object) -> Any:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            writer.write(
                json.dumps({"id": 1, "operation": operation, "params": params}, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
            await writer.drain()
            response = json.loads((await reader.readline()).decode("utf-8"))
        finally:
            writer.close()
            await writer.wait_closed()
        if not response.get("ok"):
            error = response["error"]
            raise BrokerError(error["code"], error["message"])
        return response["result"]

    async def claim_source(self, brief_id: str, worker_id: str) -> dict[str, Any]:
        return await self._request("claim_source", brief_id=brief_id, worker_id=worker_id)

    async def bridge_identity(self, worker_id: str, bridge_pid: int, endpoint: str) -> dict[str, Any]:
        return await self._request(
            "bridge_identity",
            worker_id=worker_id,
            bridge_pid=bridge_pid,
            broker_endpoint=endpoint,
        )

    async def read_source_chunk(self, brief_id: str, worker_id: str, chunk_index: int) -> dict[str, Any]:
        return await self._request(
            "read_source_chunk", brief_id=brief_id, worker_id=worker_id, chunk_index=chunk_index
        )

    async def publish_file_brief(self, brief_id: str, worker_id: str, source_hash: str, brief: object) -> None:
        await self._request(
            "publish_file_brief", brief_id=brief_id, worker_id=worker_id, source_hash=source_hash, brief=brief
        )

    async def get_file_brief(self, brief_id: str, worker_id: str) -> dict[str, Any]:
        return await self._request("get_file_brief", brief_id=brief_id, worker_id=worker_id)

    async def get_file_briefs(self, brief_ids: list[str], worker_id: str) -> dict[str, dict[str, Any]]:
        return await self._request("get_file_briefs", brief_ids=brief_ids, worker_id=worker_id)

    async def get_ready_file_briefs(self, brief_ids: list[str], worker_id: str) -> dict[str, dict[str, Any]]:
        return await self._request("get_ready_file_briefs", brief_ids=brief_ids, worker_id=worker_id)

    async def get_ready_worker_briefs(
        self, requests: list[dict[str, object]], worker_id: str
    ) -> dict[str, dict[str, Any]]:
        return await self._request("get_ready_worker_briefs", requests=requests, worker_id=worker_id)

    async def install_stage(
        self,
        *,
        control_token: str,
        ledger_path: str,
        stage_id: str,
        stage_commit: str,
        parent_stage_commit: str,
        branch_lineage: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "install_stage",
            control_token=control_token,
            ledger_path=ledger_path,
            stage_id=stage_id,
            stage_commit=stage_commit,
            parent_stage_commit=parent_stage_commit,
            branch_lineage=branch_lineage,
        )


__all__ = [
    "BrokerError",
    "MAX_SOURCE_CHUNK_BYTES",
    "MAX_PLAN_SCOPED_SOURCE_VIEW_BYTES",
    "SharedBriefBroker",
    "SharedBrokerClient",
    "SharedBrokerServer",
    "SourceClaim",
]
