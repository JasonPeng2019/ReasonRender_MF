"""Runtime adapters that put canonical provider calls behind product permits."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from rrc.cell_journal import SQLiteCellJournal
from rrc.contract import Completion, ModelPort, ModelRole, RunContext
from rrc.dispatch_permit import (
    PRODUCT_STAGE_MATRIX,
    AuthorityRef,
    DispatchPermitV1,
    ProductAttemptDispatchRequestV5,
    ProductCellDispatchRequestV1,
    ProductCellJournalCursorV1,
    ProductJournalCursorV2,
    authorize_product,
    product_call_id,
    read_authority,
)
from rrc.journal import AttemptHandle

_MAX_AUTHORITY = 16 * 1024 * 1024


def _authority_ref(path: Path, *, mode: int | None = 0o600) -> AuthorityRef:
    path = path.absolute()
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_size > _MAX_AUTHORITY
        or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
    ):
        raise ValueError(f"product authority is not a bounded regular file: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_AUTHORITY:
            block = os.read(descriptor, min(65_536, _MAX_AUTHORITY + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    after = os.lstat(path)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or len(raw) != opened.st_size
        or len(raw) > _MAX_AUTHORITY
    ):
        raise ValueError(f"product authority changed while opening: {path}")
    return AuthorityRef(path, hashlib.sha256(raw).hexdigest(), len(raw))


def product_authority_refs(repo: Path) -> dict[str, AuthorityRef]:
    """Resolve the complete reviewed product authority graph for active runtime."""

    repo = repo.resolve(strict=True)
    generated = repo / ".generated/state/rrcv2-convergence"
    calls = generated / "capability/calls"

    def regular(relative: str) -> AuthorityRef:
        return _authority_ref(repo / relative, mode=None)

    def sealed(relative: str) -> AuthorityRef:
        return _authority_ref(generated / relative)

    return {
        "workload_core": sealed("economic/workload-core.v6.json"),
        "overlay": sealed("economic/economic-binding-overlay.v10.json"),
        "source_workload": regular("contextmesh/bench/rrcv2_workload.json"),
        "source_oracles": regular("contextmesh/bench/rrcv2_oracles.json"),
        "capability_manifest": sealed("capability/capability-manifest.v1.json"),
        "capability_summary": sealed("capability/capability-summary.v1.json"),
        "capability_inventory": sealed("verify/capability-evidence-inventory.v1.json"),
        "sandbox_v2": sealed("verify/sandbox-evidence.v2.json"),
        "setup": sealed("verify/setup-accounting.v1.json"),
        "setup_v2": sealed("verify/setup-accounting.v2.json"),
        "worker_attestation": sealed("verify/native-worker-context-attestation.v1.json"),
        "root_rollout": _authority_ref(calls / "cap-08-root-strong-medium-native/rollout.jsonl"),
        "worker_rollout": _authority_ref(calls / "cap-09-worker-small-low-native/rollout.jsonl"),
        "root_environment": _authority_ref(
            calls / "cap-08-root-strong-medium-native/environment.json"
        ),
        "worker_environment": _authority_ref(
            calls / "cap-09-worker-small-low-native/environment.json"
        ),
        "output_v3": sealed("verify/generated-output-authority.v3.json"),
        "output_v2": sealed("verify/generated-output-authority.v2.json"),
        "output_v1": sealed("generated-output-authority.v1.json"),
        "abandoned": sealed("verify/aborted-capability-attempt-1/archive-manifest.json"),
        "redesign": sealed("capability/explore-strong/redesign-evidence.v2.json"),
        "analyzer": regular("contextmesh/bench/rrcv2_analyzer.py"),
        "plan_seal": sealed("reviews/plan-m6-post-v20-hook-bootstrap-v2.seal.json"),
        "plan": regular("PLAN.md"),
        "plan_transcript": sealed("reviews/plan-m6-post-v20-hook-bootstrap-v2.txt"),
        "plan_record": regular(".generated/state/reviews/plan.toml"),
        "claim_transcript": sealed("reviews/claim-post-capability-functional-v8.txt"),
        "claim_record": regular(".generated/state/reviews/claim-worktree.toml"),
        "config": sealed("verify/product-config-manifest.v15.json"),
        "economic_source": regular("rrc/economic_authority.py"),
        "permit_source": regular("rrc/dispatch_permit.py"),
        "capability_source": regular("contextmesh/scripts/rrcv2_capability_matrix.py"),
        "sandbox_source": regular("contextmesh/scripts/rrcv2_sandbox_probe_v2.py"),
    }


def authorize_product_with_refs(
    *,
    request: ProductAttemptDispatchRequestV5 | ProductCellDispatchRequestV1,
    journal_cursor: ProductJournalCursorV2 | ProductCellJournalCursorV1,
    task_envelope_ref: AuthorityRef,
    cell_attempt_binding_ref: AuthorityRef | None,
    cell_journal: SQLiteCellJournal | None,
    repo: Path,
    refs: Mapping[str, AuthorityRef],
) -> DispatchPermitV1:
    """Apply the complete reviewed authority graph to one runtime request."""

    return authorize_product(
        request=request,
        journal_cursor=journal_cursor,
        task_envelope_ref=task_envelope_ref,
        cell_attempt_binding_ref=cell_attempt_binding_ref,
        cell_journal_authority=cell_journal,
        repo_root=repo.resolve(strict=True),
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


class ContextMeshProductModel:
    """Authorize every controller and native-worker call for one rooted cell."""

    def __init__(
        self,
        delegate: ModelPort,
        *,
        repo: Path,
        refs: Mapping[str, AuthorityRef],
        task_envelope_ref: AuthorityRef,
        cell_attempt_binding_ref: AuthorityRef,
        cell_journal: SQLiteCellJournal,
        run_id: str,
        replicate_id: str,
        arm: str,
        cell_id: str,
        scope: str = "interactive",
    ) -> None:
        if arm not in {"rrc_cold", "rrc_warm"}:
            raise ValueError("ContextMesh product arm must be rrc_cold or rrc_warm")
        if scope not in {"interactive", "experiment"}:
            raise ValueError("ContextMesh product scope is invalid")
        self._delegate = delegate
        self._repo = repo.resolve(strict=True)
        self._refs = refs
        self._task_envelope_ref = task_envelope_ref
        self._binding_ref = cell_attempt_binding_ref
        self._cell_journal = cell_journal
        self._run_id = run_id
        self._replicate_id = replicate_id
        self._arm = arm
        self._cell_id = cell_id
        self._scope = scope
        self.provider = delegate.provider
        self.product_cell_id = cell_id
        raw = read_authority(task_envelope_ref)
        value = json.loads(raw.decode("utf-8", errors="strict"))
        task = value.get("task") if isinstance(value, dict) else None
        task_id = task.get("task_id") if isinstance(task, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("product task envelope has no task identity")
        self._task_id = task_id
        binding = json.loads(read_authority(cell_attempt_binding_ref))
        generation = binding.get("generation") if isinstance(binding, dict) else None
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("product cell binding has no valid generation")
        self._binding_generation = generation

    def _request(
        self,
        *,
        attempt: AttemptHandle,
        ctx: RunContext,
        branch: str,
        stage: str,
        stage_ordinal: int,
    ) -> ProductAttemptDispatchRequestV5:
        expected_arm = "cold" if self._arm == "rrc_cold" else "warm"
        if (
            ctx.task_id != self._task_id
            or ctx.arm != expected_arm
            or ctx.cell_id not in {None, self._cell_id}
        ):
            raise ValueError("provider context differs from its rooted product cell")
        transport = "contextmesh" if stage == "implement" else "direct"
        try:
            surface, cursor = PRODUCT_STAGE_MATRIX[
                (transport, self._arm, branch, stage, stage_ordinal)
            ]
        except KeyError as exc:
            raise ValueError("provider stage is absent from the product dispatch matrix") from exc
        request = ProductAttemptDispatchRequestV5(
            call_id="",
            scope=self._scope,
            controller="contextmesh",
            task_id=self._task_id,
            task_envelope_sha256=self._task_envelope_ref.sha256,
            root_binding_sha256=self._binding_ref.sha256,
            run_id=self._run_id,
            replicate_id=self._replicate_id,
            arm=self._arm,
            branch=branch,
            stage=stage,
            stage_ordinal=stage_ordinal,
            journal_cursor=cursor,
            cell_id=self._cell_id,
            attempt_id=attempt.attempt_id,
            transport=transport,
            surface_id=surface,
        )
        return replace(request, call_id=product_call_id(request))

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
            raise ValueError("product call ID changed before authorization")
        cursor = ProductJournalCursorV2(
            attempt_id=attempt.attempt_id,
            cell_id=self._cell_id,
            call_id=call_id,
            stage_ordinal=stage_ordinal,
            journal_generation=attempt.generation,
            prior_state="absent",
            root_binding_sha256=self._binding_ref.sha256,
            root_binding_generation=self._binding_generation,
        )
        permit = authorize_product_with_refs(
            request=request,
            journal_cursor=cursor,
            task_envelope_ref=self._task_envelope_ref,
            cell_attempt_binding_ref=self._binding_ref,
            cell_journal=self._cell_journal,
            repo=self._repo,
            refs=self._refs,
        )
        if permit.call_id != call_id or permit.kind != "product":
            raise ValueError("product permit differs from the provider call")

    def complete(
        self,
        role: ModelRole,
        prompt: str,
        ctx: RunContext,
        stage: str,
    ) -> Completion:
        return self._delegate.complete(role, prompt, ctx, stage)


__all__ = ["ContextMeshProductModel", "authorize_product_with_refs", "product_authority_refs"]
