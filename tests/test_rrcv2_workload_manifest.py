from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import stat
import subprocess
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any

from contextmesh.bench.rrcv2_analyzer import (
    ANALYZER_VERSION,
    CLAIM_PREDICATES,
    WORKLOAD_AUTHORITY_SHA256,
    claim_passes,
    evaluate_replicate,
)
from rrc.policy import PolicyKind, validate_synthetic_source

REPO = Path(__file__).resolve().parents[1]
WORKLOAD = REPO / "contextmesh/bench/rrcv2_workload.json"
ORACLES = REPO / "contextmesh/bench/rrcv2_oracles.json"
MASTER_SEED = "rrcv2-exact-micro-v6-20260810"
REPLICATES = ("r01", "r02", "r03", "r04")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(path: Path) -> dict[str, Any]:
    raw = path.read_text()
    value = json.loads(raw)
    assert isinstance(value, dict)
    assert raw == _canonical(value) + "\n"
    return value


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _test_artifact(tests: list[str]) -> str:
    return _canonical({"tests": tests, "v": 1})


def test_workload_is_frozen_complete_balanced_and_separate() -> None:
    workload = _load(WORKLOAD)
    oracles = _load(ORACLES)
    assert workload["v"] == oracles["v"] == 6
    assert workload["profile"] == oracles["profile"] == "rrcv2_synthetic_v1"
    assert workload["master_seed"] == MASTER_SEED
    assert workload["benchmark_scope"] == "exact_template_amortization_microbenchmark"
    assert workload["prime_economic_claim"] is False
    tasks = workload["tasks"]
    hidden = oracles["tasks"]
    assert len(tasks) == len(hidden) == 30
    assert len({row["task_id"] for row in tasks}) == 30
    assert {row["task_id"] for row in hidden} == {row["task_id"] for row in tasks}
    assert Counter(row["family"] for row in tasks) == {
        "bounded_integer": 6,
        "discounted_total": 6,
        "normalize_token": 6,
        "parse_integer_list": 6,
        "select_status_names": 6,
    }
    families = {row["family"]: row for row in workload["families"]}
    assert set(families) == {row["family"] for row in tasks}
    for family, metadata in families.items():
        family_tasks = [row for row in tasks if row["family"] == family]
        assert metadata["task_ids"] == [row["task_id"] for row in family_tasks]
        assert {_canonical(row["shape"]) for row in family_tasks} == {_canonical(metadata["shape"])}
    assert len({row["task_text"] for row in tasks}) == 30
    assert len({row["starter_source"] for row in tasks}) == 30
    assert workload["hit_claim"] == {
        "denominator": 30,
        "minimum_hits": 18,
        "structural_opportunities": 25,
    }
    assert "reference_solution" not in WORKLOAD.read_text()
    assert "hidden_oracle_tests" not in WORKLOAD.read_text()


def test_economic_evidence_matches_frozen_rates_and_bounded_source_excerpts() -> None:
    economic = _load(WORKLOAD)["economic_evidence"]
    rate = economic["rate_payload"]
    evidence = economic["evidence"]
    assert _sha(_canonical(rate)) == economic["rate_payload_sha256"]
    assert (
        economic["rate_payload_sha256"]
        == "41479a47731582016733886d7162c1ad15446c1ba503578982989ca188c9dc28"
    )
    assert _sha(_canonical(evidence)) == economic["evidence_sha256"]
    assert evidence["rate_payload_sha256"] == economic["rate_payload_sha256"]
    assert evidence["request_aliases"] == ["fast", "priority"]
    assert evidence["expected_returned_tier"] == "priority"
    assert evidence["eligibility"] == {
        "above_max": "economic_invalid",
        "assembled_prompt_utf8_bytes_max": 131072,
        "authority": "pre_dispatch_utf8_bytes_plus_capability_proven_overhead",
        "capability_overhead_tokens_max": 8192,
        "conservative_input_token_upper_bound": 139264,
        "long_context_request_estimate_threshold_exclusive": 272000,
    }
    excerpt_root = REPO / ".generated/state/rrcv2-convergence/economic/source-excerpts"
    names = {
        "https://developers.openai.com/api/docs/models/gpt-5.6-luna": "gpt-5.6-luna.txt",
        "https://developers.openai.com/api/docs/pricing": "pricing.txt",
        "https://openai.com/api-fast-mode/": "fast-mode.txt",
    }
    assert [row["url"] for row in evidence["sources"]] == sorted(names)
    for row in evidence["sources"]:
        path = excerpt_root / names[row["url"]]
        raw = path.read_bytes()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert 0 < len(raw) <= 4096
        assert row["excerpt_bytes"] == len(raw)
        assert row["excerpt_sha256"] == hashlib.sha256(raw).hexdigest()


