"""Public COLD/WARM control loop for RRCv2 Lane A."""

from __future__ import annotations

from rrc.contract import (
    ArmMode,
    BranchDecision,
    Candidate,
    Config,
    CostEvent,
    ModelPort,
    RetrievalPort,
    RunContext,
    SolveOutcome,
    Spec,
    StoreFailure,
    Task,
    Template,
)
from rrc.pipeline.stages import implement_stage, repair_stage, spec_stage
from rrc.pipeline.template import resolve_template, templatize
from rrc.pipeline.verify import run_pytest


def _oracle_result(task: Task, code: str) -> bool | None:
    if task.oracle_tests is None or not task.oracle_tests.strip():
        return None
    passed, _ = run_pytest(code, task.oracle_tests)
    return passed


def _outcome(
    task: Task,
    mode: ArmMode,
    *,
    code: str,
    passed: bool,
    branch: BranchDecision,
    repairs: int,
    escalated: bool,
    template: Template | None,
    events: list[CostEvent],
) -> SolveOutcome:
    return SolveOutcome(
        task_id=task.task_id,
        arm=mode.value,
        code=code,
        passed=passed,
        pass_at_1=_oracle_result(task, code),
        branch=branch,
        repairs=repairs,
        escalated=escalated,
        template=template,
        cost_events=tuple(events),
    )


def _implement_with_optional_repair(
    specification: Spec,
    model: ModelPort,
    ctx: RunContext,
    events: list[CostEvent],
    *,
    allow_repair: bool,
) -> tuple[str, bool, int]:
    code, event = implement_stage(specification, model, ctx)
    events.append(event)
    passed, output = run_pytest(code, specification.tests)
    repairs = 0
    if not passed and allow_repair:
        code, event = repair_stage(specification, code, output, model, ctx)
        events.append(event)
        repairs = 1
        passed, _ = run_pytest(code, specification.tests)
    return code, passed, repairs


def _resolve_reuse(
    task: Task,
    retrieval: RetrievalPort,
    cfg: Config,
) -> tuple[Spec, Template] | None:
    for candidate in retrieval.retrieve(task, cfg):
        if not isinstance(candidate, Candidate) or not candidate.external_ref:
            continue
        template = retrieval.get_template(candidate.external_ref)
        if not isinstance(template, Template) or template.external_ref != candidate.external_ref:
            continue
        rendered = resolve_template(template, task)
        if rendered is not None:
            return rendered, template
    return None


def _store_success(
    task: Task,
    outcome: SolveOutcome,
    retrieval: RetrievalPort,
) -> SolveOutcome:
    template = outcome.template
    if not outcome.passed or template is None:
        return outcome
    try:
        retrieval.store(task, template, outcome)
    except Exception as error:
        raise StoreFailure(outcome) from error
    return outcome


def _fresh(
    task: Task,
    mode: ArmMode,
    model: ModelPort,
    retrieval: RetrievalPort,
    ctx: RunContext,
    *,
    allow_repair: bool,
) -> SolveOutcome:
    events: list[CostEvent] = []
    specification, event = spec_stage(task, model, ctx)
    events.append(event)
    if specification is None:
        return _outcome(
            task,
            mode,
            code="",
            passed=False,
            branch=BranchDecision.MISS,
            repairs=0,
            escalated=False,
            template=None,
            events=events,
        )

    code, passed, repairs = _implement_with_optional_repair(
        specification,
        model,
        ctx,
        events,
        allow_repair=allow_repair,
    )
    template = templatize(specification) if passed else None
    outcome = _outcome(
        task,
        mode,
        code=code,
        passed=passed,
        branch=BranchDecision.MISS,
        repairs=repairs,
        escalated=False,
        template=template,
        events=events,
    )
    return _store_success(task, outcome, retrieval) if mode is ArmMode.WARM else outcome


def _reuse(
    task: Task,
    mode: ArmMode,
    model: ModelPort,
    retrieval: RetrievalPort,
    ctx: RunContext,
    specification: Spec,
    template: Template,
    *,
    allow_repair: bool,
) -> SolveOutcome:
    events: list[CostEvent] = []
    code, passed, repairs = _implement_with_optional_repair(
        specification,
        model,
        ctx,
        events,
        allow_repair=allow_repair,
    )
    if passed:
        return _store_success(
            task,
            _outcome(
                task,
                mode,
                code=code,
                passed=True,
                branch=BranchDecision.REUSE,
                repairs=repairs,
                escalated=False,
                template=template,
                events=events,
            ),
            retrieval,
        )

    fallback_spec, event = spec_stage(task, model, ctx, stage="fallback_spec")
    events.append(event)
    if fallback_spec is None:
        return _outcome(
            task,
            mode,
            code=code,
            passed=False,
            branch=BranchDecision.REUSE,
            repairs=repairs,
            escalated=True,
            template=None,
            events=events,
        )

    code, event = implement_stage(fallback_spec, model, ctx, stage="fallback_implement")
    events.append(event)
    passed, _ = run_pytest(code, fallback_spec.tests)
    fallback_template = templatize(fallback_spec) if passed else None
    outcome = _outcome(
        task,
        mode,
        code=code,
        passed=passed,
        branch=BranchDecision.REUSE,
        repairs=repairs,
        escalated=True,
        template=fallback_template,
        events=events,
    )
    return _store_success(task, outcome, retrieval)


def solve(
    task: Task,
    *,
    mode: ArmMode,
    model: ModelPort,
    retrieval: RetrievalPort,
    cfg: Config,
) -> SolveOutcome:
    """Solve a task through the frozen Lane A public seam."""

    if mode not in (ArmMode.COLD, ArmMode.WARM):
        raise ValueError(f"unsupported Lane A arm: {mode!r}")
    if isinstance(cfg.repair_cap_N, bool) or not isinstance(cfg.repair_cap_N, int):
        raise ValueError("repair_cap_N must be an integer")
    if cfg.repair_cap_N < 0:
        raise ValueError("repair_cap_N must be non-negative")

    ctx = RunContext(arm=mode.value, task_id=task.task_id)
    allow_repair = cfg.repair_cap_N > 0
    if mode is ArmMode.COLD:
        return _fresh(task, mode, model, retrieval, ctx, allow_repair=allow_repair)

    reusable = _resolve_reuse(task, retrieval, cfg)
    if reusable is None:
        return _fresh(task, mode, model, retrieval, ctx, allow_repair=allow_repair)
    specification, template = reusable
    return _reuse(
        task,
        mode,
        model,
        retrieval,
        ctx,
        specification,
        template,
        allow_repair=allow_repair,
    )
