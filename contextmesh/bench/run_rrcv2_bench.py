"""Canonical five-arm RRCv2 functional benchmark runner.

The runner consumes the frozen v6 task/oracle/order core and the reviewed v10
functional-only overlay.  Live rows are never passed to the historical positive
savings analyzer: they report exact provider usage, requested-profile estimates,
and oracle quality only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rrc.contract import (
    ArmMode,
    Completion,
    Config,
    InlineTaskInputV1,
    ModelPort,
    ModelRole,
    NullRetrieval,
    RunContext,
    SolveOutcome,
    StructuralShapeV1,
    TargetPreimageV1,
    Task,
    TaskEnvelopeV1,
    canonical_json_bytes,
    parse_task_envelope,
    reopen_task_inputs,
    seal_task_input,
    task_envelope_bytes,
)
from rrc.dispatch_permit import (
    PRODUCT_STAGE_MATRIX,
    AuthorityRef,
    ProductAttemptDispatchRequestV5,
    ProductJournalCursorV2,
    authorize_product,
    product_call_id,
)
from rrc.economic_authority import validate_economic_binding_overlay_v10
from rrc.journal import AttemptHandle, SQLiteRRCRepository, benchmark_operation_key
from rrc.model import CodexModel
from rrc.pipeline.solve import solve
from rrc.retrieval import SQLiteHybridRetrieval

ARMS = ("baseline", "cheap_alone", "cascade", "rrc_cold", "rrc_warm")
REPLICATES = ("r01", "r02", "r03", "r04")
NO_SAVINGS_VERDICT = "no demonstrated RRCv2 savings: effective identity or tier unattested"
OVERLAY_SHA256 = "d6a8717d706dec5af8c57bbed2f09d0312d3d6008249c3a54c05f180efbdeea5"
WORKLOAD_SHA256 = "dceacfea8c1598912fb3f893c0d38b83e264569886e50f68e0406e96f2785b0d"
ORACLES_SHA256 = "994ec5b353c4bd25a8f1534b316e389d1663a55498adf3e459cf34f96d63ca20"
ANALYZER_SHA256 = "edd0d4bd8754921b8c599b8e296bb1982e2946d34be70065c84610a826d424ff"
_RUN_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_MAX_JSON = 16 * 1024 * 1024
_RATE_NANO = {
    "gpt-5.5": (12_500, 1_250, 75_000),
    "gpt-5.6-luna": (400, 40, 2_400),
}
_ARM_MODE = {
    "baseline": ArmMode.BASELINE,
    "cheap_alone": ArmMode.CHEAP_ALONE,
    "cascade": ArmMode.CASCADE,
    "rrc_cold": ArmMode.COLD,
    "rrc_warm": ArmMode.WARM,
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _read_regular(path: Path, *, cap: int = _MAX_JSON, mode: int | None = None) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_size > cap:
        raise ValueError(f"authority is not a bounded regular file: {path}")
    if mode is not None and stat.S_IMODE(before.st_mode) != mode:
        raise ValueError(f"authority mode differs: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ValueError(f"authority identity changed: {path}")
        raw = bytearray()
        while len(raw) <= cap:
            block = os.read(descriptor, min(65_536, cap + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
    finally:
        os.close(descriptor)
    after = path.lstat()
    if len(raw) > cap or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise ValueError(f"authority changed or exceeded its cap: {path}")
    return bytes(raw)


def _json_object(raw: bytes, *, name: str, newline: bool = True) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not strict JSON") from exc
    expected = _canonical_line(value) if newline else canonical_json_bytes(value)
    if not isinstance(value, dict) or raw != expected:
        raise ValueError(f"{name} is not canonical JSON")
    return cast(dict[str, Any], value)


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _ref(path: Path, *, mode: int | None = 0o600) -> AuthorityRef:
    raw = _read_regular(path, mode=mode)
    return AuthorityRef(path=path, sha256=_sha(raw), bytes=len(raw))


def load_authorities(repo: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Reopen the complete v10 authority graph before any model call."""

    repo = repo.resolve()
    generated = repo / ".generated/state/rrcv2-convergence"
    workload_path = repo / "contextmesh/bench/rrcv2_workload.json"
    oracles_path = repo / "contextmesh/bench/rrcv2_oracles.json"
    analyzer_path = repo / "contextmesh/bench/rrcv2_analyzer.py"
    overlay_path = generated / "economic/economic-binding-overlay.v10.json"
    workload_raw = _read_regular(workload_path)
    oracles_raw = _read_regular(oracles_path)
    analyzer_raw = _read_regular(analyzer_path)
    overlay_raw = _read_regular(overlay_path, mode=0o600)
    if (
        _sha(workload_raw) != WORKLOAD_SHA256
        or _sha(oracles_raw) != ORACLES_SHA256
        or _sha(analyzer_raw) != ANALYZER_SHA256
        or _sha(overlay_raw) != OVERLAY_SHA256
    ):
        raise ValueError("RRCv2 workload, oracle, analyzer, or overlay authority drifted")
    workload = _json_object(workload_raw, name="workload")
    oracles = _json_object(oracles_raw, name="oracles")
    overlay = _json_object(overlay_raw, name="overlay")
    validate_economic_binding_overlay_v10(
        overlay,
        workload_core_ref=_ref(generated / "economic/workload-core.v6.json"),
        source_workload_ref=_ref(workload_path, mode=None),
        source_oracles_ref=_ref(oracles_path, mode=None),
        capability_manifest_ref=_ref(generated / "capability/capability-manifest.v1.json"),
        capability_summary_ref=_ref(generated / "capability/capability-summary.v1.json"),
        capability_evidence_inventory_ref=_ref(
            generated / "verify/capability-evidence-inventory.v1.json"
        ),
        sandbox_evidence_v2_ref=_ref(generated / "verify/sandbox-evidence.v2.json"),
        setup_accounting_ref=_ref(generated / "verify/setup-accounting.v1.json"),
        setup_accounting_v2_ref=_ref(generated / "verify/setup-accounting.v2.json"),
        worker_context_attestation_ref=_ref(
            generated / "verify/native-worker-context-attestation.v1.json"
        ),
        root_rollout_ref=_ref(
            generated / "capability/calls/cap-08-root-strong-medium-native/rollout.jsonl"
        ),
        worker_rollout_ref=_ref(
            generated / "capability/calls/cap-09-worker-small-low-native/rollout.jsonl"
        ),
        root_environment_ref=_ref(
            generated / "capability/calls/cap-08-root-strong-medium-native/environment.json"
        ),
        worker_environment_ref=_ref(
            generated / "capability/calls/cap-09-worker-small-low-native/environment.json"
        ),
        generated_output_authority_v3_ref=_ref(
            generated / "verify/generated-output-authority.v3.json"
        ),
        generated_output_authority_v2_ref=_ref(
            generated / "verify/generated-output-authority.v2.json"
        ),
        generated_output_authority_v1_ref=_ref(generated / "generated-output-authority.v1.json"),
        abandoned_manifest_ref=_ref(
            generated / "verify/aborted-capability-attempt-1/archive-manifest.json"
        ),
        redesign_evidence_ref=_ref(
            generated / "capability/explore-strong/redesign-evidence.v2.json"
        ),
        analyzer_ref=_ref(analyzer_path, mode=None),
    )
    return workload, oracles, overlay