def test_nested_hashes_markers_policy_and_hidden_oracles_are_exact() -> None:
    workload = _load(WORKLOAD)
    hidden_by_id = {row["task_id"]: row for row in _load(ORACLES)["tasks"]}
    for task in workload["tasks"]:
        hidden = hidden_by_id[task["task_id"]]
        shape_marker = "RRC_SHAPE: " + _canonical(task["shape"])
        slots_marker = "RRC_SLOT_VALUES: " + _canonical(task["slot_values"])
        lines = task["task_text"].splitlines()
        assert lines[-2] == shape_marker
        assert lines[-1] == slots_marker
        assert task["task_text"].count("RRC_SHAPE:") == 1
        assert task["task_text"].count("RRC_SLOT_VALUES:") == 1
        assert not task["task_text"].endswith("\n")
        assert task["profile"] == "rrcv2_synthetic_v1"
        assert task["family"] == task["expected_family"]
        assert task["primary"] == task["slot_values"]["function"]
        assert list(task["shape"]["fields"]) == sorted(set(task["shape"]["fields"]))
        assert len(task["shape"]["arg_types"]) == task["shape"]["arity"]
        assert len(set(task["slot_values"].values())) == len(task["slot_values"])
        assert task["task_sha256"] == _sha(task["task_text"])
        assert task["source_sha256"] == _sha(task["starter_source"])
        assert task["public_tests_sha256"] == _sha(_test_artifact(task["public_tests"]))
        assert hidden["oracle_sha256"] == task["oracle_sha256"]
        assert hidden["oracle_sha256"] == _sha(_test_artifact(hidden["hidden_oracle_tests"]))
        assert hidden["reference_solution_sha256"] == _sha(hidden["reference_solution"])
        assert len(hidden["reference_solution"].splitlines()) >= 7
        assert len(hidden["mutation_probes"]) >= 2
        assert hidden["hidden_oracle_tests"]
        assert hidden["reference_solution"] not in task["starter_source"]
        prompt_surface = (
            task["task_text"] + task["starter_source"] + _test_artifact(task["public_tests"])
        )
        assert hidden["reference_solution_sha256"] not in prompt_surface
        assert hidden["oracle_sha256"] not in prompt_surface
        for oracle_source in hidden["hidden_oracle_tests"]:
            assert oracle_source not in prompt_surface
        validate_synthetic_source(hidden["reference_solution"], kind=PolicyKind.CANDIDATE)
        for test_source in (*task["public_tests"], *hidden["hidden_oracle_tests"]):
            validate_synthetic_source(test_source, kind=PolicyKind.TEST, target=task["primary"])


