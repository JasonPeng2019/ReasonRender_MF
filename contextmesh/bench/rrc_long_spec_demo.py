#!/usr/bin/env python3
"""Run long-spec RuleForge experiments against a persistent benchmark app.

The live launcher adds EverOS, Lane B, ContextMesh, and OpenCode around this
deterministic fixture.  This module deliberately keeps the codebase and the
four repeated task instances inspectable before any provider call is made.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rrc.orchestrator_contract import OrchestratorTask

BENCH_ROOT = Path(__file__).resolve().parent
CANONICAL_WORKSPACE = BENCH_ROOT / "ruleforge-app"


SLOT_NAMES = (
    "domain",
    "source_field",
    "rule_name",
    "comparator",
    "error_code",
    "expected_value",
)
CASE_SHAPE = (
    "Add a {domain} policy rule named {rule_name} that reads {source_field}, "
    "uses {comparator} against configured value {expected_value}, and emits {error_code}."
)
CORE_TASK_VALUES = (
    {
        "domain": "security",
        "source_field": "access_level",
        "rule_name": "required_access_level",
        "comparator": "equals",
        "error_code": "SECURITY_ACCESS_DENIED",
        "expected_value": "'internal'",
    },
    {
        "domain": "limits",
        "source_field": "daily_requests",
        "rule_name": "minimum_daily_requests",
        "comparator": "at_least",
        "error_code": "LIMITS_REQUEST_DENIED",
        "expected_value": "100.0",
    },
    {
        "domain": "markets",
        "source_field": "market_code",
        "rule_name": "approved_market",
        "comparator": "one_of",
        "error_code": "MARKETS_CODE_DENIED",
        "expected_value": "['north', 'west', 'central']",
    },
    {
        "domain": "assurance",
        "source_field": "assurance_score",
        "rule_name": "minimum_assurance",
        "comparator": "greater_than",
        "error_code": "ASSURANCE_SCORE_DENIED",
        "expected_value": "95.0",
    },
)
GROWTH_TASK_VALUES = (
    {
        "domain": "billing",
        "source_field": "monthly_spend",
        "rule_name": "minimum_monthly_spend",
        "comparator": "at_least",
        "error_code": "BILLING_SPEND_DENIED",
        "expected_value": "5000.0",
    },
    {
        "domain": "retention",
        "source_field": "age_days",
        "rule_name": "minimum_age_days",
        "comparator": "at_least",
        "error_code": "RETENTION_AGE_DENIED",
        "expected_value": "365.0",
    },
    {
        "domain": "identity",
        "source_field": "region",
        "rule_name": "required_region",
        "comparator": "one_of",
        "error_code": "IDENTITY_REGION_DENIED",
        "expected_value": "['us', 'ca', 'gb']",
    },
    {
        "domain": "fulfillment",
        "source_field": "dispatch_score",
        "rule_name": "minimum_dispatch_score",
        "comparator": "at_least",
        "error_code": "FULFILLMENT_SCORE_DENIED",
        "expected_value": "80.0",
    },
)
STAGE3_TASK_VALUES = (
    {
        "domain": "compliance",
        "source_field": "retention_class",
        "rule_name": "required_retention_class",
        "comparator": "equals",
        "error_code": "COMPLIANCE_RETENTION_DENIED",
        "expected_value": "'regulated'",
    },
    {
        "domain": "delivery",
        "source_field": "delivery_score",
        "rule_name": "minimum_delivery_score",
        "comparator": "at_least",
        "error_code": "DELIVERY_SCORE_DENIED",
        "expected_value": "90.0",
    },
    {
        "domain": "eligibility",
        "source_field": "eligibility_tier",
        "rule_name": "approved_eligibility_tier",
        "comparator": "one_of",
        "error_code": "ELIGIBILITY_TIER_DENIED",
        "expected_value": "['gold', 'platinum']",
    },
    {
        "domain": "risk",
        "source_field": "risk_score",
        "rule_name": "minimum_risk_score",
        "comparator": "at_least",
        "error_code": "RISK_SCORE_DENIED",
        "expected_value": "25.0",
    },
)
TASK_COHORTS = {"core": CORE_TASK_VALUES, "growth": GROWTH_TASK_VALUES, "stage3": STAGE3_TASK_VALUES}
# Preserve the existing one-cohort fixture API for all historical tests.
TASK_VALUES = CORE_TASK_VALUES
ARCHITECTURE_PATHS = (
    "ruleforge/domain.py",
    "ruleforge/normalizer.py",
    "ruleforge/registry.py",
    "ruleforge/evaluator.py",
    "ruleforge/errors.py",
    "ruleforge/service.py",
    "ruleforge/rules/base.py",
    "ruleforge/policy_catalog.py",
)
ORACLE_TESTS = "\n".join(
    (
        "def test_accept(): pass",
        "def test_reject(): pass",
        "def test_missing(): pass",
        "def test_malformed(): pass",
    )
)


def _task_text(values: Mapping[str, str]) -> str:
    return "\n".join(
        (
            CASE_SHAPE.format(**values),
            "Implement it as a RuleForge rules package module with focused tests.",
            'RRC_SHAPE: {"arity":6,"arg_types":["str","str","str","str","str","str"],'
            '"fields":["domain","source_field","rule_name","comparator","error_code","expected_value"]}',
            "RRC_SLOT_VALUES: " + json.dumps(values, sort_keys=True, separators=(",", ":")),
        )
    )


def task_values(cohort: str = "core") -> tuple[dict[str, str], ...]:
    """Return one fixed four-task cohort for a direct 1+4 comparison."""

    if cohort.startswith("operational-"):
        try:
            group = int(cohort.removeprefix("operational-"))
        except ValueError as error:
            raise ValueError(f"unknown RuleForge cohort: {cohort}") from error
        if group < 1 or group > 40:
            raise ValueError(f"unknown RuleForge cohort: {cohort}")
        comparators = ("equals", "at_least", "greater_than", "one_of")
        values: list[dict[str, str]] = []
        for number in range((group - 1) * 4 + 1, (group - 1) * 4 + 5):
            comparator = comparators[(number - 1) % len(comparators)]
            if comparator == "equals":
                expected = repr(f"tier-{number % 9}")
            elif comparator == "one_of":
                expected = repr([f"region-{number % 7}", f"region-{(number + 3) % 7}", "global"])
            else:
                expected = f"{float((number % 25) + 10):.1f}"
            values.append(
                {
                    # A synthetic task domain keeps the four cache bindings
                    # distinct; the real RuleForge profile remains the shared
                    # ``operational.control_NNN`` catalog record.
                    "domain": f"operational_{number:03d}",
                    "source_field": f"control_value_{number:03d}",
                    "rule_name": f"control_{number:03d}",
                    "comparator": comparator,
                    "error_code": f"OPERATIONAL_CONTROL_{number:03d}_DENIED",
                    "expected_value": expected,
                }
            )
        return tuple(values)
    try:
        return TASK_COHORTS[cohort]
    except KeyError as error:
        raise ValueError(f"unknown RuleForge cohort: {cohort}") from error


def generic_packet() -> dict[str, Any]:
    """Return the dense reusable planner artifact accepted by the detailed policy."""

    plan_steps = [
        "Create the {domain} rule module in its owned path and resolve the declared policy profile as the complete configuration.",
        "Construct the rule definition from that profile and register it with the configured expected value through RuleRegistry.",
        "Provide Decision evaluation so matching {source_field} is allowed and missing or non-matching values are denied with {error_code}.",
        "Add focused coverage for registration, {comparator} behavior, allowed and denied decisions, then run the named acceptance command.",
    ]
    constraints = [
        "Do not couple the {domain} rule to HTTP, storage, or caller request objects.",
        "Do not bypass NormalizedInput or manufacture a Decision outside the evaluator.",
        "Keep {rule_name} deterministic and keep the public RuleRegistry API stable.",
    ]
    edges = [
        "Treat a missing {source_field} as an explicit rejected Decision, not an exception.",
        "Treat unsupported {comparator} configuration as a clear configuration error.",
    ]
    acceptance = [
        "A registered {rule_name} reads normalized {source_field} values through the evaluator.",
        "A matching value produces an allow Decision with rule metadata.",
        "A non-matching value produces {error_code} and useful evidence.",
        "A missing value produces a stable rejected Decision without raising.",
    ]
    return {
        "signature": "def evaluate_{rule_name}(input: NormalizedInput) -> Decision",
        "slot_names": list(SLOT_NAMES),
        "plan": {
            "steps": plan_steps,
            "invariants": [
                "Every rule returns a Decision and never a bare boolean.",
                "The registry owns rule selection; the service facade owns orchestration only.",
                "All task-specific names stay represented by declared placeholders until local rendering.",
            ],
            "edges": edges,
            "constraints": constraints,
        },
        "specification": (
        "Extend RuleForge with a reusable {domain} policy module. The module owns "
        "{rule_name}, reads {source_field}, delegates comparison to {comparator} using "
        "{expected_value}, and uses {error_code} for a rejected decision. Preserve the "
        "coordinator-provided source facts and task requirements; they are not implementation code."
        ),
        "acceptance": acceptance,
        "non_goals": [
            "Do not add persistence, network calls, framework handlers, or a second registry.",
            "Do not change existing rule behavior while introducing {rule_name}.",
        ],
        "write_paths": [
            "ruleforge/rules/{domain}.py",
            "tests/test_{domain}_rule.py",
        ],
        "read_first": [
            "coordinator packet source-fact anchors",
            "arm-authorized source route",
            "named acceptance command",
        ],
    }


def _render(value: Any, slots: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for name in SLOT_NAMES:
            value = value.replace("{" + name + "}", slots[name])
        return value
    if isinstance(value, list):
        return [_render(item, slots) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, slots) for key, item in value.items()}
    return value


def render_cached_packet(template: Mapping[str, Any], slots: Mapping[str, str]) -> dict[str, Any]:
    """Render one selected binding in memory; no rendered packet is cached."""

    return _render(template, slots)


_CACHE_RENDERER = '''"""Load one generic packet and render one task binding in memory."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SLOT_NAMES = (
    "domain",
    "source_field",
    "rule_name",
    "comparator",
    "error_code",
    "expected_value",
)


