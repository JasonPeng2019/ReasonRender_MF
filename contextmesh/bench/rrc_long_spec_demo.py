#!/usr/bin/env python3
"""Materialize the long-spec RuleForge demo workspace.

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
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any



SLOT_NAMES = ("domain", "source_field", "rule_name", "comparator", "error_code")
CASE_SHAPE = (
    "Add a {domain} policy rule named {rule_name} that reads {source_field}, "
    "uses {comparator}, and emits {error_code}."
)
TASK_VALUES = (
    {
        "domain": "billing",
        "source_field": "invoice_total",
        "rule_name": "minimum_invoice",
        "comparator": "at_least",
        "error_code": "BILLING_MINIMUM_NOT_MET",
    },
    {
        "domain": "identity",
        "source_field": "email_domain",
        "rule_name": "approved_domain",
        "comparator": "one_of",
        "error_code": "IDENTITY_DOMAIN_DENIED",
    },
    {
        "domain": "fulfillment",
        "source_field": "shipment_country",
        "rule_name": "allowed_destination",
        "comparator": "equals",
        "error_code": "FULFILLMENT_DESTINATION_DENIED",
    },
    {
        "domain": "risk",
        "source_field": "transaction_score",
        "rule_name": "manual_review_threshold",
        "comparator": "greater_than",
        "error_code": "RISK_MANUAL_REVIEW_REQUIRED",
    },
)
ARCHITECTURE_PATHS = (
    "ruleforge/domain.py",
    "ruleforge/normalizer.py",
    "ruleforge/registry.py",
    "ruleforge/evaluator.py",
    "ruleforge/errors.py",
    "ruleforge/service.py",
    "ruleforge/rules/base.py",
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
            'RRC_SHAPE: {"arity":5,"arg_types":["str","str","str","str","str"],'
            '"fields":["domain","source_field","rule_name","comparator","error_code"]}',
            "RRC_SLOT_VALUES: " + json.dumps(values, sort_keys=True, separators=(",", ":")),
        )
    )


def generic_packet() -> dict[str, Any]:
    """Return the dense reusable planner artifact accepted by the detailed policy."""

    plan_steps = [
        "Read the shared RuleForge contracts before adding {rule_name}.",
        "Implement the {domain} rule through RuleRegistry and evaluate normalized {source_field} with shared {comparator} comparison.",
        "Return {error_code} with typed evidence for missing or rejected {source_field} values.",
        "Add focused tests for accepted, rejected, missing, and malformed {source_field} inputs.",
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
            "{rule_name}, reads {source_field}, delegates comparison to {comparator}, and "
            "uses {error_code} for a rejected decision. Follow the domain, normalizer, "
            "registry, evaluator, errors, service, base-rule, and policy-package contracts."
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
            "ruleforge/domain.py",
            "ruleforge/normalizer.py",
            "ruleforge/registry.py",
            "ruleforge/evaluator.py",
            "ruleforge/service.py",
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


def _write(workspace: Path, relative: str, text: str) -> None:
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def _write_ruleforge(workspace: Path) -> None:
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


def materialize(output: Path) -> dict[str, Any]:
    """Create the app and a stable generic-spec manifest under ``output``."""

    output.mkdir(parents=True, exist_ok=True)
    workspace = output / "workspace"
    _write_ruleforge(workspace)
    packet = generic_packet()
    rendered = [_render(packet, values) for values in TASK_VALUES]
    manifest: dict[str, Any] = {
        "workspace": str(workspace),
        "case_shape": CASE_SHAPE,
        "slot_names": list(SLOT_NAMES),
        "generic_packet": packet,
        "tasks": [
            {
                "task_id": f"ruleforge-{values['domain']}",
                "case_shape": CASE_SHAPE,
                "slot_values": values,
                "text": _task_text(values),
            }
            for values in TASK_VALUES
        ],
        "rendered_packets": rendered,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _runtime_task(task: Mapping[str, Any]) -> OrchestratorTask:
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
            )
            for call in everos.calls
        ),
        "stable_shape_index": all(
            call["path"] != "/api/v2/memory/add"
            or call["payload"]["messages"][0]["content"] == CASE_SHAPE
            for call in everos.calls
        ),
        "opaque_refs": all(
            call["path"] not in ("/api/v2/memory/add", "/api/v2/memory/flush")
            or call["payload"].get("external_ref") in refs
            for call in everos.calls
        ),
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
    output: Path,
    manifest: Mapping[str, Any],
    rendered_packets: list[dict[str, Any]] | None = None,
) -> None:
    tasks = "\n\n".join(task["text"] for task in manifest["tasks"])
    reads = "\n".join(f"- {path}" for path in ARCHITECTURE_PATHS)
    common = f"""Modify this materialized RuleForge codebase.

Use exactly one cheap worker subagent per policy task with subagent_type=worker,
launch the four workers in parallel, and have every worker explicitly read the
shared source files below before changing code. Keep the same RuleForge policy
terms: normalized input, RuleRegistry, evaluator, typed Decision, comparator,
error code, focused tests, acceptance, non-goals, and declared write paths.

Shared source reads:
{reads}

Policy tasks (the same four tasks must be implemented in both arms):
{tasks}
"""
    baseline = common + """

Baseline arm cache behavior:
For every task separately, force the expensive orchestrator to reread every
architecture file listed above and reconstruct the full long RuleForge policy
specification before launching that task's worker. The reconstructed spec must
repeat the complete signature, plan, invariants, edges, constraints,
specification, acceptance, non-goals, write paths, and read-first decisions.
Do not reuse that reconstruction across tasks.
"""
    rendered = json.dumps(rendered_packets or manifest["rendered_packets"], indent=2, sort_keys=True)
    cached = common + f"""

Cached arm cache behavior:
Use the locally rendered generic packet for the corresponding task below. Do
not rebuild or reread the long architecture specification in the orchestrator;
the packet is the cached controller input. Workers still explicitly read the
shared source files listed above and must implement only the declared task.

Four locally rendered generic packets:
{rendered}
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


def _run_live_bench(output: Path, runid: str) -> None:
    bench = Path(__file__).resolve()
    meter = bench.parents[1] / "scripts" / "live_meter.py"
    common = [
        sys.executable,
        str(bench.parent / "run_bench.py"),
        "--runid",
        runid,
        "--workspace-template",
        str(output / "workspace"),
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
    parser.add_argument("--dry-run", action="store_true", help="materialize only; make no service or model calls")
    parser.add_argument("--live", action="store_true", help="run the real EverOS, ContextMesh, and OpenCode demo")
    parser.add_argument("--runid", help="shared ContextMesh run id for --live")
    args = parser.parse_args()
    if args.live and not args.runid:
        parser.error("--live requires --runid")
    if args.live:
        _preflight_live()
    manifest = materialize(args.out)
    print(json.dumps({"manifest": str(args.out / "manifest.json"), "tasks": len(manifest["tasks"])}))
    if args.live:
        rendered_packets = _lane_b(args.out, manifest)
        _write_prompts(args.out, manifest, rendered_packets)
        _run_live_bench(args.out, args.runid)
    elif not args.dry_run:
        print("The live launcher owns service startup and model execution.")


if __name__ == "__main__":
    main()