def _search_projection(task: dict[str, Any]) -> str:
    body = task["task_text"].split("\nRRC_SHAPE: ", 1)[0].casefold()
    values = [(name, value.casefold()) for name, value in task["slot_values"].items()]
    assert len({value for _, value in values}) == len(values)
    matches: list[tuple[int, int, str]] = []
    for name, value in sorted(values, key=lambda row: (-len(row[1].encode()), row[0])):
        start = 0
        while (index := body.find(value, start)) >= 0:
            matches.append((index, index + len(value), name))
            start = index + 1
    matches.sort()
    assert all(left[1] <= right[0] for left, right in zip(matches, matches[1:], strict=False))
    parts: list[str] = []
    cursor = 0
    for start, end, name in matches:
        parts.extend((body[cursor:start], f"slot_{name}"))
        cursor = end
    parts.append(body[cursor:])
    projected_body = "".join(parts).strip(" \t\n")
    for _, value in values:
        assert value not in projected_body.replace("slot_", "")
    return _canonical(
        {
            "body": projected_body,
            "family": task["family"],
            "shape": task["shape"],
            "slot_names": sorted(task["slot_values"]),
            "v": 1,
        }
    )


def _hashed_features(text: str) -> dict[int, int]:
    tokens = re.findall(r"[a-z0-9_]+", text)
    rows: list[tuple[bytes, tuple[str, ...]]] = [(b"u", (token,)) for token in tokens]
    rows.extend((b"b", pair) for pair in zip(tokens, tokens[1:], strict=False))
    result: dict[int, int] = {}
    for kind, values in rows:
        preimage = (
            b"rrcv2-hash-v1\0" + kind + b"\0" + b"\0".join(value.encode() for value in values)
        )
        digest = hashlib.sha256(preimage).digest()
        bucket = int.from_bytes(digest[:2], "big") % 512
        result[bucket] = result.get(bucket, 0) + (1 if digest[2] & 1 == 0 else -1)
    return result


def _rrf_top_three(
    query: str,
    candidates: list[str],
    projections: dict[str, str],
    by_id: dict[str, dict[str, Any]],
    hidden: dict[str, dict[str, Any]],
) -> list[str]:
    if not candidates:
        return []
    database = sqlite3.connect(":memory:")
    database.execute(
        "CREATE VIRTUAL TABLE docs USING fts5(task_id UNINDEXED, searchable, tokenize='unicode61 remove_diacritics 2')"
    )
    database.executemany(
        "INSERT INTO docs(task_id, searchable) VALUES (?, ?)",
        [(item, projections[item]) for item in candidates],
    )
    tokens = sorted(set(re.findall(r"[a-z0-9_]+", query)))
    expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    lexical_rows = database.execute(
        "SELECT task_id, bm25(docs) FROM docs WHERE docs MATCH ?", (expression,)
    ).fetchall()
    lexical_rows.sort(
        key=lambda row: (
            row[1],
            hidden[row[0]]["reference_external_ref"],
            by_id[row[0]]["task_sha256"],
        )
    )
    lexical = {row[0]: index for index, row in enumerate(lexical_rows[:22000], 1)}
    query_features = _hashed_features(query)
    query_norm = math.sqrt(sum(value * value for value in query_features.values()))
    hashed_rows: list[tuple[str, float]] = []
    for task_id in candidates:
        features = _hashed_features(projections[task_id])
        norm = math.sqrt(sum(value * value for value in features.values()))
        dot = sum(value * features.get(bucket, 0) for bucket, value in query_features.items())
        hashed_rows.append((task_id, dot / (query_norm * norm)))
    hashed_rows.sort(
        key=lambda row: (
            -row[1],
            hidden[row[0]]["reference_external_ref"],
            by_id[row[0]]["task_sha256"],
        )
    )
    hashed = {row[0]: index for index, row in enumerate(hashed_rows[:22000], 1)}
    fused: list[tuple[str, Fraction]] = []
    for task_id in set(lexical) | set(hashed):
        score = Fraction(0)
        if task_id in lexical:
            score += Fraction(61, 2 * (60 + lexical[task_id]))
        if task_id in hashed:
            score += Fraction(61, 2 * (60 + hashed[task_id]))
        if score >= Fraction(7, 20):
            fused.append((task_id, score))
    fused.sort(
        key=lambda row: (
            -row[1],
            hidden[row[0]]["reference_external_ref"],
            by_id[row[0]]["task_sha256"],
        )
    )
    return [row[0] for row in fused[:3]]


