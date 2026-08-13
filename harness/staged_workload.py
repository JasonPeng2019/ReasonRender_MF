"""Frozen, append-only RuleForge contracts for the measured ContextMesh workflow."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from harness.four_worker_plan import OverlapLedgerEntry, WorkerPlan, build_overlap_ledger, freeze_worker_plans, manifest_sha256

STAGE_IDS = (
    "stage-01", "stage-02", "stage-03", "stage-04", "stage-05", "stage-06", "stage-07", "stage-08", "stage-09", "stage-10",
    "stage-11", "stage-12", "stage-13", "stage-14", "stage-15", "stage-16", "stage-17", "stage-18", "stage-19", "stage-20",
    "stage-21", "stage-22",
    "stage-23", "stage-24", "stage-25", "stage-26",
    "stage-27", "stage-28", "stage-29", "stage-30",
    "stage-31", "stage-32", "stage-33", "stage-34",
    "stage-35", "stage-36", "stage-37",
)
_STAGE_COHORTS = {
    "stage-01": "core",
    "stage-02": "growth",
    "stage-03": "stage3",
    "stage-04": "operational-01",
    "stage-05": "operational-02",
    "stage-06": "operational-03",
    "stage-07": "operational-04",
    "stage-08": "operational-05",
    "stage-09": "operational-06",
    "stage-10": "operational-07",
    "stage-11": "operational-08",
    "stage-12": "operational-09",
    "stage-13": "operational-10",
    "stage-14": "operational-11",
    "stage-15": "operational-12",
    "stage-16": "operational-13",
    "stage-17": "operational-14",
    "stage-18": "operational-15",
    "stage-19": "operational-16",
    "stage-20": "operational-17",
    "stage-21": "operational-18",
    "stage-22": "operational-19",
    "stage-23": "operational-20",
    "stage-24": "operational-21",
    "stage-25": "operational-22",
    "stage-26": "operational-23",
    "stage-27": "operational-24",
    "stage-28": "operational-25",
    "stage-29": "operational-26",
    "stage-30": "operational-27",
    "stage-31": "operational-28",
    "stage-32": "operational-29",
    "stage-33": "operational-30",
    "stage-34": "operational-31",
    "stage-35": "operational-32",
    "stage-36": "operational-33",
    "stage-37": "operational-34",
}
# Stage 3 was completed before the extension existed and intentionally did not
# advance rollout.py.  The first appended stage therefore consumes its retained
# stage-02 revision, then resumes normal append-only stage revisions.
_STAGE_REVISIONS = {
    "stage-01": ("stage-00", "stage-01"),
    "stage-02": ("stage-01", "stage-02"),
    "stage-03": ("stage-02", None),
    "stage-04": ("stage-02", "stage-04"),
    "stage-05": ("stage-04", "stage-05"),
    "stage-06": ("stage-05", None),
    "stage-07": ("stage-05", "stage-07"),
    "stage-08": ("stage-07", None),
    # Clean post-repair work is independent of the earlier optional rollout
    # edit, so differing retained acceptance outcomes cannot contaminate it.
    "stage-09": ("independent", None),
    "stage-10": ("independent", None),
    "stage-11": ("independent", None),
    "stage-12": ("independent", None),
    "stage-13": ("independent", None),
    "stage-14": ("independent", None),
    "stage-15": ("independent", None),
    "stage-16": ("independent", None),
    "stage-17": ("independent", None),
    "stage-18": ("independent", None),
    "stage-19": ("independent", None),
    "stage-20": ("independent", None),
    "stage-21": ("independent", None),
    "stage-22": ("independent", None),
    "stage-23": ("independent", None),
    "stage-24": ("independent", None),
    "stage-25": ("independent", None),
    "stage-26": ("independent", None),
    "stage-27": ("independent", None),
    "stage-28": ("independent", None),
    "stage-29": ("independent", None),
    "stage-30": ("independent", None),
    "stage-31": ("independent", None),
    "stage-32": ("independent", None),
    "stage-33": ("independent", None),
    "stage-34": ("independent", None),
    "stage-35": ("stage-00", "stage-35"),
    "stage-36": ("stage-35", "stage-36"),
    "stage-37": ("stage-36", None),
}
_ROLLOUT_PATH = "ruleforge/rollout.py"
_INDEPENDENT_STAGES = frozenset({"stage-09", "stage-10", "stage-11", "stage-12", "stage-13", "stage-14", "stage-15", "stage-16", "stage-17", "stage-18", "stage-19", "stage-20", "stage-21", "stage-22", "stage-23", "stage-24", "stage-25", "stage-26", "stage-27", "stage-28", "stage-29", "stage-30", "stage-31", "stage-32", "stage-33", "stage-34"})
INDEPENDENT_COHORT_STARTS = frozenset({*_INDEPENDENT_STAGES, "stage-35"})


@dataclass(frozen=True, slots=True)
class StageWorkload:
    """One stage's independent tasks and the prior rollout revision they consume."""

    stage_id: str
    cohort: str
    required_rollout_revision: str
    next_rollout_revision: str | None
    plans: tuple[WorkerPlan, ...]


