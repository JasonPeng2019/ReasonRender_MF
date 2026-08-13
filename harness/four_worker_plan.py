"""Deterministic Stage A worker plans and the Stage B overlap ledger."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath


def _canonical_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty relative path")
    portable = value.replace("\\", "/")
    if portable.startswith("/") or PureWindowsPath(value).drive:
        raise ValueError(f"{field} must be relative")
    parts = portable.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(portable).is_absolute():
        raise ValueError(f"{field} must remain under the target root")
    return "/".join(parts)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _texts(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValueError(f"{field} must be an iterable of strings")
    result = tuple(_text(item, field) for item in value)
    if not result:
        raise ValueError(f"{field} must not be empty")
    return result


def _paths(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValueError(f"{field} must be an iterable of paths")
    result = tuple(_canonical_path(item, field) for item in value)
    if not result:
        raise ValueError(f"{field} must not be empty")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _source_requirements(value: object, read_paths: tuple[str, ...]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ValueError("source_fact_requirements must be a path-to-facts mapping")
    normalized: list[tuple[str, tuple[str, ...]]] = []
    for path, facts in value.items():
        canonical = _canonical_path(path, "source_fact_requirements path")
        if canonical not in read_paths:
            raise ValueError("source_fact_requirements path must be in initial_read_paths")
        normalized.append((canonical, _texts(facts, "source_fact_requirements facts")))
    if len({path for path, _ in normalized}) != len(normalized):
        raise ValueError("source_fact_requirements paths must not repeat")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class WorkerPlan:
    """An immutable task contract.  Terra supplies the implementation plan later."""

    task_id: str
    worker_id: str
    worktree: str
    branch: str
    objective: str
    owned_write_paths: tuple[str, ...]
    acceptance_cmd: str
    initial_read_paths: tuple[str, ...]
    task_requirements: tuple[str, ...]
    source_fact_requirements: tuple[tuple[str, tuple[str, ...]], ...] = ()
    plan_fact_requirements: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "worker_id", _text(self.worker_id, "worker_id"))
        object.__setattr__(self, "worktree", _canonical_path(self.worktree, "worktree"))
        object.__setattr__(self, "branch", _text(self.branch, "branch"))
        object.__setattr__(self, "objective", _text(self.objective, "objective"))
        object.__setattr__(self, "owned_write_paths", _paths(self.owned_write_paths, "owned_write_paths"))
        object.__setattr__(self, "acceptance_cmd", _text(self.acceptance_cmd, "acceptance_cmd"))
        object.__setattr__(self, "initial_read_paths", _paths(self.initial_read_paths, "initial_read_paths"))
        object.__setattr__(self, "task_requirements", _texts(self.task_requirements, "task_requirements"))
        source_mapping = dict(self.source_fact_requirements)
        object.__setattr__(
            self,
            "source_fact_requirements",
            _source_requirements(source_mapping, self.initial_read_paths),
        )
        plan_mapping = dict(self.plan_fact_requirements)
        object.__setattr__(
            self,
            "plan_fact_requirements",
            _source_requirements(plan_mapping, self.initial_read_paths)
            if plan_mapping
            else self.source_fact_requirements,
        )

    def source_facts_for(self, canonical_path: str) -> tuple[str, ...]:
        """Return complete brief facts this task needs from one declared path."""

        return dict(self.source_fact_requirements).get(canonical_path, ())

    def plan_facts_for(self, canonical_path: str) -> tuple[str, ...]:
        """Return the small Terra-read anchor for one declared source path."""

        return dict(self.plan_fact_requirements).get(canonical_path, ())


@dataclass(frozen=True, slots=True)
class TerraPlan:
    """One concrete, coordinator-produced plan handed to one DeepSeek worker."""

    task_id: str
    worker_id: str
    plan_steps: tuple[str, ...]
    source_facts: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "worker_id", _text(self.worker_id, "worker_id"))
        object.__setattr__(self, "plan_steps", _texts(self.plan_steps, "plan_steps"))
        object.__setattr__(self, "source_facts", _texts(self.source_facts, "source_facts"))


def fixture_terra_plan(plan: WorkerPlan) -> TerraPlan:
    """Create a deterministic plan only for local topology fixtures.

    The measured runner never calls this helper: it loads artifacts produced by
    Terra after its raw preflight or RRC HIT render.
    """

    return TerraPlan(
        task_id=plan.task_id,
        worker_id=plan.worker_id,
        plan_steps=plan.task_requirements,
        source_facts=tuple(
            fact
            for _, facts in plan.plan_fact_requirements
            for fact in facts
        ),
    )


@dataclass(frozen=True, slots=True)
class OverlapLedgerEntry:
    """The immutable requirements brief for one genuinely shared read path."""

    brief_id: str
    canonical_path: str
    manifest_hash: str
    requirements_hash: str
    plan_steps: tuple[tuple[str, tuple[str, ...]], ...]
    source_owner: str
    peer_workers: tuple[str, ...]
    required_facts: tuple[str, ...] = ()


def _validate_plans(plans: Iterable[WorkerPlan]) -> tuple[WorkerPlan, ...]:
    materialized = tuple(plans)
    if not all(isinstance(plan, WorkerPlan) for plan in materialized):
        raise ValueError("plans must contain only WorkerPlan records")
    worker_ids = [plan.worker_id for plan in materialized]
    task_ids = [plan.task_id for plan in materialized]
    write_paths = [path for plan in materialized for path in plan.owned_write_paths]
    if len(set(worker_ids)) != len(worker_ids):
        raise ValueError("duplicate worker_id")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("duplicate task_id")
    if len(set(write_paths)) != len(write_paths):
        raise ValueError("duplicate owned write path")
    return materialized


def _semantic_plan(plan: WorkerPlan) -> dict[str, object]:
    return {
        "task_id": plan.task_id,
        "worker_id": plan.worker_id,
        "worktree": plan.worktree,
        "branch": plan.branch,
        "objective": plan.objective,
        "owned_write_paths": plan.owned_write_paths,
        "acceptance_cmd": plan.acceptance_cmd,
        "initial_read_paths": plan.initial_read_paths,
        "task_requirements": plan.task_requirements,
        "source_fact_requirements": plan.source_fact_requirements,
        "plan_fact_requirements": plan.plan_fact_requirements,
    }


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def manifest_sha256(plans: Iterable[WorkerPlan]) -> str:
    """Return the stable SHA-256 of every semantic field in the plans."""

    validated = _validate_plans(plans)
    ordered = sorted(validated, key=lambda plan: (plan.worker_id, plan.task_id))
    return _digest({"plans": [_semantic_plan(plan) for plan in ordered]})


def _plan(
    task_id: str,
    worker_id: str,
    worktree: str,
    branch: str,
    objective: str,
    write_paths: tuple[str, ...],
    acceptance_path: str,
    read_paths: tuple[str, ...],
    requirements: tuple[str, ...],
    source_requirements: Mapping[str, tuple[str, ...]] | None = None,
    plan_requirements: Mapping[str, tuple[str, ...]] | None = None,
) -> WorkerPlan:
    owned_paths = tuple(_canonical_path(path, "owned_write_paths") for path in write_paths)
    reads = tuple(_canonical_path(path, "initial_read_paths") for path in read_paths)
    return WorkerPlan(
        task_id=task_id,
        worker_id=worker_id,
        worktree=worktree,
        branch=branch,
        objective=objective,
        owned_write_paths=owned_paths,
        acceptance_cmd=f"python -m pytest {acceptance_path} -q",
        initial_read_paths=reads,
        task_requirements=requirements,
        source_fact_requirements=_source_requirements(source_requirements, reads),
        plan_fact_requirements=_source_requirements(plan_requirements, reads),
    )


def freeze_worker_plans(cohort: str = "core") -> tuple[WorkerPlan, ...]:
    """Return one fixed four-task RuleForge cohort for a direct 1+4 run."""

    if cohort == "growth":
        return _growth_worker_plans()
    if cohort != "core":
        raise ValueError(f"unknown RuleForge cohort: {cohort}")

    return (
        _plan(
            "ruleforge-security",
            "worker-01",
            "worktrees/ruleforge-worker-01",
            "codex/ruleforge-worker-01",
            "Add the security rule from policy_catalog profile security.required_access_level; preserve its field, comparator, error code, and expected value.",
            ("ruleforge/rules/security.py", "tests/test_security_rule.py"),
            "tests/test_security_rule.py",
            ("ruleforge/domain.py", "ruleforge/evaluator.py", "ruleforge/policy_catalog.py"),
            ("use RuleRegistry", "return a Decision"),
            {
                "ruleforge/domain.py": (
                    "NormalizedInput is NormalizedInput(values, subject_id, request_id)",
                    "Decision: allowed has code None",
                ),
                "ruleforge/evaluator.py": (
                    "evaluate_definition(definition, expected, input) returns Decision",
                    "missing or failed comparison rejects with error_code and one Evidence item",
                    "The evaluator owns the configured comparator semantics",
                ),
                "ruleforge/policy_catalog.py": (
                    "security.required_access_level -> security, required_access_level, access_level, equals, SECURITY_ACCESS_DENIED, internal",
                ),
            },
            {
                "ruleforge/domain.py": ("RuleForge domain contracts shared by every policy module.",),
                "ruleforge/evaluator.py": ("Comparison and decision evaluation for RuleForge rules.",),
                "ruleforge/policy_catalog.py": ("Versioned operational policy profiles used by RuleForge rule modules.",),
            },
        ),
        _plan(
            "ruleforge-limits",
            "worker-02",
            "worktrees/ruleforge-worker-02",
            "codex/ruleforge-worker-02",
            "Add the limits rule from policy_catalog profile limits.minimum_daily_requests; preserve its field, comparator, error code, and expected value.",
            ("ruleforge/rules/limits.py", "tests/test_limits_rule.py"),
            "tests/test_limits_rule.py",
            ("ruleforge/domain.py", "ruleforge/evaluator.py", "ruleforge/registry.py", "ruleforge/policy_catalog.py"),
            ("use RuleRegistry", "return a Decision"),
            {
                "ruleforge/domain.py": (
                    "RuleDefinition(name, domain, source_field, comparator, error_code)",
                    "Decision: allowed has code None",
                ),
                "ruleforge/evaluator.py": (
                    "evaluate_definition(definition, expected, input) returns Decision",
                    "missing or failed comparison rejects with error_code and one Evidence item",
                    "The evaluator owns the configured comparator semantics",
                ),
                "ruleforge/registry.py": (
                    "RuleRegistry.register(definition, expected) stores it under domain:name:source_field; get(key) returns (definition, expected)",
                ),
                "ruleforge/policy_catalog.py": (
                    "limits.minimum_daily_requests -> limits, minimum_daily_requests, daily_requests, at_least, LIMITS_REQUEST_DENIED, 100.0",
                ),
            },
            {
                "ruleforge/domain.py": ("RuleForge domain contracts shared by every policy module.",),
                "ruleforge/evaluator.py": ("Comparison and decision evaluation for RuleForge rules.",),
                "ruleforge/registry.py": ("Rule registry primitives for RuleForge policy definitions.",),
                "ruleforge/policy_catalog.py": ("Versioned operational policy profiles used by RuleForge rule modules.",),
            },
        ),
        _plan(
            "ruleforge-markets",
            "worker-03",
            "worktrees/ruleforge-worker-03",
            "codex/ruleforge-worker-03",
            "Add the markets approved_market rule: market_code must be one of north, west, or central and rejection uses MARKETS_CODE_DENIED.",
            ("ruleforge/rules/markets.py", "tests/test_markets_rule.py"),
            "tests/test_markets_rule.py",
            ("ruleforge/evaluator.py", "ruleforge/registry.py", "ruleforge/errors.py", "ruleforge/rules/base.py"),
            ("use RuleRegistry", "return a Decision"),
            {
                "ruleforge/evaluator.py": (
                    "evaluate_definition(definition, expected, input) returns Decision",
                    "missing or failed comparison rejects with error_code and one Evidence item",
                    "The evaluator owns the configured comparator semantics",
                ),
                "ruleforge/registry.py": (
                    "RuleRegistry.register(definition, expected) stores it under domain:name:source_field; get(key) returns (definition, expected)",
                ),
                "ruleforge/rules/base.py": (
                    "definition(domain, name, source_field, comparator, error_code) returns RuleDefinition",
                ),
            },
            {
                "ruleforge/evaluator.py": ("Comparison and decision evaluation for RuleForge rules.",),
                "ruleforge/registry.py": ("Rule registry primitives for RuleForge policy definitions.",),
                "ruleforge/errors.py": ("Error contracts for invalid RuleForge policy definitions.",),
                "ruleforge/rules/base.py": ("Shared RuleForge policy definition helpers.",),
            },
        ),
        _plan(
            "ruleforge-assurance",
            "worker-04",
            "worktrees/ruleforge-worker-04",
            "codex/ruleforge-worker-04",
            "Add the assurance rule from policy_catalog profile assurance.minimum_assurance; preserve its field, comparator, error code, and expected value.",
            ("ruleforge/rules/assurance.py", "tests/test_assurance_rule.py"),
            "tests/test_assurance_rule.py",
            (
                "ruleforge/domain.py",
                "ruleforge/registry.py",
                "ruleforge/rules/base.py",
                "ruleforge/service.py",
                "ruleforge/policy_catalog.py",
            ),
            ("use RuleRegistry", "return a Decision"),
            {
                "ruleforge/domain.py": (
                    "NormalizedInput is NormalizedInput(values, subject_id, request_id)",
                    "Decision: allowed has code None",
                ),
                "ruleforge/registry.py": (
                    "RuleRegistry.register(definition, expected) stores it under domain:name:source_field; get(key) returns (definition, expected)",
                ),
                "ruleforge/rules/base.py": (
                    "definition(domain, name, source_field, comparator, error_code) returns RuleDefinition",
                ),
                "ruleforge/service.py": (
                    "PolicyService.evaluate(payload, subject_id, request_id) -> normalize_payload(payload, subject_id, request_id) -> evaluate_all(self.registry.all(), data)",
                ),
                "ruleforge/policy_catalog.py": (
                    "assurance.minimum_assurance -> assurance, minimum_assurance, assurance_score, greater_than, ASSURANCE_SCORE_DENIED, 95.0",
                ),
            },
            {
                "ruleforge/domain.py": ("RuleForge domain contracts shared by every policy module.",),
                "ruleforge/registry.py": ("Rule registry primitives for RuleForge policy definitions.",),
                "ruleforge/rules/base.py": ("Shared RuleForge policy definition helpers.",),
                "ruleforge/service.py": ("Service facade for evaluating RuleForge policy registries.",),
                "ruleforge/policy_catalog.py": ("Versioned operational policy profiles used by RuleForge rule modules.",),
            },
        ),
    )


def _growth_worker_plans() -> tuple[WorkerPlan, ...]:
    """Return four new tasks that extend the V23 RuleForge baseline."""

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
    return (
        _plan(
            "ruleforge-billing", "worker-01", "worktrees/ruleforge-worker-01", "codex/ruleforge-worker-01",
            "Add the billing minimum_monthly_spend rule from policy_catalog profile billing.minimum_monthly_spend; preserve every profile field.",
            ("ruleforge/rules/billing.py", "tests/test_billing_rule.py"), "tests/test_billing_rule.py",
            ("ruleforge/domain.py", "ruleforge/evaluator.py", "ruleforge/policy_catalog.py"), ("use RuleRegistry", "return a Decision"),
            {"ruleforge/domain.py": domain, "ruleforge/evaluator.py": evaluator,
             "ruleforge/policy_catalog.py": ("billing.minimum_monthly_spend -> billing, minimum_monthly_spend, monthly_spend, at_least, BILLING_SPEND_DENIED, 5000.0",)},
            {"ruleforge/domain.py": ("RuleForge domain contracts shared by every policy module.",), "ruleforge/evaluator.py": ("Comparison and decision evaluation for RuleForge rules.",), "ruleforge/policy_catalog.py": ("Versioned operational policy profiles used by RuleForge rule modules.",)},
        ),
        _plan(
            "ruleforge-retention", "worker-02", "worktrees/ruleforge-worker-02", "codex/ruleforge-worker-02",
            "Add the retention minimum_age_days rule from policy_catalog profile retention.minimum_age_days; preserve every profile field.",
            ("ruleforge/rules/retention.py", "tests/test_retention_rule.py"), "tests/test_retention_rule.py",
            ("ruleforge/domain.py", "ruleforge/evaluator.py", "ruleforge/registry.py", "ruleforge/policy_catalog.py"), ("use RuleRegistry", "return a Decision"),
            {"ruleforge/domain.py": domain, "ruleforge/evaluator.py": evaluator, "ruleforge/registry.py": registry,
             "ruleforge/policy_catalog.py": ("retention.minimum_age_days -> retention, minimum_age_days, age_days, at_least, RETENTION_AGE_DENIED, 365.0",)},
            {"ruleforge/domain.py": ("RuleForge domain contracts shared by every policy module.",), "ruleforge/evaluator.py": ("Comparison and decision evaluation for RuleForge rules.",), "ruleforge/registry.py": ("Rule registry primitives for RuleForge policy definitions.",), "ruleforge/policy_catalog.py": ("Versioned operational policy profiles used by RuleForge rule modules.",)},
        ),
        _plan(
            "ruleforge-identity", "worker-03", "worktrees/ruleforge-worker-03", "codex/ruleforge-worker-03",
            "Add the identity required_region rule from policy_catalog profile identity.required_region; preserve every profile field.",
            ("ruleforge/rules/identity.py", "tests/test_identity_rule.py"), "tests/test_identity_rule.py",
            ("ruleforge/evaluator.py", "ruleforge/registry.py", "ruleforge/errors.py", "ruleforge/rules/base.py", "ruleforge/policy_catalog.py"), ("use RuleRegistry", "return a Decision"),
            {"ruleforge/evaluator.py": evaluator, "ruleforge/registry.py": registry, "ruleforge/rules/base.py": base,
             "ruleforge/policy_catalog.py": ("identity.required_region -> identity, required_region, region, one_of, IDENTITY_REGION_DENIED, us, ca, gb",)},
            {"ruleforge/evaluator.py": ("Comparison and decision evaluation for RuleForge rules.",), "ruleforge/registry.py": ("Rule registry primitives for RuleForge policy definitions.",), "ruleforge/errors.py": ("Error contracts for invalid RuleForge policy definitions.",), "ruleforge/rules/base.py": ("Shared RuleForge policy definition helpers.",), "ruleforge/policy_catalog.py": ("Versioned operational policy profiles used by RuleForge rule modules.",)},
        ),
        _plan(
            "ruleforge-fulfillment", "worker-04", "worktrees/ruleforge-worker-04", "codex/ruleforge-worker-04",
            "Add the fulfillment minimum_dispatch_score rule from policy_catalog profile fulfillment.minimum_dispatch_score; preserve every profile field.",
            ("ruleforge/rules/fulfillment.py", "tests/test_fulfillment_rule.py"), "tests/test_fulfillment_rule.py",
            ("ruleforge/domain.py", "ruleforge/registry.py", "ruleforge/rules/base.py", "ruleforge/service.py", "ruleforge/policy_catalog.py"), ("use RuleRegistry", "return a Decision"),
            {"ruleforge/domain.py": domain, "ruleforge/registry.py": registry, "ruleforge/rules/base.py": base,
             "ruleforge/service.py": ("PolicyService.evaluate(payload, subject_id, request_id) -> normalize_payload(payload, subject_id, request_id) -> evaluate_all(self.registry.all(), data)",),
             "ruleforge/policy_catalog.py": ("fulfillment.minimum_dispatch_score -> fulfillment, minimum_dispatch_score, dispatch_score, at_least, FULFILLMENT_SCORE_DENIED, 80.0",)},
            {"ruleforge/domain.py": ("RuleForge domain contracts shared by every policy module.",), "ruleforge/registry.py": ("Rule registry primitives for RuleForge policy definitions.",), "ruleforge/rules/base.py": ("Shared RuleForge policy definition helpers.",), "ruleforge/service.py": ("Service facade for evaluating RuleForge policy registries.",), "ruleforge/policy_catalog.py": ("Versioned operational policy profiles used by RuleForge rule modules.",)},
        ),
    )


def build_overlap_ledger(
    plans: Iterable[WorkerPlan], manifest_hash: str, terra_plans: Iterable[TerraPlan] | None = None
) -> tuple[OverlapLedgerEntry, ...]:
    """Build immutable entries for paths read by at least two workers."""

    validated = _validate_plans(plans)
    if not isinstance(manifest_hash, str) or not manifest_hash:
        raise ValueError("manifest_hash must be a non-empty string")
    rendered = tuple(fixture_terra_plan(plan) for plan in validated) if terra_plans is None else tuple(terra_plans)
    if not all(isinstance(plan, TerraPlan) for plan in rendered):
        raise ValueError("terra_plans must contain only TerraPlan records")
    task_to_plan = {plan.task_id: plan for plan in rendered}
    expected_tasks = {plan.task_id for plan in validated}
    if len(task_to_plan) != len(rendered) or set(task_to_plan) != expected_tasks:
        raise ValueError("terra plan task ids must exactly match the frozen task contracts")
    for worker in validated:
        produced = task_to_plan[worker.task_id]
        if produced.worker_id != worker.worker_id:
            raise ValueError("terra plan worker id does not match the frozen task contract")

    by_path: dict[str, list[WorkerPlan]] = {}
    for plan in validated:
        for path in plan.initial_read_paths:
            by_path.setdefault(path, []).append(plan)

    # Balance raw-source ownership across the genuinely overlapping tasks.  A
    # stable "first reader owns everything" rule made one worker own the large
    # catalog plus two smaller overlaps, while peers timed out waiting for it.
    # Least-assigned eligible reader keeps ownership deterministic without
    # inventing a common task or changing any worker's natural read set.
    owner_load = {plan.worker_id: 0 for plan in validated}
    entries: list[OverlapLedgerEntry] = []
    for path in sorted(by_path):
        participants = sorted(by_path[path], key=lambda plan: plan.worker_id)
        if len(participants) < 2:
            continue
        owner = min(participants, key=lambda plan: (owner_load[plan.worker_id], plan.worker_id))
        owner_load[owner.worker_id] += 1
        plan_steps = tuple((plan.worker_id, task_to_plan[plan.task_id].plan_steps) for plan in participants)
        requirement_payload = {
            "canonical_path": path,
            "participants": [
                {
                    "worker_id": plan.worker_id,
                    "task_id": plan.task_id,
                    "plan_steps": task_to_plan[plan.task_id].plan_steps,
                    "source_facts": task_to_plan[plan.task_id].source_facts,
                    "plan_fact_anchors": plan.plan_facts_for(path),
                    "required_source_facts": plan.source_facts_for(path),
                }
                for plan in participants
            ],
        }
        requirements_hash = _digest(requirement_payload)
        brief_id = "brief-" + _digest(
            {
                "manifest_hash": manifest_hash,
                "canonical_path": path,
                "requirements_hash": requirements_hash,
            }
        )
        entries.append(
            OverlapLedgerEntry(
                brief_id=brief_id,
                canonical_path=path,
                manifest_hash=manifest_hash,
                requirements_hash=requirements_hash,
                plan_steps=plan_steps,
                source_owner=owner.worker_id,
                peer_workers=tuple(plan.worker_id for plan in participants if plan != owner),
                required_facts=tuple(
                    sorted(
                        {
                            fact
                            for plan in participants
                            for fact in plan.source_facts_for(path)
                        }
                    )
                ),
            )
        )
    return tuple(entries)


__all__ = [
    "OverlapLedgerEntry",
    "TerraPlan",
    "fixture_terra_plan",
    "WorkerPlan",
    "build_overlap_ledger",
    "freeze_worker_plans",
    "manifest_sha256",
]