def test_exact_microbenchmark_projection_and_rrf_graph_reach_all_25_opportunities() -> None:
    workload = _load(WORKLOAD)
    by_id = {row["task_id"]: row for row in workload["tasks"]}
    hidden = {row["task_id"]: row for row in _load(ORACLES)["tasks"]}
    projections = {task_id: _search_projection(task) for task_id, task in by_id.items()}
    for family in {task["family"] for task in by_id.values()}:
        assert (
            len(
                {
                    projections[task_id]
                    for task_id, task in by_id.items()
                    if task["family"] == family
                }
            )
            == 1
        )
    for rows in workload["orders"].values():
        seen: list[str] = []
        opportunities = 0
        for row in rows:
            top_three = _rrf_top_three(
                projections[row["task_id"]], seen, projections, by_id, hidden
            )
            exact_top_three = [
                task_id
                for task_id in top_three
                if by_id[task_id]["shape"] == by_id[row["task_id"]]["shape"]
            ]
            if exact_top_three:
                opportunities += 1
                assert row["expected_branch"] == "EXACT"
                assert row["prior_reference"] in exact_top_three
            else:
                assert row["expected_branch"] == "MISS"
            seen.append(row["task_id"])
        assert opportunities == 25


def test_public_and_hidden_suites_kill_every_preregistered_policy_valid_mutant() -> None:
    workload = _load(WORKLOAD)
    by_id = {row["task_id"]: row for row in workload["tasks"]}
    for hidden in _load(ORACLES)["tasks"]:
        task = by_id[hidden["task_id"]]
        tests = (*task["public_tests"], *hidden["hidden_oracle_tests"])
        for mutant in hidden["mutation_probes"]:
            validate_synthetic_source(mutant, kind=PolicyKind.CANDIDATE)
            namespace: dict[str, object] = {}
            exec(compile(mutant, "mutant", "exec"), namespace)
            killed = False
            for source in tests:
                test_namespace = dict(namespace)
                exec(compile(source, "mutant-test", "exec"), test_namespace)
                test = next(
                    value for name, value in test_namespace.items() if name.startswith("test_")
                )
                try:
                    assert callable(test)
                    test()
                except Exception:
                    killed = True
                    break
            assert killed, f"surviving mutant for {hidden['task_id']}"