def _product_authority_refs(repo: Path) -> dict[str, AuthorityRef]:
    """Resolve the complete reviewed product-permit authority set once per run."""

    repo = repo.resolve()
    generated = repo / ".generated/state/rrcv2-convergence"
    calls = generated / "capability/calls"
    return {
        "workload_core": _ref(generated / "economic/workload-core.v6.json"),
        "overlay": _ref(generated / "economic/economic-binding-overlay.v10.json"),
        "source_workload": _ref(repo / "contextmesh/bench/rrcv2_workload.json", mode=None),
        "source_oracles": _ref(repo / "contextmesh/bench/rrcv2_oracles.json", mode=None),
        "capability_manifest": _ref(generated / "capability/capability-manifest.v1.json"),
        "capability_summary": _ref(generated / "capability/capability-summary.v1.json"),
        "capability_inventory": _ref(generated / "verify/capability-evidence-inventory.v1.json"),
        "sandbox_v2": _ref(generated / "verify/sandbox-evidence.v2.json"),
        "setup": _ref(generated / "verify/setup-accounting.v1.json"),
        "setup_v2": _ref(generated / "verify/setup-accounting.v2.json"),
        "worker_attestation": _ref(generated / "verify/native-worker-context-attestation.v1.json"),
        "root_rollout": _ref(calls / "cap-08-root-strong-medium-native/rollout.jsonl"),
        "worker_rollout": _ref(calls / "cap-09-worker-small-low-native/rollout.jsonl"),
        "root_environment": _ref(calls / "cap-08-root-strong-medium-native/environment.json"),
        "worker_environment": _ref(calls / "cap-09-worker-small-low-native/environment.json"),
        "output_v3": _ref(generated / "verify/generated-output-authority.v3.json"),
        "output_v2": _ref(generated / "verify/generated-output-authority.v2.json"),
        "output_v1": _ref(generated / "generated-output-authority.v1.json"),
        "abandoned": _ref(generated / "verify/aborted-capability-attempt-1/archive-manifest.json"),
        "redesign": _ref(generated / "capability/explore-strong/redesign-evidence.v2.json"),
        "analyzer": _ref(repo / "contextmesh/bench/rrcv2_analyzer.py", mode=None),
        "plan_seal": _ref(generated / "reviews/plan-m6-post-v20-hook-bootstrap-v2.seal.json"),
        "plan": _ref(repo / "PLAN.md", mode=None),
        "plan_transcript": _ref(generated / "reviews/plan-m6-post-v20-hook-bootstrap-v2.txt"),
        "plan_record": _ref(repo / ".generated/state/reviews/plan.toml", mode=None),
        "claim_transcript": _ref(generated / "reviews/claim-post-capability-functional-v8.txt"),
        "claim_record": _ref(repo / ".generated/state/reviews/claim-worktree.toml", mode=None),
        "config": _ref(generated / "verify/product-config-manifest.v15.json"),
        "economic_source": _ref(repo / "rrc/economic_authority.py", mode=None),
        "permit_source": _ref(repo / "rrc/dispatch_permit.py", mode=None),
        "capability_source": _ref(
            repo / "contextmesh/scripts/rrcv2_capability_matrix.py", mode=None
        ),
        "sandbox_source": _ref(repo / "contextmesh/scripts/rrcv2_sandbox_probe_v2.py", mode=None),
    }