def _rollout_fact(revision: str) -> str:
    return f'STAGE_REVISION = "{revision}"'


def _stage_plan(
    plan: WorkerPlan,
    *,
    stage_id: str,
    rollout_revision: str,
    owns_rollout: bool,
    next_rollout_revision: str | None,
    include_rollout: bool = True,
) -> WorkerPlan:
    """Keep the task distinct while adding a natural two-worker rollout overlap."""

    reads = plan.initial_read_paths
    source_facts = dict(plan.source_fact_requirements)
    plan_facts = dict(plan.plan_fact_requirements)
    if include_rollout and plan.worker_id in {"worker-01", "worker-02"}:
        reads = (*reads, _ROLLOUT_PATH)
        source_facts[_ROLLOUT_PATH] = (_rollout_fact(rollout_revision),)
        plan_facts[_ROLLOUT_PATH] = ("Versioned RuleForge rollout contract.",)
    writes = plan.owned_write_paths
    objective = plan.objective
    if include_rollout and owns_rollout:
        writes = (*writes, _ROLLOUT_PATH)
        objective += f" After the focused rule work passes, advance STAGE_REVISION to {next_rollout_revision!r}."
    return replace(
        plan,
        task_id=f"{stage_id}-{plan.task_id}",
        worktree=f"stages/{stage_id}/{plan.worktree}",
        branch=f"codex/{stage_id}/{plan.worker_id}",
        objective=objective,
        owned_write_paths=writes,
        initial_read_paths=reads,
        source_fact_requirements=tuple(source_facts.items()),
        plan_fact_requirements=tuple(plan_facts.items()),
    )