def test_machine_readable_five_arm_schedule_freezes_models_cells_and_analysis() -> None:
    workload = _load(WORKLOAD)
    protocol = workload["protocol"]
    assert protocol["v"] == 5
    assert protocol["arms"] == ["baseline", "cheap_alone", "cascade", "rrc_cold", "rrc_warm"]
    assert len(protocol["cell_schedule"]) == 20
    assert [row["sequence"] for row in protocol["cell_schedule"]] == list(range(1, 21))
    assert [row["schedule_key"] for row in protocol["cell_schedule"]] == sorted(
        row["schedule_key"] for row in protocol["cell_schedule"]
    )
    assert {(row["replicate_id"], row["arm"]) for row in protocol["cell_schedule"]} == {
        (replicate, arm) for replicate in REPLICATES for arm in protocol["arms"]
    }
    assert len({row["database_path"] for row in protocol["cell_schedule"]}) == 20
    for row in protocol["cell_schedule"]:
        assert (
            row["schedule_key"]
            == hashlib.sha256(
                (f"{MASTER_SEED}\0cell-order\0{row['replicate_id']}\0{row['arm']}").encode()
            ).hexdigest()
        )
        assert row["task_order_sha256"] == _sha(_canonical(workload["orders"][row["replicate_id"]]))
        assert row["database_path"] == f"cells/{row['replicate_id']}/{row['arm']}/state.sqlite3"
    assert _sha(_canonical(protocol["analysis"])) == protocol["analysis_sha256"]
    assert protocol["analysis"]["claim_scope"] == "exact_template_amortization_microbenchmark"
    assert protocol["analysis"]["predicates"] == CLAIM_PREDICATES
    assert protocol["workload_authority_sha256"] == WORKLOAD_AUTHORITY_SHA256
    authority_projection = json.loads(_canonical(workload))
    del authority_projection["protocol"]["analyzer"]
    del authority_projection["protocol"]["workload_authority_sha256"]
    assert _sha(_canonical(authority_projection)) == WORKLOAD_AUTHORITY_SHA256
    assert protocol["empty_seed"] == {
        "payload": {"bundles": [], "cases": [], "v": 1},
        "sha256": _sha(_canonical({"bundles": [], "cases": [], "v": 1})),
    }
    analyzer = protocol["analyzer"]
    analyzer_path = REPO / analyzer["path"]
    assert analyzer["version"] == ANALYZER_VERSION
    assert analyzer["entrypoint"] == "claim_passes(replicates,workload)"
    assert analyzer["sha256"] == hashlib.sha256(analyzer_path.read_bytes()).hexdigest()
    assert protocol["arm_stage_models"] == {
        "baseline": {"whole_task": "strong_baseline"},
        "cascade": {"fallback": "strong_baseline", "first": "small"},
        "cheap_alone": {"whole_task": "small"},
        "rrc_cold": {
            "fallback_spec": "strong_spec",
            "implement": "small",
            "independent_tests": "small",
            "repair": "small",
            "spec": "strong_spec",
        },
        "rrc_warm": {
            "fallback_spec": "strong_spec",
            "implement": "small",
            "independent_tests": "small",
            "metadata_fill": "small",
            "prime": "small",
            "repair": "small",
            "spec": "strong_spec",
        },
    }
    assert protocol["models"] == {
        "small": {
            "model": "gpt-5.6-luna",
            "reasoning": "low",
            "service_tier": "priority",
        },
        "strong_baseline": {
            "model": "gpt-5.5",
            "reasoning": "medium",
            "service_tier": "priority",
        },
        "strong_spec": {
            "model": "gpt-5.5",
            "reasoning": "low",
            "service_tier": "priority",
        },
    }


def _passing_replicate(
    workload: dict[str, Any], replicate_id: str, *, run_id: str = "run-v5-test"
) -> dict[str, Any]:
    order_rows = workload["orders"][replicate_id]
    order_sha256 = _sha(_canonical(order_rows))

    def rows(arm: str, costs: list[int], *, warm: bool = False) -> list[dict[str, Any]]:
        result = []
        for position, cost in enumerate(costs, 1):
            task_id = order_rows[position - 1]["task_id"]
            owner_scope = (
                "run-"
                + _sha(
                    _canonical(
                        {
                            "arm": arm,
                            "kind": "benchmark-owner",
                            "replicate_id": replicate_id,
                            "run_id": run_id,
                            "v": 1,
                        }
                    )
                )[:32]
            )
            result.append(
                {
                    "arm": arm,
                    "branch": (
                        "REUSE"
                        if warm and order_rows[position - 1]["expected_branch"] == "EXACT"
                        else "MISS"
                        if warm
                        else ""
                    ),
                    "cell_id": f"{run_id}:{replicate_id}:{arm}",
                    "claim_cost_evidence_sha256": _sha(
                        _canonical(
                            {
                                "arm": arm,
                                "claim_cost_nano": cost,
                                "kind": "claim-cost-row",
                                "position": position,
                                "replicate_id": replicate_id,
                                "run_id": run_id,
                                "task_id": task_id,
                                "v": 1,
                            }
                        )
                    ),
                    "claim_cost_nano": cost,
                    "economic_valid": True,
                    "escalated": False if warm else None,
                    "hidden_oracle_passed": True,
                    "infrastructure_valid": True,
                    "oracle_solved": True,
                    "order_sha256": order_sha256,
                    "owner_scope": owner_scope,
                    "position": position,
                    "public_accepted": True,
                    "replicate_id": replicate_id,
                    "run_id": run_id,
                    "task_id": task_id,
                    "terminal_score_count": 1,
                    "terminal_score_id": _sha(
                        _canonical(
                            {
                                "arm": arm,
                                "kind": "terminal-score",
                                "position": position,
                                "replicate_id": replicate_id,
                                "run_id": run_id,
                                "task_id": task_id,
                                "v": 1,
                            }
                        )
                    ),
                }
            )
        return result

    arms = {
        "baseline": rows("baseline", [100] * 30),
        "cheap_alone": rows("cheap_alone", [90] * 30),
        "cascade": rows("cascade", [80] * 30),
        "rrc_cold": rows("rrc_cold", [70] * 30),
        "rrc_warm": rows("rrc_warm", [60] * 10 + [40] * 10 + [10] * 10, warm=True),
    }
    for arm, arm_rows in arms.items():
        for row in arm_rows:
            row["branch"] = {
                "baseline": "BASELINE",
                "cascade": "CHEAP",
                "cheap_alone": "CHEAP_ALONE",
                "rrc_cold": "MISS",
            }.get(arm, row["branch"])
            row["escalated"] = False
    return {
        "arms": arms,
        "order_sha256": order_sha256,
        "replicate_id": replicate_id,
        "run_id": run_id,
    }