class ProductPermittedModel:
    """Bind one task's direct provider calls to the reviewed product permit."""

    def __init__(
        self,
        delegate: ModelPort,
        *,
        repo: Path,
        refs: Mapping[str, AuthorityRef],
        task_envelope_ref: AuthorityRef,
        run_id: str,
        replicate_id: str,
        arm: str,
        cell_id: str,
    ) -> None:
        self._delegate = delegate
        self._repo = repo.resolve()
        self._refs = refs
        self._task_envelope_ref = task_envelope_ref
        self._run_id = run_id
        self._replicate_id = replicate_id
        self._arm = arm
        self._cell_id = cell_id
        self.provider = delegate.provider

    def _request(
        self,
        *,
        attempt: AttemptHandle,
        ctx: RunContext,
        branch: str,
        stage: str,
        stage_ordinal: int,
    ) -> ProductAttemptDispatchRequestV5:
        if ctx.task_id != self._task_envelope_task_id():
            raise ValueError("product model was bound to another task envelope")
        expected_ctx_arm = {"rrc_cold": "cold", "rrc_warm": "warm"}.get(self._arm, self._arm)
        if ctx.arm != expected_ctx_arm:
            raise ValueError("product model was bound to another arm")
        try:
            surface, cursor = PRODUCT_STAGE_MATRIX[
                ("direct", self._arm, branch, stage, stage_ordinal)
            ]
        except KeyError as exc:
            raise ValueError("provider stage is absent from the product dispatch matrix") from exc
        request = ProductAttemptDispatchRequestV5(
            call_id="",
            scope="experiment",
            controller="direct",
            task_id=ctx.task_id,
            task_envelope_sha256=self._task_envelope_ref.sha256,
            root_binding_sha256=None,
            run_id=self._run_id,
            replicate_id=self._replicate_id,
            arm=self._arm,
            branch=branch,
            stage=stage,
            stage_ordinal=stage_ordinal,
            journal_cursor=cursor,
            cell_id=self._cell_id,
            attempt_id=attempt.attempt_id,
            transport="direct",
            surface_id=surface,
        )
        return replace(request, call_id=product_call_id(request))

    def _task_envelope_task_id(self) -> str:
        value = _json_object(
            _read_regular(self._task_envelope_ref.path, mode=0o600),
            name="task envelope",
            newline=False,
        )
        task = value.get("task")
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
            raise ValueError("task envelope has no task identity")
        return cast(str, task["task_id"])

    def product_call_id(
        self,
        *,
        attempt: AttemptHandle,
        ctx: RunContext,
        branch: str,
        stage: str,
        stage_ordinal: int,
    ) -> str:
        return self._request(
            attempt=attempt,
            ctx=ctx,
            branch=branch,
            stage=stage,
            stage_ordinal=stage_ordinal,
        ).call_id

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
        request = self._request(
            attempt=attempt,
            ctx=ctx,
            branch=branch,
            stage=stage,
            stage_ordinal=stage_ordinal,
        )
        if request.call_id != call_id:
            raise ValueError("product call ID changed between prepare and authorization")
        cursor = ProductJournalCursorV2(
            attempt_id=attempt.attempt_id,
            cell_id=self._cell_id,
            call_id=call_id,
            stage_ordinal=stage_ordinal,
            journal_generation=attempt.generation,
            prior_state="absent",
            root_binding_sha256=None,
            root_binding_generation=None,
        )
        refs = self._refs
        permit = authorize_product(
            request=request,
            journal_cursor=cursor,
            task_envelope_ref=self._task_envelope_ref,
            cell_attempt_binding_ref=None,
            cell_journal_authority=None,
            repo_root=self._repo,
            workload_core_ref=refs["workload_core"],
            overlay_ref=refs["overlay"],
            source_workload_ref=refs["source_workload"],
            source_oracles_ref=refs["source_oracles"],
            capability_manifest_ref=refs["capability_manifest"],
            capability_summary_ref=refs["capability_summary"],
            capability_evidence_inventory_ref=refs["capability_inventory"],
            sandbox_evidence_v2_ref=refs["sandbox_v2"],
            setup_accounting_ref=refs["setup"],
            setup_accounting_v2_ref=refs["setup_v2"],
            worker_context_attestation_ref=refs["worker_attestation"],
            root_rollout_ref=refs["root_rollout"],
            worker_rollout_ref=refs["worker_rollout"],
            root_environment_ref=refs["root_environment"],
            worker_environment_ref=refs["worker_environment"],
            generated_output_authority_v3_ref=refs["output_v3"],
            generated_output_authority_v2_ref=refs["output_v2"],
            generated_output_authority_v1_ref=refs["output_v1"],
            abandoned_manifest_ref=refs["abandoned"],
            redesign_evidence_ref=refs["redesign"],
            analyzer_ref=refs["analyzer"],
            plan_review_seal_ref=refs["plan_seal"],
            plan_ref=refs["plan"],
            plan_transcript_ref=refs["plan_transcript"],
            plan_record_ref=refs["plan_record"],
            claim_transcript_ref=refs["claim_transcript"],
            claim_record_ref=refs["claim_record"],
            config_manifest_ref=refs["config"],
            economic_authority_file_ref=refs["economic_source"],
            dispatch_permit_file_ref=refs["permit_source"],
            capability_matrix_file_ref=refs["capability_source"],
            sandbox_probe_file_ref=refs["sandbox_source"],
        )
        if permit.call_id != call_id or permit.kind != "product":
            raise ValueError("product permit does not match the provider call")

    def complete(
        self,
        role: ModelRole,
        prompt: str,
        ctx: RunContext,
        stage: str,
    ) -> Completion:
        return self._delegate.complete(role, prompt, ctx, stage)


