"""Frozen integer-only claim predicates for the RRCv2 exact-template microbenchmark."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

ANALYZER_VERSION = "rrcv2-exact-analyzer-v3"
WORKLOAD_AUTHORITY_SHA256 = "5349930d2e0e21ab9512c265b2dca4b4140d6044567550c2e85a94c65598185c"
ARMS = ("baseline", "cheap_alone", "cascade", "rrc_cold", "rrc_warm")
COMPARATORS = ("cheap_alone", "cascade", "rrc_cold")
REPLICATES = ("r01", "r02", "r03", "r04")
ROW_FIELDS = frozenset(
    {
        "arm",
        "branch",
        "cell_id",
        "claim_cost_nano",
        "claim_cost_evidence_sha256",
        "economic_valid",
        "escalated",
        "hidden_oracle_passed",
        "infrastructure_valid",
        "oracle_solved",
        "order_sha256",
        "position",
        "public_accepted",
        "replicate_id",
        "run_id",
        "owner_scope",
        "task_id",
        "terminal_score_count",
        "terminal_score_id",
    }
)
CLAIM_PREDICATES = {
    "comparators": list(COMPARATORS),
    "head_positions_inclusive": [1, 10],
    "per_replicate": {
        "minimum_non_escalated_reuse_hits": 18,
        "parity_margin_tasks": 1,
        "require_all_arms_nonzero_solved": True,
        "require_endpoint_warm_cps_below_each_comparator": True,
        "require_tail_warm_cps_below_each_comparator": True,
        "require_warm_tail_cps_below_warm_head": True,
    },
    "pooled": {
        "minimum_non_escalated_reuse_hits": 72,
        "parity_margin_tasks": 4,
        "require_all_arms_nonzero_solved": True,
        "require_endpoint_warm_cps_below_each_comparator": True,
        "require_tail_warm_cps_below_each_comparator": True,
        "require_warm_tail_cps_below_warm_head": True,
    },
    "replicate_count": 4,
    "required_row_fields": sorted(ROW_FIELDS),
    "strict_cost_per_solved": True,
    "tail_positions_inclusive": [21, 30],
    "tasks_per_arm": 30,
    "v": 3,
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _order_sha256(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(rows)).hexdigest()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _workload_authority_sha256(workload: dict[str, Any]) -> str:
    expected_keys = {
        "benchmark_scope",
        "economic_evidence",
        "families",
        "hit_claim",
        "master_seed",
        "orders",
        "prime_economic_claim",
        "profile",
        "protocol",
        "tasks",
        "v",
    }
    if set(workload) != expected_keys or not isinstance(workload.get("protocol"), dict):
        raise ValueError("workload fields do not match the frozen authority schema")
    projection = json.loads(_canonical(workload))
    del projection["protocol"]["analyzer"]
    del projection["protocol"]["workload_authority_sha256"]
    return _sha256(projection)


def _cost_solved(rows: list[dict[str, Any]]) -> tuple[int, int]:
    return sum(row["claim_cost_nano"] for row in rows), sum(
        row["oracle_solved"] is True for row in rows
    )


def _strictly_cheaper(left: tuple[int, int], right: tuple[int, int]) -> bool:
    left_cost, left_solved = left
    right_cost, right_solved = right
    return (
        left_solved > 0 and right_solved > 0 and left_cost * right_solved < right_cost * left_solved
    )


def _validate_orders(workload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    protocol = workload.get("protocol")
    if (
        not isinstance(protocol, dict)
        or protocol.get("workload_authority_sha256") != WORKLOAD_AUTHORITY_SHA256
        or _workload_authority_sha256(workload) != WORKLOAD_AUTHORITY_SHA256
    ):
        raise ValueError("workload does not match the frozen authority digest")
    orders = workload.get("orders")
    tasks = workload.get("tasks")
    if (
        not isinstance(orders, dict)
        or set(orders) != set(REPLICATES)
        or not isinstance(tasks, list)
    ):
        raise ValueError("workload must provide the four frozen orders")
    task_ids = [row.get("task_id") for row in tasks if isinstance(row, dict)]
    if len(task_ids) != 30 or any(not isinstance(item, str) for item in task_ids):
        raise ValueError("workload must provide 30 task identities")
    expected_set = set(task_ids)
    if len(expected_set) != 30:
        raise ValueError("workload task identities must be unique")
    result: dict[str, list[dict[str, Any]]] = {}
    for replicate_id in REPLICATES:
        rows = orders[replicate_id]
        if (
            not isinstance(rows, list)
            or len(rows) != 30
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise ValueError("each order must contain 30 rows")
        ordered_ids = [row.get("task_id") for row in rows]
        positions = [row.get("position") for row in rows]
        if set(ordered_ids) != expected_set or positions != list(range(1, 31)):
            raise ValueError("order rows must be a positioned task bijection")
        result[replicate_id] = rows
    return result


def _validate_arm_rows(
    rows: object,
    *,
    arm: str,
    replicate_id: str,
    order_rows: list[dict[str, Any]],
    order_sha256: str,
    run_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) != 30:
        raise ValueError("each arm requires exactly 30 task rows")
    for position, (row, order_row) in enumerate(zip(rows, order_rows, strict=True), 1):
        if not isinstance(row, dict) or set(row) != ROW_FIELDS:
            raise ValueError("task result fields do not match the frozen analyzer row")
        cost = row["claim_cost_nano"]
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            raise ValueError("claim_cost_nano must be a nonnegative integer")
        booleans = (
            row["economic_valid"],
            row["escalated"],
            row["hidden_oracle_passed"],
            row["infrastructure_valid"],
            row["oracle_solved"],
            row["public_accepted"],
        )
        if any(not isinstance(value, bool) for value in booleans):
            raise ValueError("row validity fields must be boolean")
        score_count = row["terminal_score_count"]
        if (
            row["economic_valid"] is not True
            or row["infrastructure_valid"] is not True
            or isinstance(score_count, bool)
            or score_count != 1
        ):
            raise ValueError("every claim row needs valid economics and one terminal score")
        if row["oracle_solved"] is not (
            row["public_accepted"] is True and row["hidden_oracle_passed"] is True
        ):
            raise ValueError("oracle_solved must equal public acceptance plus hidden pass")
        owner_scope = (
            "run-"
            + _sha256(
                {
                    "arm": arm,
                    "kind": "benchmark-owner",
                    "replicate_id": replicate_id,
                    "run_id": run_id,
                    "v": 1,
                }
            )[:32]
        )
        if (
            row["replicate_id"] != replicate_id
            or row["run_id"] != run_id
            or row["arm"] != arm
            or row["cell_id"] != f"{run_id}:{replicate_id}:{arm}"
            or row["owner_scope"] != owner_scope
            or row["task_id"] != order_row["task_id"]
            or isinstance(row["position"], bool)
            or row["position"] != position
            or row["order_sha256"] != order_sha256
        ):
            raise ValueError("task result identity does not match its frozen cell/order")
        branch = row["branch"]
        direct_branches = {
            "baseline": {"BASELINE"},
            "cheap_alone": {"CHEAP_ALONE"},
            "cascade": {"CHEAP", "FALLBACK"},
            "rrc_cold": {"MISS"},
        }
        if arm == "rrc_warm":
            expected_branch = order_row.get("expected_branch")
            prior_reference = order_row.get("prior_reference")
            if expected_branch == "MISS" and prior_reference is None:
                allowed_branches = {"MISS"}
            elif expected_branch == "EXACT" and isinstance(prior_reference, str):
                allowed_branches = {"MISS", "REUSE"}
            else:
                raise ValueError("manifest branch feasibility is malformed")
        else:
            allowed_branches = direct_branches[arm]
        if branch not in allowed_branches:
            raise ValueError("branch is impossible for this arm/task/order")
        score_id = row["terminal_score_id"]
        expected_score_id = _sha256(
            {
                "arm": arm,
                "kind": "terminal-score",
                "position": position,
                "replicate_id": replicate_id,
                "run_id": run_id,
                "task_id": order_row["task_id"],
                "v": 1,
            }
        )
        evidence_sha = row["claim_cost_evidence_sha256"]
        expected_cost_evidence_sha = _sha256(
            {
                "arm": arm,
                "claim_cost_nano": cost,
                "kind": "claim-cost-row",
                "position": position,
                "replicate_id": replicate_id,
                "run_id": run_id,
                "task_id": order_row["task_id"],
                "v": 1,
            }
        )
        if score_id != expected_score_id or evidence_sha != expected_cost_evidence_sha:
            raise ValueError("score/cost evidence identity is not canonical")
    return rows


def evaluate_replicate(
    replicate: dict[str, Any], order_rows: list[dict[str, Any]]
) -> dict[str, bool]:
    if set(replicate) != {"arms", "order_sha256", "replicate_id", "run_id"}:
        raise ValueError("replicate fields do not match the frozen analyzer input")
    replicate_id = replicate["replicate_id"]
    run_id = replicate["run_id"]
    if (
        replicate_id not in REPLICATES
        or not isinstance(replicate["arms"], dict)
        or not isinstance(run_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", run_id) is None
    ):
        raise ValueError("invalid replicate identity or arms")
    expected_order_sha = _order_sha256(order_rows)
    if replicate["order_sha256"] != expected_order_sha or set(replicate["arms"]) != set(ARMS):
        raise ValueError("replicate order hash or arm set mismatch")
    arms = {
        arm: _validate_arm_rows(
            replicate["arms"][arm],
            arm=arm,
            replicate_id=replicate_id,
            order_rows=order_rows,
            order_sha256=expected_order_sha,
            run_id=run_id,
        )
        for arm in ARMS
    }
    solved = {arm: _cost_solved(rows)[1] for arm, rows in arms.items()}
    warm = arms["rrc_warm"]
    hit_count = sum(
        row["oracle_solved"] is True and row["branch"] == "REUSE" and row["escalated"] is False
        for row in warm
    )
    result = {
        "all_arms_solve": all(value > 0 for value in solved.values()),
        "hit_gate": hit_count >= 18,
        "parity": solved["rrc_warm"] >= solved["baseline"] - 1,
        "warm_head_solved": _cost_solved(warm[:10])[1] > 0,
        "warm_tail_solved": _cost_solved(warm[20:])[1] > 0,
    }
    for comparator in COMPARATORS:
        result[f"endpoint_vs_{comparator}"] = _strictly_cheaper(
            _cost_solved(warm), _cost_solved(arms[comparator])
        )
        result[f"tail_vs_{comparator}"] = _strictly_cheaper(
            _cost_solved(warm[20:]), _cost_solved(arms[comparator][20:])
        )
        result[f"{comparator}_tail_solved"] = _cost_solved(arms[comparator][20:])[1] > 0
    result["warm_tail_below_head"] = _strictly_cheaper(
        _cost_solved(warm[20:]), _cost_solved(warm[:10])
    )
    return result


def claim_passes(replicates: list[dict[str, Any]], workload: dict[str, Any]) -> bool:
    if not isinstance(replicates, list) or len(replicates) != 4:
        return False
    try:
        orders = _validate_orders(workload)
        by_id = {
            replicate["replicate_id"]: replicate
            for replicate in replicates
            if isinstance(replicate, dict) and isinstance(replicate.get("replicate_id"), str)
        }
        if set(by_id) != set(REPLICATES) or len(by_id) != 4:
            return False
        if len({replicate["run_id"] for replicate in by_id.values()}) != 1:
            return False
        outcomes = [evaluate_replicate(by_id[item], orders[item]) for item in REPLICATES]
        score_ids = [
            row["terminal_score_id"]
            for item in REPLICATES
            for arm in ARMS
            for row in by_id[item]["arms"][arm]
        ]
        if len(score_ids) != len(set(score_ids)):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    if not all(all(row.values()) for row in outcomes):
        return False
    arms_by_replicate = [by_id[item]["arms"] for item in REPLICATES]
    pooled = {arm: [task for arms in arms_by_replicate for task in arms[arm]] for arm in ARMS}
    pooled_solved = {arm: _cost_solved(rows)[1] for arm, rows in pooled.items()}
    pooled_warm = pooled["rrc_warm"]
    pooled_hits = sum(
        row["oracle_solved"] is True and row["branch"] == "REUSE" and row["escalated"] is False
        for row in pooled_warm
    )
    heads = {arm: [row for arms in arms_by_replicate for row in arms[arm][:10]] for arm in ARMS}
    tails = {arm: [row for arms in arms_by_replicate for row in arms[arm][20:]] for arm in ARMS}
    if not (
        all(value > 0 for value in pooled_solved.values())
        and pooled_hits >= 72
        and pooled_solved["rrc_warm"] >= pooled_solved["baseline"] - 4
        and _cost_solved(heads["rrc_warm"])[1] > 0
        and _cost_solved(tails["rrc_warm"])[1] > 0
        and _strictly_cheaper(_cost_solved(tails["rrc_warm"]), _cost_solved(heads["rrc_warm"]))
    ):
        return False
    return all(
        _cost_solved(tails[comparator])[1] > 0
        and _strictly_cheaper(_cost_solved(pooled_warm), _cost_solved(pooled[comparator]))
        and _strictly_cheaper(_cost_solved(tails["rrc_warm"]), _cost_solved(tails[comparator]))
        for comparator in COMPARATORS
    )


__all__ = [
    "ANALYZER_VERSION",
    "ARMS",
    "CLAIM_PREDICATES",
    "COMPARATORS",
    "REPLICATES",
    "ROW_FIELDS",
    "WORKLOAD_AUTHORITY_SHA256",
    "claim_passes",
    "evaluate_replicate",
]