def _render(value: Any, slots: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for name in SLOT_NAMES:
            value = value.replace("{" + name + "}", slots[name])
        return value
    if isinstance(value, list):
        return [_render(item, slots) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, slots) for key, item in value.items()}
    return value


def load_rendered_packet(workspace: str | Path, task_id: str) -> dict[str, Any]:
    cache = Path(workspace) / ".rrc-cache"
    template = json.loads((cache / "template.json").read_text(encoding="utf-8"))
    index = json.loads((cache / "bindings.json").read_text(encoding="utf-8"))
    return _render(template, index["bindings"][task_id])
'''


def _packet_reuse_savings(
    generic: Mapping[str, Any], bindings: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    """Count template storage text, not provider model usage."""

    def text_tokens(value: str) -> int:
        return len(value.split())

    generic_text = json.dumps(generic, indent=2, sort_keys=True)
    binding_index = {"slot_names": list(SLOT_NAMES), "bindings": bindings}
    rendered_texts = [
        json.dumps(render_cached_packet(generic, slots), indent=2, sort_keys=True)
        for slots in bindings.values()
    ]
    generic_tokens = text_tokens(generic_text)
    rendered_tokens = [text_tokens(text) for text in rendered_texts]
    full_rendered_total = sum(rendered_tokens)
    stored_payload_tokens = generic_tokens + text_tokens(
        json.dumps(binding_index, indent=2, sort_keys=True)
    )
    return {
        "metric": "template storage saved",
        "method": "whitespace-delimited tokens in prepared JSON text",
        "full_rendered_packet_tokens": rendered_tokens,
        "full_rendered_packet_total_tokens": full_rendered_total,
        "template_text_tokens": generic_tokens,
        "binding_index_text_tokens": stored_payload_tokens - generic_tokens,
        "stored_template_and_binding_tokens": stored_payload_tokens,
        "saved_tokens": max(0, full_rendered_total - stored_payload_tokens),
    }


def _write_cache(
    workspace: Path,
    packet: Mapping[str, Any],
    bindings: Mapping[str, Mapping[str, str]],
) -> None:
    """Store one generic packet and one separate task binding index."""

    _write(workspace, ".rrc-cache/template.json", json.dumps(packet, indent=2, sort_keys=True))
    _write(
        workspace,
        ".rrc-cache/bindings.json",
        json.dumps(
            {"slot_names": list(SLOT_NAMES), "bindings": bindings},
            indent=2,
            sort_keys=True,
        ),
    )
    _write(workspace, "rrc_cache.py", _CACHE_RENDERER)


def _write(workspace: Path, relative: str, text: str) -> None:
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def _policy_catalog_source() -> str:
    """Build a realistic, source-heavy catalogue used by several policy tasks.

    The catalogue is actual RuleForge data rather than padding: each row is a
    registrable policy profile and the three measured tasks consume distinct
    named rows.  A sizeable catalogue is normal for a policy service, and is a
    representative case for ContextMesh because workers need small, exact
    slices of the same source file.
    """

    special = (
        ("security.required_access_level", "security", "required_access_level", "access_level", "equals", "SECURITY_ACCESS_DENIED", "internal", "identity-control"),
        ("limits.minimum_daily_requests", "limits", "minimum_daily_requests", "daily_requests", "at_least", "LIMITS_REQUEST_DENIED", 100.0, "rate-control"),
        ("assurance.minimum_assurance", "assurance", "minimum_assurance", "assurance_score", "greater_than", "ASSURANCE_SCORE_DENIED", 95.0, "assurance-control"),
        ("billing.minimum_monthly_spend", "billing", "minimum_monthly_spend", "monthly_spend", "at_least", "BILLING_SPEND_DENIED", 5000.0, "billing-control"),
        ("retention.minimum_age_days", "retention", "minimum_age_days", "age_days", "at_least", "RETENTION_AGE_DENIED", 365.0, "retention-control"),
        ("identity.required_region", "identity", "required_region", "region", "one_of", "IDENTITY_REGION_DENIED", ("us", "ca", "gb"), "identity-control"),
        ("fulfillment.minimum_dispatch_score", "fulfillment", "minimum_dispatch_score", "dispatch_score", "at_least", "FULFILLMENT_SCORE_DENIED", 80.0, "fulfillment-control"),
        ("compliance.required_retention_class", "compliance", "required_retention_class", "retention_class", "equals", "COMPLIANCE_RETENTION_DENIED", "regulated", "compliance-control"),
        ("delivery.minimum_delivery_score", "delivery", "minimum_delivery_score", "delivery_score", "at_least", "DELIVERY_SCORE_DENIED", 90.0, "delivery-control"),
        ("eligibility.approved_eligibility_tier", "eligibility", "approved_eligibility_tier", "eligibility_tier", "one_of", ("gold", "platinum"), "ELIGIBILITY_TIER_DENIED", "eligibility-control"),
        ("risk.minimum_risk_score", "risk", "minimum_risk_score", "risk_score", "at_least", 25.0, "RISK_SCORE_DENIED", "risk-control"),
    )
    synthetic = []
    comparators = ("equals", "at_least", "greater_than", "one_of")
    for number in range(1, 481):
        comparator = comparators[(number - 1) % len(comparators)]
        expected: object
        if comparator == "equals":
            expected = f"tier-{number % 9}"
        elif comparator == "one_of":
            expected = (f"region-{number % 7}", f"region-{(number + 3) % 7}", "global")
        else:
            expected = float((number % 25) + 10)
        synthetic.append(
            (
                f"operational.control_{number:03d}",
                "operational",
                f"control_{number:03d}",
                f"control_value_{number:03d}",
                comparator,
                f"OPERATIONAL_CONTROL_{number:03d}_DENIED",
                expected,
                f"operational-group-{(number - 1) // 12 + 1:02d}",
            )
        )
    rows = []
    for key, domain, name, field, comparator, error_code, expected, family in (*special, *synthetic):
        rows.append(
            "    "
            + repr(key)
            + ": PolicyProfile("
            + f"key={key!r}, domain={domain!r}, name={name!r}, source_field={field!r}, "
            + f"comparator={comparator!r}, error_code={error_code!r}, expected={expected!r}, family={family!r}),"
        )
    return "\n".join(
        (
            '\"\"\"Versioned operational policy profiles used by RuleForge rule modules.\"\"\"',
            "from __future__ import annotations",
            "",
            "from dataclasses import dataclass",
            "",
            "",
            "@dataclass(frozen=True)",
            "class PolicyProfile:",
            "    key: str",
            "    domain: str",
            "    name: str",
            "    source_field: str",
            "    comparator: str",
            "    error_code: str",
            "    expected: object",
            "    family: str",
            "",
            "",
            "POLICY_CATALOG: dict[str, PolicyProfile] = {",
            *rows,
            "}",
            "",
            "",
            "def profile(key: str) -> PolicyProfile:",
            "    try:",
            "        return POLICY_CATALOG[key]",
            "    except KeyError as error:",
            "        raise ValueError(f'unknown policy profile: {key}') from error",
            "",
            "",
            "def profile_keys(family: str) -> tuple[str, ...]:",
            "    return tuple(key for key, value in POLICY_CATALOG.items() if value.family == family)",
        )
    )


def _write_ruleforge(workspace: Path) -> None:
    _write(workspace, "ruleforge/policy_catalog.py", _policy_catalog_source())
    _write(
        workspace,
        "ruleforge/rollout.py",
        '''"""Versioned rollout contract consumed by staged policy work."""