def _task(row: Mapping[str, Any], oracle: Mapping[str, Any]) -> Task:
    shape = cast(Mapping[str, Any], row["shape"])
    if oracle.get("task_id") != row.get("task_id") or oracle.get("oracle_sha256") != row.get(
        "oracle_sha256"
    ):
        raise ValueError("oracle row differs from its task authority")
    slot_values = cast(Mapping[str, str], row["slot_values"])
    return Task(
        task_id=cast(str, row["task_id"]),
        text=cast(str, row["task_text"]),
        oracle_tests=tuple(cast(Sequence[str], oracle["hidden_oracle_tests"])),
        family=cast(str, row["family"]),
        artifact_path=cast(str, row["artifact_path"]),
        public_tests=tuple(cast(Sequence[str], row["public_tests"])),
        searchable_public=False,
        verification_profile=cast(str, row["profile"]),
        primary=cast(str, row["primary"]),
        shape=StructuralShapeV1(
            tuple(cast(Sequence[str], shape["arg_types"])),
            cast(int, shape["arity"]),
            tuple(cast(Sequence[str], shape["fields"])),
        ),
        slot_values=tuple(sorted(slot_values.items())),
    )


def _sealed_envelope(*, task: Task, starter_source: str, task_root: Path) -> TaskEnvelopeV1:
    task_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    envelope_path = task_root.parent / f"{task_root.name}.envelope.v1.json"
    if not task_root.exists() and not envelope_path.exists():
        envelope = seal_task_input(
            InlineTaskInputV1(task, starter_source, TargetPreimageV1.none()),
            input_root=task_root,
        )
        _atomic_write(envelope_path, task_envelope_bytes(envelope))
        return envelope
    if not task_root.exists() or not envelope_path.exists():
        raise ValueError("partial sealed task input cannot be resumed")
    raw = _read_regular(envelope_path, cap=2 * 1024 * 1024, mode=0o600)
    envelope = parse_task_envelope(raw)
    object.__setattr__(envelope, "_input_root", task_root)
    materials = reopen_task_inputs(envelope)
    if (
        envelope.task.task_id != task.task_id
        or materials.source != starter_source
        or materials.public_tests != task.public_tests
        or materials.oracle_tests != task.oracle_tests
        or task_envelope_bytes(envelope) != raw
    ):
        raise ValueError("resumed task input differs from the frozen authority")
    return envelope