def _stage_three_base_plans() -> tuple[WorkerPlan, ...]:
    domain = (
        "NormalizedInput is NormalizedInput(values, subject_id, request_id)",
        "Decision: allowed has code None",
    )
    evaluator = (
        "evaluate_definition(definition, expected, input) returns Decision",
        "missing or failed comparison rejects with error_code and one Evidence item",
        "The evaluator owns the configured comparator semantics",
    )
    registry = (
        "RuleRegistry.register(definition, expected) stores it under domain:name:source_field; get(key) returns (definition, expected)",
    )
    base = ("definition(domain, name, source_field, comparator, error_code) returns RuleDefinition",)
    values = (
        (
            "compliance",
            "retention_class",
            "required_retention_class",
            "equals",
            "COMPLIANCE_RETENTION_DENIED",
            "regulated",
            ("ruleforge/domain.py", "ruleforge/evaluator.py", "ruleforge/policy_catalog.py"),
            {"ruleforge/domain.py": domain, "ruleforge/evaluator.py": evaluator,
             "ruleforge/policy_catalog.py": ("compliance.required_retention_class -> compliance, required_retention_class, retention_class, equals, COMPLIANCE_RETENTION_DENIED, regulated",)},
        ),
        (
            "delivery",
            "delivery_score",
            "minimum_delivery_score",
            "at_least",
            "DELIVERY_SCORE_DENIED",
            "90.0",
            ("ruleforge/domain.py", "ruleforge/evaluator.py", "ruleforge/registry.py", "ruleforge/policy_catalog.py"),
            {"ruleforge/domain.py": domain, "ruleforge/evaluator.py": evaluator, "ruleforge/registry.py": registry,
             "ruleforge/policy_catalog.py": ("delivery.minimum_delivery_score -> delivery, minimum_delivery_score, delivery_score, at_least, DELIVERY_SCORE_DENIED, 90.0",)},
        ),
        (
            "eligibility",
            "eligibility_tier",
            "approved_eligibility_tier",
            "one_of",
            "ELIGIBILITY_TIER_DENIED",
            "gold, platinum",
            ("ruleforge/evaluator.py", "ruleforge/registry.py", "ruleforge/errors.py", "ruleforge/rules/base.py", "ruleforge/policy_catalog.py"),
            {"ruleforge/evaluator.py": evaluator, "ruleforge/registry.py": registry, "ruleforge/rules/base.py": base,
             "ruleforge/policy_catalog.py": ("eligibility.approved_eligibility_tier -> eligibility, approved_eligibility_tier, eligibility_tier, one_of, ELIGIBILITY_TIER_DENIED, gold, platinum",)},
        ),
        (
            "risk",
            "risk_score",
            "minimum_risk_score",
            "at_least",
            "RISK_SCORE_DENIED",
            "25.0",
            ("ruleforge/domain.py", "ruleforge/registry.py", "ruleforge/rules/base.py", "ruleforge/service.py", "ruleforge/policy_catalog.py"),
            {"ruleforge/domain.py": domain, "ruleforge/registry.py": registry, "ruleforge/rules/base.py": base,
             "ruleforge/service.py": ("PolicyService.evaluate(payload, subject_id, request_id) -> normalize_payload(payload, subject_id, request_id) -> evaluate_all(self.registry.all(), data)",),
             "ruleforge/policy_catalog.py": ("risk.minimum_risk_score -> risk, minimum_risk_score, risk_score, at_least, RISK_SCORE_DENIED, 25.0",)},
        ),
    )
    plans: list[WorkerPlan] = []
    for index, (domain_name, source_field, rule_name, comparator, error_code, expected, reads, facts) in enumerate(values, start=1):
        worker_id = f"worker-{index:02d}"
        plans.append(
            WorkerPlan(
                task_id=f"ruleforge-{domain_name}",
                worker_id=worker_id,
                worktree=f"worktrees/ruleforge-{worker_id}",
                branch=f"codex/ruleforge-{worker_id}",
                objective=(
                    f"Add the {domain_name} {rule_name} rule from policy_catalog profile "
                    f"{domain_name}.{rule_name}; preserve {source_field}, {comparator}, {error_code}, and {expected}."
                ),
                owned_write_paths=(f"ruleforge/rules/{domain_name}_stage.py", f"tests/test_{domain_name}_stage_rule.py"),
                acceptance_cmd=f"python -m pytest tests/test_{domain_name}_stage_rule.py -q",
                initial_read_paths=reads,
                task_requirements=("use RuleRegistry", "return a Decision"),
                source_fact_requirements=tuple(facts.items()),
                plan_fact_requirements=tuple(
                    (path, ("RuleForge source contract used by this staged task.",)) for path in reads
                ),
            )
        )
    return tuple(plans)