from __future__ import annotations


STAGE_REVISION = "stage-00"


def rollout_revision() -> str:
    return STAGE_REVISION
''',
    )
    _write(
        workspace,
        "ruleforge/domain.py",
        '''"""RuleForge domain contracts shared by every policy module."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class NormalizedInput:
    values: Mapping[str, object]
    subject_id: str
    request_id: str

    def value(self, field: str) -> object | None:
        return self.values.get(field)


@dataclass(frozen=True)
class Evidence:
    rule_name: str
    field: str
    observed: object | None
    comparator: str
    detail: str


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str | None
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)

    @classmethod
    def allow(cls, evidence: Evidence) -> "Decision":
        return cls(allowed=True, code=None, evidence=(evidence,))

    @classmethod
    def reject(cls, code: str, evidence: Evidence) -> "Decision":
        return cls(allowed=False, code=code, evidence=(evidence,))


@dataclass(frozen=True)
class RuleDefinition:
    name: str
    domain: str
    source_field: str
    comparator: str
    error_code: str

    def description(self) -> str:
        return f"{self.domain}:{self.name} reads {self.source_field} with {self.comparator}"


def require_text(value: object | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    text = value.strip()
    return text or None


def decision_summary(decision: Decision) -> str:
    if decision.allowed:
        return "allowed"
    return decision.code or "rejected"


def merge_evidence(*decisions: Decision) -> tuple[Evidence, ...]:
    return tuple(item for decision in decisions for item in decision.evidence)


def stable_rule_key(definition: RuleDefinition) -> str:
    return ":".join((definition.domain, definition.name, definition.source_field))


def is_terminal(decision: Decision) -> bool:
    return not decision.allowed


def empty_input(subject_id: str, request_id: str) -> NormalizedInput:
    return NormalizedInput(values={}, subject_id=subject_id, request_id=request_id)
''',
    )
    _write(
        workspace,
        "ruleforge/normalizer.py",
        '''"""Input normalization keeps policy modules independent from request transport."""
from __future__ import annotations

from collections.abc import Mapping

from .domain import NormalizedInput


def normalize_payload(payload: Mapping[str, object], subject_id: str, request_id: str) -> NormalizedInput:
    return NormalizedInput(values={key: normalize_value(value) for key, value in payload.items()}, subject_id=subject_id, request_id=request_id)


def normalize_value(value: object) -> object:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, list):
        return tuple(normalize_value(item) for item in value)
    if isinstance(value, dict):
        return {key: normalize_value(item) for key, item in value.items()}
    return value


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


def lower_text(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return normalize_text(value).lower()


def number(value: object | None) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def sequence(value: object | None) -> tuple[object, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return ()


def has_field(data: NormalizedInput, field: str) -> bool:
    return data.value(field) is not None


def required_text(data: NormalizedInput, field: str) -> str | None:
    return lower_text(data.value(field))


def required_number(data: NormalizedInput, field: str) -> float | None:
    return number(data.value(field))


def normalized_fields(data: NormalizedInput) -> tuple[str, ...]:
    return tuple(sorted(data.values))


def diagnostic_value(data: NormalizedInput, field: str) -> str:
    value = data.value(field)
    if value is None:
        return "<missing>"
    return str(value)
''',
    )
    _write(
        workspace,
        "ruleforge/evaluator.py",
        '''"""Evaluation orchestration for registered RuleForge policies."""
from __future__ import annotations

from collections.abc import Iterable

from .domain import Decision, Evidence, NormalizedInput, RuleDefinition, merge_evidence
from .normalizer import lower_text, number


def compare(observed: object | None, comparator: str, expected: object) -> bool:
    if comparator == "one_of":
        return lower_text(observed) in {str(item).lower() for item in expected}
    if comparator == "equals":
        return observed == expected
    if comparator == "at_least":
        value = number(observed)
        return value is not None and value >= float(expected)
    if comparator == "greater_than":
        value = number(observed)
        return value is not None and value > float(expected)
    raise ValueError(f"unsupported comparator: {comparator}")


def evaluate_definition(definition: RuleDefinition, expected: object, data: NormalizedInput) -> Decision:
    observed = data.value(definition.source_field)
    evidence = Evidence(definition.name, definition.source_field, observed, definition.comparator, "comparison evaluated")
    if observed is None:
        return Decision.reject(definition.error_code, Evidence(definition.name, definition.source_field, None, definition.comparator, "required value is missing"))
    if compare(observed, definition.comparator, expected):
        return Decision.allow(evidence)
    return Decision.reject(definition.error_code, evidence)


def evaluate_all(rules: Iterable[tuple[RuleDefinition, object]], data: NormalizedInput) -> Decision:
    decisions = tuple(evaluate_definition(definition, expected, data) for definition, expected in rules)
    rejected = next((decision for decision in decisions if not decision.allowed), None)
    if rejected is not None:
        return Decision(False, rejected.code, merge_evidence(*decisions))
    return Decision(True, None, merge_evidence(*decisions))


def explain(decision: Decision) -> list[str]:
    return [f"{item.rule_name}:{item.field}:{item.detail}" for item in decision.evidence]


def has_error(decision: Decision, code: str) -> bool:
    return decision.code == code


def is_allowed(decision: Decision) -> bool:
    return decision.allowed


def rule_count(rules: Iterable[tuple[RuleDefinition, object]]) -> int:
    return sum(1 for _ in rules)


def rejected_fields(decision: Decision) -> tuple[str, ...]:
    return tuple(item.field for item in decision.evidence if decision.code)


def requires_manual_review(decision: Decision) -> bool:
    return decision.code == "RISK_MANUAL_REVIEW_REQUIRED"
''',
    )
    _write(
        workspace,
        "ruleforge/errors.py",
        '''"""Typed errors reserved for configuration and registry boundaries."""


class RuleForgeError(Exception):
    pass


class DuplicateRuleError(RuleForgeError):
    pass


class UnknownRuleError(RuleForgeError):
    pass


class InvalidRuleConfiguration(RuleForgeError):
    pass
''',
    )
    _write(
        workspace,
        "ruleforge/registry.py",
        '''"""The registry is the only source of truth for policy definitions."""
from __future__ import annotations

from .domain import RuleDefinition, stable_rule_key
from .errors import DuplicateRuleError, UnknownRuleError


class RuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, tuple[RuleDefinition, object]] = {}

    def register(self, definition: RuleDefinition, expected: object) -> None:
        key = stable_rule_key(definition)
        if key in self._rules:
            raise DuplicateRuleError(key)
        self._rules[key] = (definition, expected)

    def get(self, key: str) -> tuple[RuleDefinition, object]:
        try:
            return self._rules[key]
        except KeyError as error:
            raise UnknownRuleError(key) from error

    def all(self) -> tuple[tuple[RuleDefinition, object], ...]:
        return tuple(self._rules.values())
''',
    )
    _write(
        workspace,
        "ruleforge/service.py",
        '''"""Service facade used by callers after transport normalization."""
from __future__ import annotations

from collections.abc import Mapping

from .evaluator import evaluate_all
from .normalizer import normalize_payload
from .registry import RuleRegistry


class PolicyService:
    def __init__(self, registry: RuleRegistry) -> None:
        self.registry = registry

    def evaluate(self, payload: Mapping[str, object], subject_id: str, request_id: str):
        data = normalize_payload(payload, subject_id, request_id)
        return evaluate_all(self.registry.all(), data)
''',
    )
    _write(workspace, "ruleforge/rules/__init__.py", '"""Rule modules register definitions with RuleRegistry."""')
    _write(
        workspace,
        "ruleforge/rules/base.py",
        '''"""Shared helpers for policy modules."""
from __future__ import annotations

from ruleforge.domain import RuleDefinition


def definition(domain: str, name: str, source_field: str, comparator: str, error_code: str) -> RuleDefinition:
    return RuleDefinition(name=name, domain=domain, source_field=source_field, comparator=comparator, error_code=error_code)
''',
    )
    _write(
        workspace,
        "tests/test_ruleforge.py",
        '''from ruleforge.domain import RuleDefinition
from ruleforge.registry import RuleRegistry
from ruleforge.service import PolicyService


def test_registry_policy_service_rejects_missing_field() -> None:
    registry = RuleRegistry()
    registry.register(RuleDefinition("minimum_invoice", "billing", "invoice_total", "at_least", "BILLING_MINIMUM_NOT_MET"), 100)
    decision = PolicyService(registry).evaluate({}, "subject", "request")
    assert decision.allowed is False
    assert decision.code == "BILLING_MINIMUM_NOT_MET"
''',
    )


def _canonical_workspace() -> Path:
    """Return the persistent app that each isolated arm clones.

    The first experiment initializes it.  Later successful arms are promoted
    explicitly, so experiments accumulate product code instead of silently
    throwing their changes away with a temporary run directory.
    """

    if not (CANONICAL_WORKSPACE / "ruleforge" / "domain.py").is_file():
        _write_ruleforge(CANONICAL_WORKSPACE)
    else:
        catalog = _policy_catalog_source()
        catalog_path = CANONICAL_WORKSPACE / "ruleforge" / "policy_catalog.py"
        if not catalog_path.is_file() or catalog_path.read_text(encoding="utf-8") != catalog.strip() + "\n":
            _write(CANONICAL_WORKSPACE, "ruleforge/policy_catalog.py", catalog)
        rollout_path = CANONICAL_WORKSPACE / "ruleforge" / "rollout.py"
        if not rollout_path.is_file():
            _write(
                CANONICAL_WORKSPACE,
                "ruleforge/rollout.py",
                '''"""Versioned rollout contract consumed by staged policy work."""
from __future__ import annotations


STAGE_REVISION = "stage-00"


def rollout_revision() -> str:
    return STAGE_REVISION
''',
            )
    return CANONICAL_WORKSPACE


def _copy_product_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git", ".rrc-cache", "__pycache__", "*.pyc"),
    )


def promote_workspace(source: Path) -> Path:
    """Promote a tested arm's product code into the persistent benchmark app."""

    if not (source / "ruleforge").is_dir() or not (source / "tests").is_dir():
        raise ValueError(f"not a RuleForge workspace: {source}")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=source, check=False)
    if result.returncode:
        raise RuntimeError("refusing to promote a workspace whose focused tests fail")
    canonical = _canonical_workspace()
    _copy_product_tree(source, canonical)
    return canonical