def owner_scope(*, run_id: str, replicate_id: str, arm: str) -> str:
    raw = canonical_json_bytes(
        {
            "arm": arm,
            "kind": "benchmark-owner",
            "replicate_id": replicate_id,
            "run_id": run_id,
            "v": 1,
        }
    )
    return "run-" + _sha(raw)[:32]


def _token_metrics(outcome: SolveOutcome) -> dict[str, int]:
    return {
        "input_tokens": sum(row.input_tokens for row in outcome.cost_events),
        "cached_input_tokens": sum(row.cached_input_tokens for row in outcome.cost_events),
        "uncached_input_tokens": sum(
            row.input_tokens - row.cached_input_tokens for row in outcome.cost_events
        ),
        "output_tokens": sum(row.output_tokens for row in outcome.cost_events),
        "reasoning_output_tokens": sum(row.reasoning_output_tokens for row in outcome.cost_events),
        "provider_total_tokens": sum(row.provider_total_tokens for row in outcome.cost_events),
    }


def _estimates(outcome: SolveOutcome) -> tuple[int, int]:
    request_priced = 0
    cache_neutral = 0
    for event in outcome.cost_events:
        try:
            input_rate, cached_rate, output_rate = _RATE_NANO[event.requested_model]
        except KeyError as exc:
            raise ValueError("event requested an unpriced model") from exc
        uncached = event.input_tokens - event.cached_input_tokens
        request_priced += (
            uncached * input_rate
            + event.cached_input_tokens * cached_rate
            + event.output_tokens * output_rate
        )
        cache_neutral += event.input_tokens * input_rate + event.output_tokens * output_rate
    return request_priced, cache_neutral