def _operational_stage_plans(cohort: str) -> tuple[WorkerPlan, ...]:
    """Return four new operational controls from the existing large catalog.

    These are deliberately new RuleForge modules and focused tests, not a
    replay of the first three stages.  The catalog remains a natural overlap:
    every worker needs a different record from it, while their remaining read
    sets overlap only where their concrete rule implementation requires it.
    """

    try:
        group = int(cohort.removeprefix("operational-"))
    except ValueError as error:  # pragma: no cover - guarded by the stage map.
        raise ValueError(f"unknown operational cohort: {cohort}") from error
    if group < 1:
        raise ValueError(f"unknown operational cohort: {cohort}")
    # These are intentionally full worker-facing interface facts, not vague
    # anchors.  The source owner must preserve them in its five-field brief so
    # a peer can implement without guessing imports or trial-and-error tests.
    domain = (
        "class NormalizedInput -> Import Decision, Evidence, NormalizedInput, RuleDefinition, stable_rule_key from ruleforge.domain; call NormalizedInput(values, subject_id, request_id) and data.value(field)",
        "class Decision -> Decision.allow(evidence) returns allowed with code None; Decision.reject(code, evidence) returns denied with the supplied code",
        "class RuleDefinition -> RuleDefinition(name, domain, source_field, comparator, error_code); stable_rule_key(definition) returns exactly domain:name:source_field and is the key passed to RuleRegistry.get",
    )
    evaluator = (
        "def evaluate_definition -> Import evaluate_definition from ruleforge.evaluator and call evaluate_definition(definition, expected, data); it returns Decision and is not a RuleRegistry method",
        "def compare -> comparator semantics are one_of, equals, at_least, greater_than",
        "if observed is None -> missing or failed comparison returns Decision.reject(definition.error_code, Evidence(...))",
    )
    registry = (
        "class RuleRegistry -> Import RuleRegistry from ruleforge.registry; construct RuleRegistry(); register(definition, expected) stores under stable_rule_key(definition), get(key) returns (definition, expected), all() returns registered pairs",
    )
    base = (
        "def definition -> Import definition from ruleforge.rules.base; definition(domain, name, source_field, comparator, error_code) returns RuleDefinition",
    )
    catalog_api = (
        "class PolicyProfile -> Import profile from ruleforge.policy_catalog; profile(key) returns frozen PolicyProfile(key, domain, name, source_field, comparator, error_code, expected, family) or raises ValueError for an unknown key",
    )
    read_sets = (
        ("ruleforge/domain.py", "ruleforge/evaluator.py", "ruleforge/policy_catalog.py"),
        ("ruleforge/domain.py", "ruleforge/registry.py", "ruleforge/policy_catalog.py"),
        ("ruleforge/evaluator.py", "ruleforge/registry.py", "ruleforge/errors.py", "ruleforge/rules/base.py"),
        ("ruleforge/domain.py", "ruleforge/registry.py", "ruleforge/rules/base.py", "ruleforge/service.py", "ruleforge/policy_catalog.py"),
    )
    plans: list[WorkerPlan] = []
    for offset, reads in enumerate(read_sets, start=1):
        number = (group - 1) * 4 + offset
        control = f"control_{number:03d}"
        profile_key = f"operational.{control}"
        comparator = ("equals", "at_least", "greater_than", "one_of")[(number - 1) % 4]
        if comparator == "equals":
            expected = f"tier-{number % 9}"
        elif comparator == "one_of":
            expected = f"region-{number % 7}, region-{(number + 3) % 7}, global"
        else:
            expected = f"{float((number % 25) + 10):.1f}"
        profile_fact = (
            f"{profile_key} -> operational, {control}, control_value_{number:03d}, "
            f"{comparator}, OPERATIONAL_CONTROL_{number:03d}_DENIED, {expected}"
        )
        worker_id = f"worker-{offset:02d}"
        facts: dict[str, tuple[str, ...]] = {}
        if "ruleforge/policy_catalog.py" in reads:
            facts["ruleforge/policy_catalog.py"] = (*catalog_api, f"'{profile_key}': PolicyProfile(")
        if "ruleforge/domain.py" in reads:
            facts["ruleforge/domain.py"] = domain
        if "ruleforge/evaluator.py" in reads:
            facts["ruleforge/evaluator.py"] = evaluator
        if "ruleforge/registry.py" in reads:
            facts["ruleforge/registry.py"] = registry
        if "ruleforge/rules/base.py" in reads:
            facts["ruleforge/rules/base.py"] = base
        if "ruleforge/errors.py" in reads:
            facts["ruleforge/errors.py"] = (
                "class RuleForgeError -> Import DuplicateRuleError, InvalidRuleConfiguration, RuleForgeError, UnknownRuleError from ruleforge.errors; RuleForgeError is their base class",
            )
        if "ruleforge/service.py" in reads:
            facts["ruleforge/service.py"] = (
                "class PolicyService -> Import PolicyService from ruleforge.service; PolicyService(registry).evaluate(payload, subject_id, request_id) calls normalize_payload then evaluate_all(self.registry.all(), data)",
            )
        plan_facts = {
            path: ("RuleForge source contract used by this operational task.",)
            for path in reads
        }
        # The coordinator plan carries this task's exact profile binding. The
        # shared file brief can therefore remain a reusable API/anchor digest
        # rather than copying every future catalog record to every Luna.
        if "ruleforge/policy_catalog.py" in reads:
            plan_facts["ruleforge/policy_catalog.py"] = (profile_fact,)
        else:
            anchor = reads[0]
            plan_facts[anchor] = (*plan_facts[anchor], *catalog_api, profile_fact)
        if "ruleforge/registry.py" not in reads:
            anchor = reads[0]
            plan_facts[anchor] = (*plan_facts[anchor], *registry)
        if "ruleforge/domain.py" not in reads:
            anchor = reads[0]
            plan_facts[anchor] = (*plan_facts[anchor], domain[-1])
        plans.append(
            WorkerPlan(
                task_id=f"ruleforge-operational_{number:03d}",
                worker_id=worker_id,
                worktree=f"worktrees/operational-{number:03d}",
                branch=f"codex/operational-{number:03d}",
                objective=(
                    f"Add a new RuleForge module for profile {profile_key}; resolve its complete PolicyProfile from "
                    "policy_catalog and preserve its source field, comparator, denial code, expected value, RuleRegistry registration, and Decision behavior."
                ),
                owned_write_paths=(f"ruleforge/rules/operational_{number:03d}.py", f"tests/test_operational_{number:03d}_rule.py"),
                acceptance_cmd=f"python -m pytest tests/test_operational_{number:03d}_rule.py -q",
                initial_read_paths=reads,
                task_requirements=("use the declared operational profile", "use RuleRegistry", "return a Decision"),
                source_fact_requirements=tuple(facts.items()),
                plan_fact_requirements=tuple(plan_facts.items()),
            )
        )
    return tuple(plans)