def materialize(output: Path, cohort: str = "core", stage_id: str | None = None) -> dict[str, Any]:
    """Clone the persistent app, then add this run's packet cache and manifest."""

    output.mkdir(parents=True, exist_ok=True)
    workspace = output / "workspace"
    _copy_product_tree(_canonical_workspace(), workspace)
    packet = generic_packet()
    values_for_cohort = task_values(cohort)
    def task_id(values: Mapping[str, str]) -> str:
        prefix = f"{stage_id}-" if stage_id else ""
        return f"{prefix}ruleforge-{values['domain']}"

    bindings = {task_id(values): values for values in values_for_cohort}
    _write_cache(workspace, packet, bindings)
    manifest: dict[str, Any] = {
        "workspace": str(workspace),
        "cohort": cohort,
        "stage_id": stage_id,
        "case_shape": CASE_SHAPE,
        "slot_names": list(SLOT_NAMES),
        "generic_packet": packet,
        "cache": {
            "template": ".rrc-cache/template.json",
            "bindings": ".rrc-cache/bindings.json",
            "renderer": "rrc_cache.py",
        },
        "tasks": [
            {
                "task_id": task_id(values),
                "case_shape": CASE_SHAPE,
                "slot_values": values,
                "text": _task_text(values),
            }
            for values in values_for_cohort
        ],
        "packet_reuse_savings": _packet_reuse_savings(packet, bindings),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _runtime_task(task: Mapping[str, Any]) -> "OrchestratorTask":
    from rrc.orchestrator_contract import OrchestratorTask

    return OrchestratorTask(
        task_id=str(task["task_id"]),
        family="ruleforge-policy",
        text=str(task["text"]),
        case_shape=CASE_SHAPE,
        slot_values=task["slot_values"],
        oracle_tests=ORACLE_TESTS,
    )


def _lane_b(output: Path, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Run the local typed packet proof against real EverOS and SQLite."""

    from rrc.everos import EverOSClient
    from rrc.orchestrator_contract import PlanSpecPacket
    from rrc.orchestrator_runtime import OrchestratorRuntime
    from rrc.store import SQLiteTemplateStore

    class RecordingEverOS(EverOSClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[dict[str, Any]] = []

        def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append({"path": path, "payload": payload})
            return super()._post(path, payload)

    planner_calls: list[dict[str, str]] = []
    worker_calls: list[dict[str, str]] = []
    packet = PlanSpecPacket.from_dict(manifest["generic_packet"])
    packet_json = json.dumps(packet.to_dict(), separators=(",", ":"), sort_keys=True)
    everos = RecordingEverOS()

    def planner(prompt: str, model: str) -> tuple[str, int]:
        planner_calls.append({"model": model, "prompt": prompt})
        return packet_json, 0

    def worker(prompt: str, model: str) -> tuple[str, int]:
        worker_calls.append({"model": model, "prompt": prompt})
        return "local-rendered-worker-prompt", 0

    runtime = OrchestratorRuntime(
        planner,
        "deterministic-local-planner",
        worker,
        "deterministic-local-worker",
        SQLiteTemplateStore(output / "lane_b.sqlite3"),
        everos,
    )
    outcomes: list[dict[str, Any]] = []
    for raw_task in manifest["tasks"]:
        result = runtime.run(_runtime_task(raw_task))
        outcomes.append(
            {
                "task_id": raw_task["task_id"],
                "branch": "reuse" if result.hit else "miss",
                "external_ref": result.external_ref,
                "planner_tokens": result.planner_tokens,
                "worker_tokens": result.worker_tokens,
                "profile": result.profile,
                "packet_token_budget": result.packet_token_budget,
            }
        )
        if not result.hit:
            everos.wait_for_index()

    refs = [str(outcome["external_ref"]) for outcome in outcomes]
    values = [value for task in manifest["tasks"] for value in task["slot_values"].values()]
    generic = json.dumps(manifest["generic_packet"], sort_keys=True)
    privacy_checks = {
        "keyword_search": all(
            call["path"] != "/api/v2/memory/search"
            or (
                call["payload"].get("query") == CASE_SHAPE
                and call["payload"].get("method") == "keyword"
                and isinstance(call["payload"].get("filters"), Mapping)
                and isinstance(call["payload"]["filters"].get("session_id"), str)
            )
            for call in everos.calls
        ),
        "stable_shape_index": all(
            call["path"] != "/api/v2/memory/add"
            or (
                isinstance(call["payload"].get("messages"), list)
                and bool(call["payload"]["messages"])
                and json.loads(call["payload"]["messages"][0]["content"]).get("case_shape") == CASE_SHAPE
            )
            for call in everos.calls
        ),
        "opaque_refs": all(
            call["path"] != "/api/v2/memory/add"
            or json.loads(call["payload"]["messages"][0]["content"]).get("external_ref") in refs
            for call in everos.calls
        ),
        "no_flush": all(call["path"] != "/api/v2/memory/flush" for call in everos.calls),
        "no_slot_values_or_packet": all(
            value not in json.dumps(call, sort_keys=True) for call in everos.calls for value in values
        )
        and generic not in json.dumps(everos.calls, sort_keys=True),
    }
    privacy_passed = all(privacy_checks.values())
    if not (outcomes and outcomes[0]["branch"] == "miss" and all(item["branch"] == "reuse" for item in outcomes[1:])):
        raise AssertionError("Lane B did not produce one miss followed by three reuses")
    if len(planner_calls) != 1 or len(set(refs)) != 1 or not privacy_passed:
        raise AssertionError("Lane B proof failed")

    # These are the exact packets parsed, validated, stored, retrieved, and
    # locally rendered by OrchestratorRuntime.  Feeding them to the measured
    # coding arm keeps the Lane B proof and cache-arm work on one data path.
    rendered_packets = [
        json.loads(call["prompt"].removeprefix("Product worker input:\n"))["packet"]
        for call in worker_calls
    ]

    (output / "lane_b_proof.json").write_text(
        json.dumps(
            {
                "outcomes": outcomes,
                "external_refs": refs,
                "planner_calls": planner_calls,
                "worker_calls": worker_calls,
                "everos_calls": everos.calls,
                "planner_token_savings_measured": False,
                "privacy_assertion": {"passed": privacy_passed, "checks": privacy_checks},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return rendered_packets


def _write_prompts(
    output: Path, manifest: Mapping[str, Any], packet_level: str = "structural"
) -> None:
    if packet_level not in {"structural", "verbatim"}:
        raise ValueError(f"unknown packet level: {packet_level}")
    tasks = "\n\n".join(task["text"] for task in manifest["tasks"])
    reads = "\n".join(f"- {path}" for path in ARCHITECTURE_PATHS)
    worker_patch_template = '''
MANDATORY TWO-FILE PATCH CONTRACT. After the complete JSON packet, render this
template with that packet's six binding fields and give it verbatim to the
worker. The worker must apply it as its first action; it must not read examples.

ruleforge/rules/{domain}.py
from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition

def register(registry: RuleRegistry) -> None:
    registry.register(definition(domain="{domain}", name="{rule_name}", source_field="{source_field}", comparator="{comparator}", error_code="{error_code}"), expected={expected_value})

tests/test_{domain}_rule.py
from ruleforge.domain import NormalizedInput
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.{domain} import register

def _registered():
    registry = RuleRegistry(); register(registry)
    return registry.get("{domain}:{rule_name}:{source_field}")

def test_registration():
    definition, expected = _registered()
    assert definition.name == "{rule_name}" and expected == {expected_value}

def test_allowed():
    definition, expected = _registered()
    value = expected[0] if isinstance(expected, list) else expected
    assert evaluate_definition(definition, expected, NormalizedInput({"{source_field}": value}, "s", "r")).allowed

def test_rejected():
    definition, expected = _registered()
    decision = evaluate_definition(definition, expected, NormalizedInput({"{source_field}": object()}, "s", "r"))
    assert not decision.allowed and decision.code == "{error_code}"

def test_missing():
    definition, expected = _registered()
    decision = evaluate_definition(definition, expected, NormalizedInput({}, "s", "r"))
    assert not decision.allowed and decision.code == "{error_code}"
'''
    common = f"""Modify this materialized RuleForge codebase.

Use exactly four independent cheap worker subagents total, with
subagent_type=worker and exactly one worker assigned to each policy task.
Launch all four before any edits. The orchestrator makes no edits. Each worker
must independently read every
shared source file below before editing. These are independent repeated reads:
no worker may rely on another worker's read or evidence. Each worker's result
must include this short named list:
READ_EVIDENCE[<assigned task>]: ruleforge/domain.py, ruleforge/normalizer.py,
ruleforge/registry.py, ruleforge/evaluator.py, ruleforge/errors.py,
ruleforge/service.py, ruleforge/rules/base.py
The orchestrator must verify exactly four complete READ_EVIDENCE lists, one per
task, before merging any worker changes. Keep the same RuleForge policy terms:
normalized input, RuleRegistry, evaluator, typed Decision, comparator, error
code, focused tests, acceptance, non-goals, and declared write paths.
Finish within the fixed 12 orchestrator turns; each worker has four turns.
Spend turns on implementation and focused tests, not a prose recap.
The shared session ceiling is exactly 600,000 measured provider tokens across
the Pro orchestrator, all Flash workers, and any ContextMesh summarizer. Use
the allowance to finish and pass focused tests; do not abandon a task merely
because the remaining allowance is small.

Shared source reads:
{reads}

Policy tasks (the same four tasks must be implemented in both arms):
{tasks}
"""
    baseline = common + """

Baseline arm cache behavior:
For every task separately, the expensive orchestrator must reread every
architecture file listed above and reconstruct the full long RuleForge policy
specification live before launching that task's worker. It must derive the
signature, plan, invariants, edges, constraints, acceptance, non-goals, write
paths, and read-first decisions from those reads. Do not reuse that reconstruction
across tasks. The exact complete derived specification must be included in that
task's worker handoff, so the worker receives its full plan and implementation
contract. Dispatch that handoff directly after construction; do not emit a
separate prose recap before the worker call.
Do not read or use .rrc-cache; it is reserved for the cached arm.
"""
    task_ids = "\n".join(f"- {task['task_id']}" for task in manifest["tasks"])
    packet_mode = (
        "This is the structural packet arm: it contains signatures, invariants, "
        "constraints, acceptance, non-goals, write paths, and contract summaries, "
        "but no literal source body to transcribe."
        if packet_level == "structural"
        else "This is the opt-in full-verbatim floor: it additionally carries the literal "
        "two-file patch contract below and must never be reported as the headline arm."
    )
    verbatim_contract = worker_patch_template if packet_level == "verbatim" else ""
    cached = f"""Modify this materialized RuleForge codebase within 12 orchestrator turns.

Use exactly four cheap worker subagents total: one per task ID, launched before
any edits. Each worker has four turns and receives its complete rendered packet
verbatim in the handoff (not a cache path, abbreviated summary, or reference).
The shared session ceiling is exactly 600,000 measured provider tokens across
the Pro orchestrator, all Flash workers, and any ContextMesh summarizer. Use
the allowance to finish and pass focused tests; do not abandon a task merely
because the remaining allowance is small.

RRCv2 cache contract:
1. Load .rrc-cache/template.json once.
2. Load .rrc-cache/bindings.json once.
3. For every task ID below, call load_rendered_packet(workspace, task_id) from
   rrc_cache.py, print the resulting complete JSON packet in that worker's
   handoff, and give the worker that exact packet.
4. Each worker follows its packet's plan, specification, write paths,
   read-first list, acceptance criteria, and non-goals. The packet never contains source bodies, imports, function bodies, or a prewritten test; use only the source route authorized by the arm.

The six binding fields (domain, source_field, rule_name, comparator,
error_code, expected_value) are authoritative task metadata. They deterministically render the
module path, test path, signature, behavior, expected configuration, and errors. Do not inspect source
to infer, second-guess, or recreate any field or architecture specification.
Do not rebuild or reread the long architecture specification.
After loading the two cache JSON files, neither the orchestrator nor workers
may invoke read, glob, grep, search, find, ls, or shell inspection against
ruleforge/ or tests/. Workers use their packet's complete contract directly:
their first tool action writes their two declared files, their second action
runs the focused pytest file, and any remaining actions only repair test
failures. The orchestrator dispatches all four rendered handoffs immediately
and does not inspect the repository.
Never write rendered packets back to .rrc-cache.

{packet_mode}

{verbatim_contract}

Task IDs:
{task_ids}
"""
    (output / "baseline_prompt.md").write_text(baseline.strip() + "\n", encoding="utf-8")
    (output / "cached_prompt.md").write_text(cached.strip() + "\n", encoding="utf-8")


def _preflight_live() -> None:
    repo = str(Path(__file__).resolve().parents[2])
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from rrc.everos import EverOSClient

    opencode_bin = os.environ.get("CONTEXTMESH_OPENCODE_BIN")
    if opencode_bin:
        if not Path(opencode_bin).is_file():
            raise SystemExit(f"live demo configured a missing OpenCode binary: {opencode_bin}")
    elif shutil.which("bun") is None:
        raise SystemExit("live demo requires Bun on PATH or CONTEXTMESH_OPENCODE_BIN")
    try:
        EverOSClient().wait_for_index(timeout=10.0)
    except Exception as error:
        raise SystemExit(f"live demo requires a healthy local EverOS at http://127.0.0.1:8000 ({error})") from error


def _run_live_bench(output: Path, runid: str, arm_timeout: int) -> None:
    bench = Path(__file__).resolve()
    meter = bench.parents[1] / "scripts" / "live_meter.py"
    common = [
        sys.executable,
        str(bench.parent / "run_bench.py"),
        "--runid",
        runid,
        "--workspace-template",
        str(output / "workspace"),
        "--timeout",
        str(arm_timeout),
    ]
    subprocess.run(
        [*common, "--arms", "a", "--no-warm", "--task-file", str(output / "baseline_prompt.md")],
        cwd=bench.parents[2],
        check=True,
    )
    subprocess.run(
        [*common, "--arms", "b", "--warm", "--task-file", str(output / "cached_prompt.md")],
        cwd=bench.parents[2],
        check=True,
    )
    subprocess.run([sys.executable, str(meter), "--runid", runid, "--once"], cwd=bench.parents[2], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="isolated demo output directory")
    parser.add_argument("--cohort", choices=tuple(TASK_COHORTS), default="core")
    parser.add_argument("--dry-run", action="store_true", help="materialize only; make no service or model calls")
    parser.add_argument("--live", action="store_true", help="run the real EverOS, ContextMesh, and OpenCode demo")
    parser.add_argument(
        "--prepare-tui",
        action="store_true",
        help="prepare real Lane B packets and prompts for the interactive three-arm TUI demo",
    )
    parser.add_argument("--runid", help="shared ContextMesh run id for --live")
    parser.add_argument(
        "--promote-from",
        type=Path,
        help="promote a passing arm workspace into the persistent RuleForge benchmark app",
    )
    parser.add_argument("--arm-timeout", type=int, default=600, help="bounded seconds per live OpenCode arm")
    parser.add_argument(
        "--packet-level",
        choices=("structural", "verbatim"),
        default="structural",
        help="structural is the primary packet; verbatim is an opt-in full-verbatim floor",
    )
    args = parser.parse_args()
    if args.live and not args.runid:
        parser.error("--live requires --runid")
    if args.promote_from:
        canonical = promote_workspace(args.promote_from)
        print(json.dumps({"promoted": str(canonical)}))
        return
    if args.live or args.prepare_tui:
        _preflight_live()
    manifest = materialize(args.out, args.cohort)
    print(json.dumps({"manifest": str(args.out / "manifest.json"), "tasks": len(manifest["tasks"])}))
    if args.prepare_tui:
        rendered_packets = _lane_b(args.out, manifest)
        _write_prompts(args.out, manifest, args.packet_level)
        print(json.dumps({"tui_ready": True, "packet_count": len(rendered_packets)}))
    elif args.live:
        rendered_packets = _lane_b(args.out, manifest)
        _write_prompts(args.out, manifest, args.packet_level)
        _run_live_bench(args.out, args.runid, args.arm_timeout)
    elif not args.dry_run:
        print("The live launcher owns service startup and model execution.")


if __name__ == "__main__":
    main()