def _task_result(
    *,
    outcome: SolveOutcome,
    run_id: str,
    replicate_id: str,
    arm: str,
    position: int,
) -> dict[str, object]:
    metrics = _token_metrics(outcome)
    request_priced, cache_neutral = _estimates(outcome)
    scored = outcome.oracle_status in {"passed", "failed"}
    return {
        "v": 10,
        "kind": "rrcv2_functional_task",
        "run_id": run_id,
        "replicate_id": replicate_id,
        "arm": arm,
        "position": position,
        "task_id": outcome.task_id,
        "branch": outcome.branch.value,
        "public_accepted": outcome.passed,
        "oracle_status": outcome.oracle_status,
        "hidden_oracle_passed": outcome.pass_at_1 is True,
        "semantic_quality": {"scored": scored, "passed": outcome.pass_at_1 is True},
        "infrastructure_valid": scored,
        "repairs": outcome.repairs,
        "escalated": outcome.escalated,
        "token_metrics": metrics,
        "request_priced_estimate_nano": request_priced,
        "cache_neutral_sensitivity_nano": cache_neutral,
        "provider_events": [event.as_json() for event in outcome.cost_events],
    }


def _sum_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "provider_total_tokens",
    )
    return {
        field: sum(cast(int, cast(Mapping[str, Any], row["token_metrics"])[field]) for row in rows)
        for field in fields
    }