def stage_workload(stage_id: str) -> StageWorkload:
    """Return the frozen, stage-qualified task mapping for one linked stage."""

    try:
        cohort = _STAGE_COHORTS[stage_id]
        required, next_revision = _STAGE_REVISIONS[stage_id]
    except (KeyError, ValueError) as error:
        raise ValueError(f"unknown RuleForge stage: {stage_id}") from error
    if cohort.startswith("operational-"):
        base = _operational_stage_plans(cohort)
    else:
        base = _stage_three_base_plans() if cohort == "stage3" else freeze_worker_plans(cohort)
    plans = tuple(
        _stage_plan(
            plan,
            stage_id=stage_id,
            rollout_revision=required,
            owns_rollout=plan.worker_id == "worker-01" and next_revision is not None,
            next_rollout_revision=next_revision,
            include_rollout=stage_id not in _INDEPENDENT_STAGES,
        )
        for plan in base
    )
    return StageWorkload(stage_id, cohort, required, next_revision, plans)


def staged_workloads() -> tuple[StageWorkload, ...]:
    return tuple(stage_workload(stage_id) for stage_id in STAGE_IDS)


def _entry_with_horizon_facts(entry: OverlapLedgerEntry, facts: tuple[str, ...]) -> OverlapLedgerEntry:
    """Bind a reusable brief to its source contract, not a task manifest."""

    requirement_payload = {
        "canonical_path": entry.canonical_path,
        "required_facts": facts,
        "brief_schema_version": "file-brief/v1",
    }
    requirements_hash = hashlib.sha256(
        json.dumps(requirement_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    brief_id = "brief-" + hashlib.sha256(
        json.dumps(
            {
                "manifest_hash": entry.manifest_hash,
                "canonical_path": entry.canonical_path,
                "requirements_hash": requirements_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return replace(entry, brief_id=brief_id, requirements_hash=requirements_hash, required_facts=facts)


def staged_overlap_ledgers() -> tuple[tuple[OverlapLedgerEntry, ...], ...]:
    """Return stage ledgers with stable source facts sized for useful reuse.

    The horizon carries literal source facts only.  It never puts a later
    worker's objective, plan, test, or write path into an earlier worker packet.
    The rollout source is intentionally excluded because its revision changes at
    every stage and must exercise the diff-refresh path.  The large catalog is
    uses an immutable interface contract after the clean stage-21/22 seed.
    Task-specific PolicyProfile bindings travel in each Terra plan, so the
    file brief need contain the catalog API, not an ever-growing copy of new
    profile records.  Every later stage therefore reuses the owner-published
    catalog brief instead of paying a fresh owner publication tax.
    """

    stages = staged_workloads()
    ledgers = tuple(
        build_overlap_ledger(stage.plans, manifest_sha256(stage.plans)) for stage in stages
    )
    changing_paths = {_ROLLOUT_PATH}
    horizon: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for entries in ledgers:
        for entry in entries:
            horizon.setdefault(entry.canonical_path, set()).update(entry.required_facts)
            counts[entry.canonical_path] = counts.get(entry.canonical_path, 0) + 1
    result: list[tuple[OverlapLedgerEntry, ...]] = []
    # Keep the successfully published clean seed exactly as it was, then hold
    # its compact catalog interface brief stable for all later controls.  New
    # tasks get their literal profile binding from Terra, never from a new raw
    # catalog read, while the shared owner brief continues to provide the API.
    stable_catalog_window: set[str] = set()
    for window_entries in ledgers[20:22]:
        catalog = next(
            (entry for entry in window_entries if entry.canonical_path == "ruleforge/policy_catalog.py"),
            None,
        )
        if catalog is not None:
            stable_catalog_window.update(catalog.required_facts)
    for index, entries in enumerate(ledgers):
        catalog_window: set[str] = set()
        pair_start = index if index % 2 == 0 else index - 1
        for window_entries in ledgers[pair_start:pair_start + 2]:
            catalog = next(
                (entry for entry in window_entries if entry.canonical_path == "ruleforge/policy_catalog.py"),
                None,
            )
            if catalog is not None:
                catalog_window.update(catalog.required_facts)
        enriched = tuple(
            _entry_with_horizon_facts(
                entry,
                tuple(
                    sorted(
                        (stable_catalog_window if index >= 20 else catalog_window)
                        if entry.canonical_path == "ruleforge/policy_catalog.py"
                        else horizon[entry.canonical_path]
                    )
                ),
            )
            if counts[entry.canonical_path] > 1 and entry.canonical_path not in changing_paths
            else entry
            for entry in entries
        )
        result.append(enriched)
    return tuple(result)


def write_stage_manifest(root: str | Path, stage: StageWorkload) -> Path:
    """Write an arm-neutral task contract used by all three comparison arms."""

    destination = Path(root) / f"{stage.stage_id}-manifest.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload: Mapping[str, object] = {
        "schema_version": 1,
        "stage_id": stage.stage_id,
        "cohort": stage.cohort,
        "required_rollout_revision": stage.required_rollout_revision,
        "next_rollout_revision": stage.next_rollout_revision,
        "worker_contract_sha256": manifest_sha256(stage.plans),
        "tasks": [
            {
                "task_id": plan.task_id,
                "worker_id": plan.worker_id,
                "objective": plan.objective,
                "owned_write_paths": list(plan.owned_write_paths),
                "initial_read_paths": list(plan.initial_read_paths),
                "acceptance_cmd": plan.acceptance_cmd,
            }
            for plan in stage.plans
        ],
    }
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def stage_manifest_sha256(stage: StageWorkload) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "stage_id": stage.stage_id,
                "cohort": stage.cohort,
                "plans": [plan.task_id for plan in stage.plans],
                "contract": manifest_sha256(stage.plans),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "STAGE_IDS",
    "StageWorkload",
    "stage_manifest_sha256",
    "stage_workload",
    "staged_overlap_ledgers",
    "staged_workloads",
    "write_stage_manifest",
]