def _set_claim_cost(row: dict[str, Any], cost: int) -> None:
    row["claim_cost_nano"] = cost
    row["claim_cost_evidence_sha256"] = _sha(
        _canonical(
            {
                "arm": row["arm"],
                "claim_cost_nano": cost,
                "kind": "claim-cost-row",
                "position": row["position"],
                "replicate_id": row["replicate_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "v": 1,
            }
        )
    )


def test_frozen_analyzer_enforces_every_replicate_and_pooled_integer_predicate() -> None:
    workload = _load(WORKLOAD)
    replicates = [_passing_replicate(workload, item) for item in REPLICATES]
    assert all(
        evaluate_replicate(replicate, workload["orders"][replicate["replicate_id"]]).values()
        for replicate in replicates
    )
    assert claim_passes(replicates, workload)

    parity_failure = [_passing_replicate(workload, item) for item in REPLICATES]
    for row in parity_failure[2]["arms"]["rrc_warm"][:2]:
        row["hidden_oracle_passed"] = False
        row["oracle_solved"] = False
    assert not claim_passes(parity_failure, workload)

    malformed_cost = [_passing_replicate(workload, item) for item in REPLICATES]
    malformed_cost[0]["arms"]["baseline"][0]["claim_cost_nano"] = "100"
    assert not claim_passes(malformed_cost, workload)

    pooled_reversal = [_passing_replicate(workload, item) for item in REPLICATES]
    for replicate in pooled_reversal[:2]:
        eligible = [
            index
            for index, order in enumerate(workload["orders"][replicate["replicate_id"]])
            if order["expected_branch"] == "EXACT"
        ]
        selected = set(eligible[:17] + eligible[-1:])
        for index, row in enumerate(replicate["arms"]["rrc_warm"]):
            row["oracle_solved"] = index in selected
            row["public_accepted"] = row["oracle_solved"]
            row["hidden_oracle_passed"] = row["oracle_solved"]
            _set_claim_cost(row, 1 if index < 10 else 0)
        for index, row in enumerate(replicate["arms"]["baseline"]):
            row["oracle_solved"] = index < 18 or index == 29
            row["public_accepted"] = row["oracle_solved"]
            row["hidden_oracle_passed"] = row["oracle_solved"]
        for arm in ("cheap_alone", "cascade", "rrc_cold"):
            for row in replicate["arms"][arm]:
                _set_claim_cost(row, 2)
    for replicate in pooled_reversal[2:]:
        for index, row in enumerate(replicate["arms"]["rrc_warm"]):
            _set_claim_cost(row, 150 if index < 10 else 100 if index < 20 else 50)
        for arm in ("cheap_alone", "cascade", "rrc_cold"):
            for index, row in enumerate(replicate["arms"][arm]):
                _set_claim_cost(row, 101 if index == 29 else 0)
                row["oracle_solved"] = index == 29
                row["public_accepted"] = row["oracle_solved"]
                row["hidden_oracle_passed"] = row["oracle_solved"]
    assert all(
        all(evaluate_replicate(replicate, workload["orders"][replicate["replicate_id"]]).values())
        for replicate in pooled_reversal
    )
    assert not claim_passes(pooled_reversal, workload)


def test_frozen_analyzer_rejects_identity_duplication_and_cross_cell_rows() -> None:
    workload = _load(WORKLOAD)
    duplicated = [_passing_replicate(workload, item) for item in REPLICATES]
    duplicated[0]["arms"]["baseline"][1] = dict(duplicated[0]["arms"]["baseline"][0])
    assert not claim_passes(duplicated, workload)

    wrong_arm = [_passing_replicate(workload, item) for item in REPLICATES]
    wrong_arm[0]["arms"]["baseline"][0]["arm"] = "rrc_warm"
    assert not claim_passes(wrong_arm, workload)

    cross_replicate = [_passing_replicate(workload, item) for item in REPLICATES]
    cross_replicate[0]["arms"]["rrc_warm"], cross_replicate[1]["arms"]["rrc_warm"] = (
        cross_replicate[1]["arms"]["rrc_warm"],
        cross_replicate[0]["arms"]["rrc_warm"],
    )
    assert not claim_passes(cross_replicate, workload)

    reordered = [_passing_replicate(workload, item) for item in REPLICATES]
    reordered[0]["arms"]["rrc_warm"].reverse()
    assert not claim_passes(reordered, workload)

    invalid_infrastructure = [_passing_replicate(workload, item) for item in REPLICATES]
    invalid_infrastructure[0]["arms"]["baseline"][0]["infrastructure_valid"] = False
    assert not claim_passes(invalid_infrastructure, workload)

    duplicate_score = [_passing_replicate(workload, item) for item in REPLICATES]
    duplicate_score[0]["arms"]["baseline"][1]["terminal_score_id"] = duplicate_score[0]["arms"][
        "baseline"
    ][0]["terminal_score_id"]
    assert not claim_passes(duplicate_score, workload)

    relabeled_score = [_passing_replicate(workload, item) for item in REPLICATES]
    relabeled_score[0]["arms"]["baseline"][0]["terminal_score_id"] = "f" * 64
    assert not claim_passes(relabeled_score, workload)

    relabeled_cost = [_passing_replicate(workload, item) for item in REPLICATES]
    relabeled_cost[0]["arms"]["baseline"][0]["claim_cost_evidence_sha256"] = "f" * 64
    assert not claim_passes(relabeled_cost, workload)


def test_frozen_analyzer_rejects_impossible_seed_hits_and_cross_run_splices() -> None:
    workload = _load(WORKLOAD)
    impossible_hits = [_passing_replicate(workload, item) for item in REPLICATES]
    for replicate in impossible_hits:
        warm = replicate["arms"]["rrc_warm"]
        reachable = [
            row
            for row, order in zip(warm, workload["orders"][replicate["replicate_id"]], strict=True)
            if order["expected_branch"] == "EXACT"
        ]
        seeds = [
            row
            for row, order in zip(warm, workload["orders"][replicate["replicate_id"]], strict=True)
            if order["expected_branch"] == "MISS"
        ]
        for row in reachable:
            row["branch"] = "MISS"
        for row in reachable[:13]:
            row["branch"] = "REUSE"
        for row in seeds:
            row["branch"] = "REUSE"
    assert not claim_passes(impossible_hits, workload)
    for replicate in impossible_hits:
        for row, order in zip(
            replicate["arms"]["rrc_warm"],
            workload["orders"][replicate["replicate_id"]],
            strict=True,
        ):
            if order["expected_branch"] == "MISS":
                row["branch"] = "MISS"
    assert not claim_passes(impossible_hits, workload)

    run_a = [_passing_replicate(workload, item, run_id="run-a") for item in REPLICATES]
    run_b = [_passing_replicate(workload, item, run_id="run-b") for item in REPLICATES]
    hybrid = json.loads(json.dumps(run_a))
    for index in range(4):
        for arm in ("baseline", "cheap_alone", "cascade", "rrc_cold"):
            hybrid[index]["arms"][arm] = run_b[index]["arms"][arm]
    assert not claim_passes(hybrid, workload)


def test_frozen_analyzer_authenticates_the_exact_workload_authority() -> None:
    workload = _load(WORKLOAD)
    replicates = [_passing_replicate(workload, item) for item in REPLICATES]
    forged = json.loads(json.dumps(workload))
    forged["v"] = 999
    forged["master_seed"] = "forged"
    forged["benchmark_scope"] = "arbitrary"
    assert not claim_passes(replicates, forged)

    missing_protocol = json.loads(json.dumps(workload))
    del missing_protocol["protocol"]
    assert not claim_passes(replicates, missing_protocol)


def test_four_orders_are_hash_derived_and_have_exactly_25_prior_exact_refs() -> None:
    workload = _load(WORKLOAD)
    by_id = {row["task_id"]: row for row in workload["tasks"]}
    assert set(workload["orders"]) == set(REPLICATES)
    for replicate in REPLICATES:
        expected_ids = sorted(
            by_id,
            key=lambda task_id: (
                hashlib.sha256(f"{MASTER_SEED}\0{replicate}\0{task_id}".encode()).hexdigest(),
                task_id,
            ),
        )
        rows = workload["orders"][replicate]
        assert [row["task_id"] for row in rows] == expected_ids
        assert [row["position"] for row in rows] == list(range(1, 31))
        assert sum(row["prior_reference"] is not None for row in rows) == 25
        seen: list[str] = []
        for row in rows:
            current = by_id[row["task_id"]]
            prior = row["prior_reference"]
            if prior is None:
                assert row["expected_branch"] == "MISS"
                assert not any(by_id[item]["family"] == current["family"] for item in seen)
            else:
                assert prior in seen
                assert row["expected_branch"] == "EXACT"
                assert by_id[prior]["family"] == current["family"]
                assert by_id[prior]["shape"] == current["shape"]
            seen.append(row["task_id"])


def test_reference_solutions_pass_policy_toolchain_public_and_hidden_nodes(tmp_path: Path) -> None:
    workload = _load(WORKLOAD)
    hidden_by_id = {row["task_id"]: row for row in _load(ORACLES)["tasks"]}
    sources = tmp_path / "sources"
    sources.mkdir()
    for task in workload["tasks"]:
        hidden = hidden_by_id[task["task_id"]]
        namespace: dict[str, object] = {}
        exec(compile(hidden["reference_solution"], task["task_id"], "exec"), namespace)
        for index, test_source in enumerate(
            (*task["public_tests"], *hidden["hidden_oracle_tests"])
        ):
            test_namespace = dict(namespace)
            exec(compile(test_source, f"{task['task_id']}-test-{index}", "exec"), test_namespace)
            tests = [value for name, value in test_namespace.items() if name.startswith("test_")]
            assert len(tests) == 1
            test = tests[0]
            assert callable(test)
            test()
        (sources / f"{task['task_id'].replace('-', '_')}.py").write_text(
            hidden["reference_solution"]
        )
    ruff = REPO / ".venv/bin/ruff"
    pyright = REPO / ".venv/bin/pyright"
    assert (
        subprocess.run(
            [ruff, "check", "--fix", sources], capture_output=True, text=True, check=False
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            [ruff, "format", sources], capture_output=True, text=True, check=False
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            [ruff, "check", sources], capture_output=True, text=True, check=False
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            [ruff, "format", "--check", sources], capture_output=True, text=True, check=False
        ).returncode
        == 0
    )
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        _canonical(
            {"include": [str(sources)], "pythonVersion": "3.11", "typeCheckingMode": "basic"}
        )
    )
    result = subprocess.run(
        [pyright, "--project", config], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