def run_cell(
    *,
    repo: Path,
    output_root: Path,
    run_id: str,
    replicate_id: str,
    arm: str,
    model: ModelPort,
    workload: Mapping[str, Any] | None = None,
    oracles: Mapping[str, Any] | None = None,
    overlay: Mapping[str, Any] | None = None,
    product_authority_refs: Mapping[str, AuthorityRef] | None = None,
) -> dict[str, Any]:
    """Run or resume one isolated 30-task cell and seal its functional evidence."""

    if _RUN_ID.fullmatch(run_id) is None or replicate_id not in REPLICATES or arm not in ARMS:
        raise ValueError("invalid benchmark cell identity")
    if workload is None or oracles is None or overlay is None:
        loaded_workload, loaded_oracles, loaded_overlay = load_authorities(repo)
        workload = loaded_workload
        oracles = loaded_oracles
        overlay = loaded_overlay
    cell_root = output_root / "cells" / replicate_id / arm
    summary_path = cell_root / "summary.v10.json"
    if summary_path.exists():
        summary = _json_object(_read_regular(summary_path, mode=0o600), name="cell summary")
        if (
            summary.get("run_id") != run_id
            or summary.get("replicate_id") != replicate_id
            or summary.get("arm") != arm
            or summary.get("overlay_sha256") != OVERLAY_SHA256
        ):
            raise ValueError("sealed cell summary belongs to another run")
        return summary
    cell_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    order = cast(Mapping[str, Sequence[Mapping[str, Any]]], workload["orders"])[replicate_id]
    task_rows = {
        cast(str, row["task_id"]): row
        for row in cast(Sequence[Mapping[str, Any]], workload["tasks"])
    }
    oracle_rows = {
        cast(str, row["task_id"]): row
        for row in cast(Sequence[Mapping[str, Any]], oracles["tasks"])
    }
    rows: list[dict[str, object]] = []
    scope = owner_scope(run_id=run_id, replicate_id=replicate_id, arm=arm)
    config = Config(scope)
    with SQLiteRRCRepository(cell_root / "state.sqlite3") as repository:
        retrieval = SQLiteHybridRetrieval(repository) if arm == "rrc_warm" else NullRetrieval()
        for position, order_row in enumerate(order, 1):
            task_id = cast(str, order_row["task_id"])
            manifest_task = task_rows[task_id]
            task = _task(manifest_task, oracle_rows[task_id])
            task_root = cell_root / "inputs" / f"{position:02d}-{task_id}"
            envelope = _sealed_envelope(
                task=task,
                starter_source=cast(str, manifest_task["starter_source"]),
                task_root=task_root,
            )
            task_model: ModelPort = model
            if product_authority_refs is not None:
                envelope_ref = _ref(task_root.parent / f"{task_root.name}.envelope.v1.json")
                cell_identity = (
                    "cell-"
                    + _sha(
                        canonical_json_bytes(
                            {
                                "arm": arm,
                                "kind": "rrcv2-benchmark-cell",
                                "replicate_id": replicate_id,
                                "run_id": run_id,
                                "v": 1,
                            }
                        )
                    )[:32]
                )
                task_model = ProductPermittedModel(
                    model,
                    repo=repo,
                    refs=product_authority_refs,
                    task_envelope_ref=envelope_ref,
                    run_id=run_id,
                    replicate_id=replicate_id,
                    arm=arm,
                    cell_id=cell_identity,
                )
            outcome = solve(
                envelope,
                mode=_ARM_MODE[arm],
                model=task_model,
                retrieval=retrieval,
                cfg=config,
                journal=repository,
                acceptance=repository,
                operation_key=benchmark_operation_key(
                    run_id=run_id,
                    replicate_id=replicate_id,
                    arm=arm,
                    task_id=task_id,
                ),
            )
            rows.append(
                _task_result(
                    outcome=outcome,
                    run_id=run_id,
                    replicate_id=replicate_id,
                    arm=arm,
                    position=position,
                )
            )
    hit_count = sum(
        row["branch"] == "reuse"
        and row["escalated"] is False
        and row["public_accepted"] is True
        and row["hidden_oracle_passed"] is True
        for row in rows
    )
    scored = sum(cast(Mapping[str, Any], row["semantic_quality"])["scored"] is True for row in rows)
    passed = sum(row["hidden_oracle_passed"] is True for row in rows)
    summary = {
        "v": 10,
        "kind": "rrcv2_functional_cell",
        "run_id": run_id,
        "replicate_id": replicate_id,
        "arm": arm,
        "owner_scope": scope,
        "overlay_sha256": OVERLAY_SHA256,
        "workload_sha256": WORKLOAD_SHA256,
        "oracles_sha256": ORACLES_SHA256,
        "task_count": len(rows),
        "functional_evidence_valid": len(rows) == 30 and scored == 30,
        "comparison_valid": len(rows) == 30
        and scored == 30
        and (arm != "rrc_warm" or hit_count * 10 >= 30 * 6),
        "quality": {"scored": scored, "passed": passed},
        "warm_hit_rate": {"numerator": hit_count, "denominator": 30},
        "token_metrics": _sum_metrics(rows),
        "request_priced_estimate_nano": sum(
            cast(int, row["request_priced_estimate_nano"]) for row in rows
        ),
        "cache_neutral_sensitivity_nano": sum(
            cast(int, row["cache_neutral_sensitivity_nano"]) for row in rows
        ),
        "tasks": rows,
    }
    _atomic_write(summary_path, _canonical_line(summary))
    return summary


def assemble_report(
    *, run_id: str, cells: Sequence[Mapping[str, Any]], overlay: Mapping[str, Any]
) -> dict[str, object]:
    """Assemble the closed functional-only report for exactly 20 isolated cells."""

    expected = {(replicate, arm) for replicate in REPLICATES for arm in ARMS}
    observed = {(cell.get("replicate_id"), cell.get("arm")) for cell in cells}
    complete = len(cells) == 20 and observed == expected
    by_key = {(cast(str, row["replicate_id"]), cast(str, row["arm"])): row for row in cells}
    parity: dict[str, bool] = {}
    warm_hits: dict[str, bool] = {}
    if complete:
        for replicate in REPLICATES:
            baseline = cast(Mapping[str, int], by_key[(replicate, "baseline")]["quality"])
            warm = cast(Mapping[str, int], by_key[(replicate, "rrc_warm")]["quality"])
            parity[replicate] = warm["passed"] >= baseline["passed"] - 1
            rate = cast(Mapping[str, int], by_key[(replicate, "rrc_warm")]["warm_hit_rate"])
            warm_hits[replicate] = rate["numerator"] * 10 >= rate["denominator"] * 6
    valid = (
        complete
        and all(cell.get("functional_evidence_valid") is True for cell in cells)
        and all(parity.values())
        and all(warm_hits.values())
    )
    totals: dict[str, object] = {}
    for arm in ARMS:
        arm_cells = [row for row in cells if row.get("arm") == arm]
        totals[arm] = {
            "token_metrics": _sum_metrics(arm_cells),
            "request_priced_estimate_nano": sum(
                cast(int, row["request_priced_estimate_nano"]) for row in arm_cells
            ),
            "cache_neutral_sensitivity_nano": sum(
                cast(int, row["cache_neutral_sensitivity_nano"]) for row in arm_cells
            ),
            "quality_passed": sum(
                cast(int, cast(Mapping[str, Any], row["quality"])["passed"]) for row in arm_cells
            ),
        }
    return {
        "v": 10,
        "kind": "rrcv2_functional_report",
        "run_id": run_id,
        "overlay_sha256": _sha(_canonical_line(dict(overlay))),
        "economic_claim_eligible": False,
        "savings_claimed": False,
        "verdict": NO_SAVINGS_VERDICT,
        "functional_comparison_valid": valid,
        "complete_replicates": 4 if complete else 0,
        "parity": parity,
        "warm_hit_gate": warm_hits,
        "arm_totals": totals,
        "cells": [dict(row) for row in cells],
    }


def run_matrix(
    *,
    repo: Path,
    output_root: Path,
    run_id: str,
    model: ModelPort,
    authorize_calls: bool = True,
) -> dict[str, object]:
    workload, oracles, overlay = load_authorities(repo)
    manifest = {
        "v": 10,
        "kind": "rrcv2_functional_run",
        "run_id": run_id,
        "workload_sha256": WORKLOAD_SHA256,
        "oracles_sha256": ORACLES_SHA256,
        "analyzer_sha256": ANALYZER_SHA256,
        "overlay_sha256": OVERLAY_SHA256,
        "cell_schedule": workload["protocol"]["cell_schedule"],
    }
    manifest_path = output_root / "manifest.v10.json"
    if manifest_path.exists():
        if _read_regular(manifest_path, mode=0o600) != _canonical_line(manifest):
            raise ValueError("resume manifest differs from the frozen run")
    else:
        _atomic_write(manifest_path, _canonical_line(manifest))
    product_refs = _product_authority_refs(repo) if authorize_calls else None
    cells = [
        run_cell(
            repo=repo,
            output_root=output_root,
            run_id=run_id,
            replicate_id=cast(str, scheduled["replicate_id"]),
            arm=cast(str, scheduled["arm"]),
            model=model,
            workload=workload,
            oracles=oracles,
            overlay=overlay,
            product_authority_refs=product_refs,
        )
        for scheduled in cast(Sequence[Mapping[str, Any]], workload["protocol"]["cell_schedule"])
    ]
    report = assemble_report(run_id=run_id, cells=cells, overlay=overlay)
    _atomic_write(output_root / "report.v10.json", _canonical_line(report))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--codex-bin", default=os.environ.get("RRC_CODEX_BIN", "codex"))
    args = parser.parse_args()
    repo = args.repo.resolve()
    load_authorities(repo)
    if args.validate_only:
        print("RRCv2 v10 functional authorities: valid")
        return
    if args.output is None or args.run_id is None:
        parser.error("--output and --run-id are required unless --validate-only is used")
    report = run_matrix(
        repo=repo,
        output_root=args.output.resolve(),
        run_id=args.run_id,
        model=CodexModel(executable=args.codex_bin),
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
