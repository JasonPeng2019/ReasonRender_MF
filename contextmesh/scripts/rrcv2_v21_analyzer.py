#!/usr/bin/env python3
"""Reconcile the one-shot V21 smoke and render model-separated economics."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from rrc.cell_journal import CombinedSessionAttemptV1, CombinedSessionRecordV1
from rrc.contract import CostEventV1, canonical_json_bytes

_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_MAX_FILE = 16 * 1024 * 1024
_EXPECTED_MODELS = {"gpt-5.5", "gpt-5.6-luna"}
_EXPECTED_FAST_RATES = {
    "gpt-5.5_short_lt_272k": {
        "cache_write": None,
        "cached_input": "1.25",
        "output": "75.00",
        "uncached_input": "12.50",
    },
    "gpt-5.6-luna_long": {
        "cache_write": "1.00",
        "cached_input": "0.08",
        "output": "3.60",
        "uncached_input": "0.80",
    },
    "gpt-5.6-luna_short": {
        "cache_write": "0.50",
        "cached_input": "0.04",
        "output": "2.40",
        "uncached_input": "0.40",
    },
}


class AnalysisError(RuntimeError):
    """V21 evidence is incomplete, contradictory, or outside its frozen schema."""


@dataclass(frozen=True)
class TokenSlice:
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int

    @property
    def ordinary_input_tokens(self) -> int:
        return self.input_tokens - self.cached_input_tokens - self.cache_write_input_tokens


@dataclass(frozen=True)
class UsageEvidence(TokenSlice):
    transcript_sha256: str
    effective_model: str | None
    effective_reasoning: str | None
    source: str
    turns: tuple[TokenSlice, ...]
    turn_usage_attested: bool
    visible_failure: bool = False


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path, *, cap: int = _MAX_FILE) -> bytes:
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_size > cap:
        raise AnalysisError(f"evidence is special or oversized: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, cap + 1)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or len(raw) != opened.st_size
        or len(raw) > cap
    ):
        raise AnalysisError(f"evidence changed while reading: {path}")
    return raw


def _integer(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnalysisError(f"{name} is not a nonnegative integer")
    return value


def _usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(_FIELDS):
        raise AnalysisError("usage row is not an object")
    result = {field: _integer(value[field], name=field) for field in _FIELDS}
    if (
        result["cached_input_tokens"] + result["cache_write_input_tokens"] > result["input_tokens"]
        or result["reasoning_output_tokens"] > result["output_tokens"]
        or result["total_tokens"] != result["input_tokens"] + result["output_tokens"]
    ):
        raise AnalysisError("usage arithmetic differs")
    return result


def parse_native_transcript(
    raw: bytes, *, source: str, allow_visible_failure: bool = False
) -> UsageEvidence:
    """Parse one complete native Codex transcript and its final cumulative usage."""

    if not raw:
        raise AnalysisError("native transcript is empty")
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise AnalysisError("native transcript is not UTF-8") from exc
    usages: list[tuple[int, dict[str, int]]] = []
    turns: list[TokenSlice] = []
    turn_usage_attested = True
    completions: list[int] = []
    models: set[str] = set()
    reasonings: set[str] = set()
    visible_failure = False
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnalysisError("native transcript contains malformed JSONL") from exc
        if not isinstance(row, dict):
            raise AnalysisError("native transcript contains a non-object row")
        payload = row.get("payload")
        if row.get("type") == "turn_context" and isinstance(payload, dict):
            model = payload.get("model")
            reasoning = payload.get("effort")
            if isinstance(model, str) and model:
                models.add(model)
            if isinstance(reasoning, str) and reasoning:
                reasonings.add(reasoning)
        if (
            row.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "token_count"
        ):
            info = payload.get("info")
            cumulative = info.get("total_token_usage") if isinstance(info, dict) else None
            converted = _usage(cumulative)
            previous = usages[-1][1] if usages else None
            last = info.get("last_token_usage") if isinstance(info, dict) else None
            expected_last = _usage(
                {
                    field: converted[field] - (0 if previous is None else previous[field])
                    for field in _FIELDS
                }
            )
            if last is None:
                turn_usage_attested = False
                last_converted = expected_last
            else:
                last_converted = _usage(last)
                if last_converted != expected_last:
                    raise AnalysisError("native last-turn usage differs from cumulative delta")
            turns.append(TokenSlice(**last_converted))
            usages.append((index, converted))
        payload_type = payload.get("type") if isinstance(payload, dict) else None
        if row.get("type") == "event_msg" and payload_type == "task_complete":
            completions.append(index)
        names = {str(row.get("type", "")).lower(), str(payload_type or "").lower()}
        if any(marker in name for name in names for marker in ("error", "failed", "aborted")):
            visible_failure = True
            if not allow_visible_failure:
                raise AnalysisError("native transcript contains a visible failure")
    if not usages:
        raise AnalysisError("native transcript has no cumulative usage")
    for (_old_index, old), (_new_index, new) in zip(usages, usages[1:]):
        if any(new[field] < old[field] for field in _FIELDS):
            raise AnalysisError("native cumulative usage regressed")
    final_index, final = usages[-1]
    if {field: sum(getattr(turn, field) for turn in turns) for field in _FIELDS} != final:
        raise AnalysisError("native per-turn usage does not reconcile to cumulative usage")
    # Stop hooks run before Codex appends its terminal task_complete row.  The
    # product CostEvent is bound to that exact Stop-time snapshot, so zero or
    # one later completion is valid; multiple or pre-usage completions are not.
    if len(completions) > 1 or (completions and completions[0] <= final_index):
        raise AnalysisError("native transcript completion evidence differs")
    if len(models) != 1 or len(reasonings) != 1:
        raise AnalysisError("native effective model/reasoning is not unique")
    return UsageEvidence(
        transcript_sha256=_sha(raw),
        input_tokens=final["input_tokens"],
        cached_input_tokens=final["cached_input_tokens"],
        cache_write_input_tokens=final["cache_write_input_tokens"],
        output_tokens=final["output_tokens"],
        reasoning_output_tokens=final["reasoning_output_tokens"],
        total_tokens=final["total_tokens"],
        effective_model=next(iter(models)),
        effective_reasoning=next(iter(reasonings)),
        source=source,
        turns=tuple(turns),
        turn_usage_attested=turn_usage_attested,
        visible_failure=visible_failure,
    )


def parse_codex_exec_transcript(raw: bytes, *, source: str) -> UsageEvidence:
    """Recover one root Codex `exec --json` completion before a Stop snapshot exists."""

    rows: list[dict[str, object]] = []
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise AnalysisError("Codex exec transcript is not UTF-8") from exc
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnalysisError("Codex exec transcript contains malformed JSONL") from exc
        if not isinstance(value, dict):
            raise AnalysisError("Codex exec transcript contains a non-object row")
        rows.append(cast(dict[str, object], value))
    started = [row for row in rows if row.get("type") == "thread.started"]
    completed = [row for row in rows if row.get("type") == "turn.completed"]
    failed = [row for row in rows if row.get("type") in {"turn.failed", "error"}]
    if len(started) != 1 or len(completed) != 1 or failed:
        raise AnalysisError("Codex exec transcript has no unique successful completion")
    usage_raw = completed[0].get("usage")
    if not isinstance(usage_raw, dict):
        raise AnalysisError("Codex exec transcript completion has no usage")
    usage = _usage(
        {
            "input_tokens": usage_raw.get("input_tokens"),
            "cached_input_tokens": usage_raw.get("cached_input_tokens"),
            "cache_write_input_tokens": usage_raw.get("cache_write_input_tokens"),
            "output_tokens": usage_raw.get("output_tokens"),
            "reasoning_output_tokens": usage_raw.get("reasoning_output_tokens"),
            "total_tokens": _integer(usage_raw.get("input_tokens"), name="input_tokens")
            + _integer(usage_raw.get("output_tokens"), name="output_tokens"),
        }
    )
    turn = TokenSlice(**usage)
    return UsageEvidence(
        input_tokens=usage["input_tokens"],
        cached_input_tokens=usage["cached_input_tokens"],
        cache_write_input_tokens=usage["cache_write_input_tokens"],
        output_tokens=usage["output_tokens"],
        reasoning_output_tokens=usage["reasoning_output_tokens"],
        total_tokens=usage["total_tokens"],
        transcript_sha256=_sha(raw),
        effective_model=None,
        effective_reasoning=None,
        source=source,
        turns=(turn,),
        turn_usage_attested=True,
    )


def parse_planner_row(row: object, *, source: str) -> UsageEvidence:
    if not isinstance(row, dict):
        raise AnalysisError("planner evidence row is not an object")
    required = {
        "arm",
        "model",
        "prompt",
        "requested_reasoning",
        "requested_service_tier",
        "response",
        "role",
        "stage",
        "task_id",
        "transcript_sha256",
        "usage",
    }
    if set(row) != required:
        raise AnalysisError("planner evidence schema differs")
    usage_raw = row["usage"]
    if not isinstance(usage_raw, dict) or set(usage_raw) != {
        "cached_input_tokens",
        "cache_write_input_tokens",
        "completion_tokens",
        "prompt_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    }:
        raise AnalysisError("planner usage schema differs")
    converted = _usage(
        {
            "input_tokens": usage_raw["prompt_tokens"],
            "cached_input_tokens": usage_raw["cached_input_tokens"],
            "cache_write_input_tokens": usage_raw["cache_write_input_tokens"],
            "output_tokens": usage_raw["completion_tokens"],
            "reasoning_output_tokens": usage_raw["reasoning_output_tokens"],
            "total_tokens": usage_raw["total_tokens"],
        }
    )
    digest = row["transcript_sha256"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise AnalysisError("planner transcript digest differs")
    model = row["model"]
    reasoning = row["requested_reasoning"]
    if not isinstance(model, str) or not isinstance(reasoning, str):
        raise AnalysisError("planner requested identity differs")
    return UsageEvidence(
        transcript_sha256=digest,
        input_tokens=converted["input_tokens"],
        cached_input_tokens=converted["cached_input_tokens"],
        cache_write_input_tokens=converted["cache_write_input_tokens"],
        output_tokens=converted["output_tokens"],
        reasoning_output_tokens=converted["reasoning_output_tokens"],
        total_tokens=converted["total_tokens"],
        effective_model=None,
        effective_reasoning=None,
        source=source,
        turns=(TokenSlice(**converted),),
        turn_usage_attested=True,
    )


def price_call(*, model: str, usage: UsageEvidence) -> tuple[int | None, str | None]:
    """Return exact Fast-mode nanodollars, or a reason the call is unpriced."""

    if not usage.turn_usage_attested:
        return None, "native transcript omitted exact last-turn usage"
    total = 0
    for turn in usage.turns:
        ordinary = turn.ordinary_input_tokens
        if ordinary < 0:
            return None, "cached plus cache-write input exceeds input"
        if model == "gpt-5.5":
            if turn.input_tokens >= 272_000:
                return None, "gpt-5.5 Fast is frozen only below 272K context per turn"
            if turn.cache_write_input_tokens:
                return None, "official GPT-5.5 Fast table lists no separate cache-write rate"
            total += (
                ordinary * 12_500 + turn.cached_input_tokens * 1_250 + turn.output_tokens * 75_000
            )
        elif model == "gpt-5.6-luna":
            rates = (
                (400, 40, 500, 2_400) if turn.input_tokens < 272_000 else (800, 80, 1_000, 3_600)
            )
            total += (
                ordinary * rates[0]
                + turn.cached_input_tokens * rates[1]
                + turn.cache_write_input_tokens * rates[2]
                + turn.output_tokens * rates[3]
            )
        else:
            return None, f"no frozen Fast rate for {model}"
    if model in _EXPECTED_MODELS:
        return total, None
    return None, f"no frozen Fast rate for {model}"


def _format_nanodollars(value: int) -> str:
    return f"{value // 1_000_000_000}.{value % 1_000_000_000:09d}"


def _price_scenario_nanodollars(*, model: str, usage: UsageEvidence) -> dict[str, int]:
    """Return named non-definitive bounds only where the official table supports them."""

    if not usage.turn_usage_attested or model != "gpt-5.5" or not usage.cache_write_input_tokens:
        return {}
    if any(turn.input_tokens >= 272_000 for turn in usage.turns):
        return {}
    base = sum(
        turn.ordinary_input_tokens * 12_500
        + turn.cached_input_tokens * 1_250
        + turn.output_tokens * 75_000
        for turn in usage.turns
    )
    writes = sum(turn.cache_write_input_tokens for turn in usage.turns)
    return {
        "cache_write_priced_as_cached_input": base + writes * 1_250,
        "cache_write_priced_as_uncached_input": base + writes * 12_500,
    }


def price_scenarios(*, model: str, usage: UsageEvidence) -> dict[str, str]:
    return {
        name + "_dollars": _format_nanodollars(value)
        for name, value in _price_scenario_nanodollars(model=model, usage=usage).items()
    }


def _cost_event(raw: bytes) -> CostEventV1:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisError("cost event is not strict JSON") from exc
    if not isinstance(value, dict):
        raise AnalysisError("cost event is not an object")
    try:
        event = CostEventV1(**cast(dict[str, Any], value))
    except (TypeError, ValueError) as exc:
        raise AnalysisError("cost event schema differs") from exc
    if event.canonical_bytes() != raw:
        raise AnalysisError("cost event is not canonical")
    return event


def _jsonl(path: Path) -> list[dict[str, object]]:
    raw = _read(path)
    result: list[dict[str, object]] = []
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnalysisError(f"malformed JSONL at {path}:{number}") from exc
        if not isinstance(value, dict):
            raise AnalysisError(f"non-object JSONL at {path}:{number}")
        result.append(cast(dict[str, object], value))
    return result


def _match(event: CostEventV1, usage: UsageEvidence) -> None:
    if (
        event.transcript_sha256 != usage.transcript_sha256
        or event.input_tokens != usage.input_tokens
        or event.cached_input_tokens != usage.cached_input_tokens
        or event.output_tokens != usage.output_tokens
        or event.reasoning_output_tokens != usage.reasoning_output_tokens
        or event.provider_total_tokens != usage.total_tokens
    ):
        raise AnalysisError(f"usage differs from CostEvent {event.cost_event_id}")
    if usage.effective_model is not None:
        expected_model = (
            event.requested_model
            if event.identity_attestation == "usage_only"
            else event.effective_model
        )
        if usage.effective_model != expected_model:
            raise AnalysisError(f"effective model differs for {event.cost_event_id}")
    if usage.effective_reasoning is not None:
        expected_reasoning = (
            event.requested_reasoning
            if event.identity_attestation == "usage_only"
            else event.effective_reasoning
        )
        if usage.effective_reasoning != expected_reasoning:
            raise AnalysisError(f"effective reasoning differs for {event.cost_event_id}")


def _sum_rows(rows: Iterable[Mapping[str, object]]) -> dict[str, object]:
    rows = list(rows)
    fields = (
        "input_tokens",
        "ordinary_input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "provider_visible_tokens",
    )
    total = {field: sum(cast(int, row[field]) for row in rows) for field in fields}
    costs = [row.get("api_equivalent_fast_nanodollars") for row in rows]
    definitive = all(isinstance(value, int) and not isinstance(value, bool) for value in costs)
    known = sum(
        cast(int, value)
        for value in costs
        if isinstance(value, int) and not isinstance(value, bool)
    )
    scenario_totals: dict[str, int] | None = {
        "cache_write_priced_as_cached_input": 0,
        "cache_write_priced_as_uncached_input": 0,
    }
    for row, cost in zip(rows, costs):
        if isinstance(cost, int) and not isinstance(cost, bool):
            assert scenario_totals is not None
            for name in scenario_totals:
                scenario_totals[name] += cost
            continue
        scenarios = row.get("cost_scenario_nanodollars")
        if not isinstance(scenarios, dict) or set(scenarios) != set(
            cast(dict[str, int], scenario_totals)
        ):
            scenario_totals = None
            break
        assert scenario_totals is not None
        for name in scenario_totals:
            value = scenarios.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                scenario_totals = None
                break
            scenario_totals[name] += value
        if scenario_totals is None:
            break
    return {
        "api_equivalent_fast_dollars": (_format_nanodollars(known) if definitive else None),
        "api_equivalent_fast_nanodollars": known if definitive else None,
        "calls": len(rows),
        "cost_definitive": definitive,
        "known_api_equivalent_fast_dollars": _format_nanodollars(known),
        "known_api_equivalent_fast_nanodollars": known,
        "scenario_api_equivalent_fast_dollars": (
            None
            if definitive or scenario_totals is None
            else {
                name + "_dollars": _format_nanodollars(value)
                for name, value in scenario_totals.items()
            }
        ),
        "effective_identity_attestations": sorted(
            {str(row.get("identity_attestation", "unattested")) for row in rows}
        ),
        "effective_service_tiers": sorted(
            {str(row.get("effective_service_tier", "unattested")) for row in rows}
        ),
        "requested_service_tiers": sorted(
            {str(row.get("requested_service_tier", "unknown")) for row in rows}
        ),
        "solved_statuses": sorted({str(row.get("solved_status", "unknown")) for row in rows}),
        **total,
    }


def _combined_record(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AnalysisError("combined-session authority is malformed") from exc
    expected = {
        "all_cost_event_ids",
        "attempts",
        "cell_id",
        "root_cost_event_id",
        "root_final_sha256",
        "root_session_id",
        "root_transcript_sha256",
        "round_id",
        "tool_event_sha256s",
        "v",
        "wait_envelope_sha256",
        "wait_id",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("v") != 1
        or not isinstance(value.get("cell_id"), str)
        or not isinstance(value.get("all_cost_event_ids"), list)
        or not isinstance(value.get("attempts"), list)
    ):
        raise AnalysisError("combined-session authority schema differs")
    try:
        attempts = tuple(
            CombinedSessionAttemptV1(**cast(dict[str, Any], attempt))
            for attempt in cast(list[object], value["attempts"])
            if isinstance(attempt, dict)
        )
        if len(attempts) != len(cast(list[object], value["attempts"])):
            raise ValueError("combined attempt is not an object")
        record = CombinedSessionRecordV1(
            cell_id=cast(str, value["cell_id"]),
            round_id=cast(str, value["round_id"]),
            root_session_id=cast(str, value["root_session_id"]),
            root_cost_event_id=cast(str, value["root_cost_event_id"]),
            root_transcript_sha256=cast(str, value["root_transcript_sha256"]),
            root_final_sha256=cast(str, value["root_final_sha256"]),
            wait_id=cast(str, value["wait_id"]),
            wait_envelope_sha256=cast(str, value["wait_envelope_sha256"]),
            tool_event_sha256s=tuple(cast(list[str], value["tool_event_sha256s"])),
            attempts=attempts,
            all_cost_event_ids=tuple(cast(list[str], value["all_cost_event_ids"])),
            v=cast(int, value["v"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalysisError("combined-session semantic authority differs") from exc
    if record.canonical_bytes() != raw:
        raise AnalysisError("combined-session canonical relation differs")
    return cast(dict[str, object], value)


def _group_rows(rows: Sequence[Mapping[str, object]], field: str) -> dict[str, dict[str, object]]:
    values = sorted({str(row.get(field, "unknown")) for row in rows})
    return {
        value: _sum_rows(row for row in rows if str(row.get(field, "unknown")) == value)
        for value in values
    }


def _authority_hashes(producer_root: Path) -> dict[str, str]:
    repo = Path(__file__).resolve().parents[2]
    paths = {
        "producer": producer_root / "producer.json",
        "launch": repo / ".generated/state/rrcv2-convergence/verify/v21-launch-manifest.v1.json",
        "price": repo
        / ".generated/state/rrcv2-convergence/economic/openai-pricing-20260812.v1.json",
        "price_source": repo
        / ".generated/state/rrcv2-convergence/economic/openai-pricing-20260812.md",
    }
    if any(stat.S_IMODE(os.lstat(path).st_mode) != 0o600 for path in paths.values()):
        raise AnalysisError("V21 economic authority mode differs")
    producer = _read(paths["producer"])
    launch = _read(paths["launch"])
    price = _read(paths["price"])
    price_source = _read(paths["price_source"])
    try:
        producer_value = json.loads(producer)
        price_value = json.loads(price)
    except json.JSONDecodeError as exc:
        raise AnalysisError("V21 economic authority is malformed") from exc
    if (
        not isinstance(producer_value, dict)
        or canonical_json_bytes(producer_value) != producer
        or set(producer_value)
        != {
            "experiment_id",
            "fixture_manifest_sha256",
            "launch_manifest_sha256",
            "predecessor_manifest_sha256",
            "price_authority_sha256",
            "producer_sha256",
            "round_token",
            "session_plan_sha256",
            "session_review_sha256",
            "v",
        }
        or producer_value.get("experiment_id") != "rrcv2-cli-smoke-v21"
        or producer_value.get("v") != 21
        or not isinstance(producer_value.get("producer_sha256"), str)
        or producer_value.get("producer_sha256") != producer_root.name
        or not isinstance(producer_value.get("round_token"), str)
        or producer_value.get("round_token") != f"rrcv2-cli-smoke-{producer_root.name[:32]}"
        or not isinstance(price_value, dict)
        or canonical_json_bytes(price_value) != price
        or producer_value.get("launch_manifest_sha256") != _sha(launch)
        or producer_value.get("price_authority_sha256") != _sha(price)
        or price_value.get("sha256") != _sha(price_source)
        or price_value.get("fast_rates_per_million") != _EXPECTED_FAST_RATES
        or price_value.get("url") != "https://developers.openai.com/api/docs/pricing.md"
        or not isinstance(price_value.get("retrieved_at_utc"), str)
        or not str(price_value["retrieved_at_utc"]).endswith("Z")
    ):
        raise AnalysisError("V21 economic authority linkage differs")
    return {
        "launch_manifest_sha256": _sha(launch),
        "price_authority_sha256": _sha(price),
        "price_source_capture_sha256": _sha(price_source),
        "producer_identity": str(producer_value.get("producer_sha256", "")),
        "producer_json_sha256": _sha(producer),
    }


def analyze(producer_root: Path) -> dict[str, object]:
    terminal_path = producer_root / "terminal.json"
    if stat.S_IMODE(os.lstat(terminal_path).st_mode) != 0o600:
        raise AnalysisError("terminal receipt mode differs")
    terminal_raw = _read(terminal_path)
    terminal = json.loads(terminal_raw)
    authorities = _authority_hashes(producer_root)
    if (
        not isinstance(terminal, dict)
        or canonical_json_bytes(terminal) != terminal_raw
        or set(terminal) != {"producer_sha256", "returncode", "status", "v"}
        or terminal.get("producer_sha256") != authorities["producer_identity"]
        or terminal.get("producer_sha256") != producer_root.name
        or terminal.get("status") != "success"
        or terminal.get("returncode") != 0
        or terminal.get("v") != 1
    ):
        raise AnalysisError("terminal receipt differs")
    round_root = producer_root / "round"
    database = round_root / "rrcv2.sqlite3"
    if not database.is_file():
        raise AnalysisError("V21 database is absent; consumption is unquantified")

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        root_event_rows = list(
            connection.execute("SELECT state,root_cost_event FROM rrcv2p_cells ORDER BY cell_id")
        )
        call_event_rows = list(
            connection.execute("SELECT state,cost_event FROM rrcv2_calls ORDER BY call_id")
        )
        if (
            len(root_event_rows) != 3
            or len(call_event_rows) != 7
            or any(state != "combined_committed" or raw is None for state, raw in root_event_rows)
            or any(state != "call_committed" or raw is None for state, raw in call_event_rows)
        ):
            raise AnalysisError("V21 durable call inventory is incomplete or contains extras")
        event_raws = [cast(bytes, raw) for _state, raw in root_event_rows]
        event_raws.extend(cast(bytes, raw) for _state, raw in call_event_rows)
        worker_raws = [
            cast(bytes, row[0])
            for row in connection.execute(
                "SELECT transcript FROM rrcv2p_attempts_bindings WHERE transcript IS NOT NULL"
            )
        ]
        combined_raws = [
            cast(bytes, row[0])
            for row in connection.execute(
                "SELECT combined_session FROM rrcv2p_cells WHERE combined_session IS NOT NULL"
            )
        ]
        oracle_rows = [
            {"attempt_id": cast(str, row[0]), "status": cast(str, row[1]), "score": bool(row[2])}
            for row in connection.execute(
                "SELECT attempt_id,status,score FROM rrcv2_oracle_scores ORDER BY attempt_id"
            )
        ]
        accepted_rows = [
            {
                "attempt_id": cast(str, row[0]),
                "state": cast(str, row[1]),
                "terminal_outcome_sha256": cast(str, row[2]),
                "receipt": cast(str, row[3]),
            }
            for row in connection.execute(
                "SELECT a.attempt_id,a.state,m.outcome_sha256,r.receipt "
                "FROM rrcv2_attempts AS a "
                "JOIN rrcv2_acceptance_markers AS m USING(attempt_id) "
                "JOIN rrcv2_receipts AS r USING(attempt_id) ORDER BY a.attempt_id"
            )
        ]
        apply_rows = [
            {
                "cell_id": cast(str, row[0]),
                "event_sha256": cast(str, row[1]),
                "output": json.loads(cast(bytes, row[2])),
            }
            for row in connection.execute(
                "SELECT cell_id,event_sha256,output FROM rrcv2p_cell_tool_events "
                "WHERE kind='apply' ORDER BY cell_id,sequence"
            )
        ]
    finally:
        connection.close()

    events = [_cost_event(raw) for raw in event_raws]
    if len({event.cost_event_id for event in events}) != len(events) or len(
        {event.transcript_sha256 for event in events}
    ) != len(events):
        raise AnalysisError("duplicate CostEvent or transcript identity")
    combined = [_combined_record(raw) for raw in combined_raws]
    if len({cast(str, row["cell_id"]) for row in combined}) != len(combined):
        raise AnalysisError("duplicate combined-session cell identity")
    native: dict[str, UsageEvidence] = {}
    for path in sorted(round_root.glob("*/root-transcript.jsonl")):
        evidence = parse_native_transcript(_read(path), source=str(path.relative_to(producer_root)))
        if evidence.transcript_sha256 in native:
            raise AnalysisError("duplicate native transcript digest")
        native[evidence.transcript_sha256] = evidence
    for raw in worker_raws:
        digest = _sha(raw)
        evidence = parse_native_transcript(raw, source=f"sqlite:worker:{digest}")
        if digest != evidence.transcript_sha256 or digest in native:
            raise AnalysisError("worker transcript digest differs")
        native[digest] = evidence

    planner: dict[str, tuple[UsageEvidence, dict[str, object]]] = {}
    for path in sorted(round_root.glob("*/rrc-model-events.jsonl")):
        for index, row in enumerate(_jsonl(path), 1):
            evidence = parse_planner_row(row, source=f"{path.relative_to(producer_root)}:{index}")
            if evidence.transcript_sha256 in planner:
                raise AnalysisError("duplicate planner transcript digest")
            planner[evidence.transcript_sha256] = (evidence, row)

    summary_path = round_root / "summary.json"
    product_summary = json.loads(_read(summary_path)) if summary_path.is_file() else None
    summary_cells = product_summary.get("cells") if isinstance(product_summary, dict) else None
    solved_by_task = {
        cast(str, row["task_id"]): str(row.get("terminal_kind", "unknown"))
        for row in (summary_cells if isinstance(summary_cells, list) else [])
        if isinstance(row, dict) and isinstance(row.get("task_id"), str)
    }

    rows: list[dict[str, object]] = []
    used_evidence: set[str] = set()
    for event in sorted(
        events, key=lambda item: (item.cell_id, item.stage_ordinal, item.cost_event_id)
    ):
        if event.requested_model not in _EXPECTED_MODELS:
            raise AnalysisError("CostEvent requested model differs")
        if event.stage == "contextmesh_root_session":
            role = "root"
            evidence = native.get(event.transcript_sha256)
        elif event.stage == "implement":
            role = "native_worker"
            evidence = native.get(event.transcript_sha256)
        else:
            role = "planner"
            pair = planner.get(event.transcript_sha256)
            evidence = None if pair is None else pair[0]
            if pair is not None:
                log = pair[1]
                if (
                    log.get("stage") != event.stage
                    or log.get("task_id") != event.task_id
                    or log.get("model") != event.requested_model
                    or log.get("requested_reasoning") != event.requested_reasoning
                    or log.get("requested_service_tier") != event.requested_service_tier
                ):
                    raise AnalysisError("planner evidence identity differs")
        if evidence is None:
            raise AnalysisError(f"no transcript evidence for {event.cost_event_id}")
        _match(event, evidence)
        used_evidence.add(evidence.transcript_sha256)
        price, price_error = price_call(model=event.requested_model, usage=evidence)
        rows.append(
            {
                "api_equivalent_fast_dollars": (
                    None if price is None else _format_nanodollars(price)
                ),
                "api_equivalent_fast_nanodollars": price,
                "arm": event.arm,
                "cache_write_input_tokens": evidence.cache_write_input_tokens,
                "cached_input_tokens": evidence.cached_input_tokens,
                "cell_id": event.cell_id,
                "cost_event_id": event.cost_event_id,
                "cost_scenarios": price_scenarios(model=event.requested_model, usage=evidence),
                "cost_scenario_nanodollars": _price_scenario_nanodollars(
                    model=event.requested_model, usage=evidence
                ),
                "cost_unknown_reason": price_error,
                "effective_model": event.effective_model,
                "effective_provider": event.effective_provider,
                "effective_reasoning": event.effective_reasoning,
                "effective_service_tier": event.effective_service_tier,
                "identity_attestation": event.identity_attestation,
                "grouped_effective_model": (evidence.effective_model or event.effective_model),
                "ordinary_input_tokens": evidence.ordinary_input_tokens,
                "input_tokens": evidence.input_tokens,
                "output_tokens": evidence.output_tokens,
                "provider_visible_tokens": evidence.total_tokens,
                "reasoning_output_tokens": evidence.reasoning_output_tokens,
                "requested_model": event.requested_model,
                "requested_provider": event.requested_provider,
                "requested_reasoning": event.requested_reasoning,
                "requested_service_tier": event.requested_service_tier,
                "role": role,
                "solved_status": solved_by_task.get(event.task_id, "unknown"),
                "source": evidence.source,
                "stage": event.stage,
                "stage_ordinal": event.stage_ordinal,
                "task_id": event.task_id,
                "transcript_sha256": evidence.transcript_sha256,
                "transcript_observed_model": evidence.effective_model,
                "transcript_observed_reasoning": evidence.effective_reasoning,
                "turn_context_bands": [
                    "short" if turn.input_tokens < 272_000 else "long" for turn in evidence.turns
                ],
                "turn_input_tokens": [turn.input_tokens for turn in evidence.turns],
            }
        )
    available = set(native) | set(planner)
    if used_evidence != available:
        raise AnalysisError("provider usage exists outside the committed CostEvent inventory")

    by_model = _group_rows(rows, "requested_model")
    by_effective_model = _group_rows(rows, "grouped_effective_model")
    by_cell = _group_rows(rows, "cell_id")
    by_role = _group_rows(rows, "role")
    by_stage = _group_rows(rows, "stage")
    total = _sum_rows(rows)
    summary_exact = (
        isinstance(summary_cells, list)
        and len(summary_cells) == 3
        and [row.get("task_id") for row in summary_cells if isinstance(row, dict)]
        == ["rrcv2-cli-miss-001", "rrcv2-cli-hit-001", "rrcv2-cli-near-001"]
        and [row.get("branch") for row in summary_cells if isinstance(row, dict)]
        == ["miss", "reuse", "miss"]
        and [
            len(cast(list[object], row.get("all_cost_event_ids", [])))
            for row in summary_cells
            if isinstance(row, dict)
        ]
        == [4, 2, 4]
        and all(
            isinstance(row, dict)
            and row.get("terminal_kind") == "accepted"
            and isinstance(row.get("root_cost_event_id"), str)
            and len(cast(str, row["root_cost_event_id"])) == 64
            for row in summary_cells
        )
        and isinstance(summary_cells[2], dict)
        and {"cache_render_rejection", "tier_minus_one"}
        <= set(cast(list[str], summary_cells[2].get("deterministic_stages", [])))
    )
    combined_by_cell = {cast(str, row["cell_id"]): row for row in combined}
    summary_by_cell = {
        cast(str, row["cell_id"]): row
        for row in (summary_cells if isinstance(summary_cells, list) else [])
        if isinstance(row, dict) and isinstance(row.get("cell_id"), str)
    }
    combined_exact = len(combined_by_cell) == 3 and set(combined_by_cell) == set(summary_by_cell)
    if combined_exact:
        for cell_id, record in combined_by_cell.items():
            cell_events = [event for event in events if event.cell_id == cell_id]
            event_ids = {event.cost_event_id for event in cell_events}
            root_events = [
                event for event in cell_events if event.stage == "contextmesh_root_session"
            ]
            record_ids = set(cast(list[str], record["all_cost_event_ids"]))
            summary_row = summary_by_cell[cell_id]
            attempts = cast(list[dict[str, object]], record["attempts"])
            attempt = attempts[0] if len(attempts) == 1 else None
            attempt_id = attempt.get("attempt_id") if attempt is not None else None
            attempt_receipt = attempt.get("receipt") if attempt is not None else None
            nonroot_attempt_ids = {
                event.attempt_id
                for event in cell_events
                if event.stage != "contextmesh_root_session"
            }
            event_task_ids = {event.task_id for event in cell_events}
            event_ordinals = sorted(event.stage_ordinal for event in cell_events)
            matching_oracles = [row for row in oracle_rows if row["attempt_id"] == attempt_id]
            matching_accepted = [row for row in accepted_rows if row["attempt_id"] == attempt_id]
            matching_apply = [
                row
                for row in apply_rows
                if row["cell_id"] == cell_id
                and isinstance(row["output"], dict)
                and row["output"].get("attempt_id") == attempt_id
                and row["output"].get("receipt") == attempt_receipt
            ]
            if (
                event_ids != record_ids
                or record_ids != set(cast(list[str], summary_row.get("all_cost_event_ids", [])))
                or record.get("root_cost_event_id") != summary_row.get("root_cost_event_id")
                or event_task_ids != {summary_row.get("task_id")}
                or event_ordinals != list(range(1, len(cell_events) + 1))
                or len(root_events) != 1
                or record.get("root_cost_event_id") != root_events[0].cost_event_id
                or record.get("root_transcript_sha256") != root_events[0].transcript_sha256
                or record.get("root_final_sha256") != root_events[0].final_message_sha256
                or attempt is None
                or attempt.get("terminal_kind") != "accepted"
                or not isinstance(attempt.get("receipt"), str)
                or not isinstance(attempt.get("apply_event_sha256"), str)
                or not isinstance(attempt.get("terminal_outcome_sha256"), str)
                or summary_row.get("attempt_id") != attempt_id
                or nonroot_attempt_ids != {attempt_id}
                or len(matching_oracles) != 1
                or matching_oracles[0]["status"] != "passed"
                or matching_oracles[0]["score"] is not True
                or len(matching_accepted) != 1
                or matching_accepted[0]["state"] != "accepted"
                or matching_accepted[0]["terminal_outcome_sha256"]
                != attempt.get("terminal_outcome_sha256")
                or matching_accepted[0]["receipt"] != attempt.get("receipt")
                or len(matching_apply) != 1
                or matching_apply[0]["event_sha256"] != attempt.get("apply_event_sha256")
            ):
                combined_exact = False
                break
    expected_stages = {
        "rrcv2-cli-miss-001": {
            "contextmesh_root_session": 1,
            "spec": 2,
            "independent_tests": 3,
            "implement": 4,
        },
        "rrcv2-cli-hit-001": {"contextmesh_root_session": 1, "prime": 2},
        "rrcv2-cli-near-001": {
            "contextmesh_root_session": 1,
            "spec": 2,
            "independent_tests": 3,
            "implement": 4,
        },
    }
    tuple_exact = True
    for task_id, expected in expected_stages.items():
        task_rows = [row for row in rows if row["task_id"] == task_id]
        if {
            cast(str, row["stage"]): cast(int, row["stage_ordinal"]) for row in task_rows
        } != expected:
            tuple_exact = False
            break
        for row in task_rows:
            stage = row["stage"]
            expected_tuple = (
                ("gpt-5.5", "medium", "priority")
                if stage == "contextmesh_root_session"
                else (
                    ("gpt-5.5", "low", "priority")
                    if stage == "spec"
                    else ("gpt-5.6-luna", "low", "priority")
                )
            )
            if (
                row["requested_model"],
                row["requested_reasoning"],
                row["requested_service_tier"],
            ) != expected_tuple:
                tuple_exact = False
                break
    functional_success = (
        terminal.get("status") == "success"
        and terminal.get("returncode") == 0
        and summary_exact
        and combined_exact
        and tuple_exact
        and len(events) == 10
        and len(oracle_rows) == 3
        and all(row["status"] == "passed" and row["score"] for row in oracle_rows)
    )
    if not functional_success:
        raise AnalysisError("V21 functional terminal, combined session, or call matrix differs")
    cell_tokens = {
        cell: cast(int, value["provider_visible_tokens"]) for cell, value in by_cell.items()
    }
    task_cells = {
        cast(str, row["task_id"]): cast(str, row["cell_id"])
        for row in rows
        if row["role"] == "root"
    }
    by_case = {task_id: by_cell[cell_id] for task_id, cell_id in sorted(task_cells.items())}
    descriptive: dict[str, object] = {}
    if {
        "rrcv2-cli-miss-001",
        "rrcv2-cli-hit-001",
        "rrcv2-cli-near-001",
    } <= set(task_cells):
        miss = cell_tokens[task_cells["rrcv2-cli-miss-001"]]
        hit = cell_tokens[task_cells["rrcv2-cli-hit-001"]]
        near = cell_tokens[task_cells["rrcv2-cli-near-001"]]
        descriptive = {
            "hit_tokens": hit,
            "miss_tokens": miss,
            "near_fallback_tokens": near,
            "hit_vs_miss_token_reduction_fraction": (
                None if miss == 0 else f"{(miss - hit) / miss:.9f}"
            ),
            "warning": "dependent single-run task comparison; descriptive, not causal",
        }
    return {
        "analysis_status": "functional_success" if functional_success else "failure_or_incomplete",
        "billing_attested": False,
        "authority_hashes": authorities,
        "by_cell": by_cell,
        "by_case": by_case,
        "by_effective_model": by_effective_model,
        "by_requested_model": by_model,
        "by_role": by_role,
        "by_stage": by_stage,
        "calls": rows,
        "descriptive_efficiency": descriptive,
        "kind": "rrcv2_v21_model_economics",
        "oracle_scores": oracle_rows,
        "price_basis": {
            "claim": "api_equivalent_requested_fast_estimate_only",
            "service_tier": "priority/fast",
            "source": "https://developers.openai.com/api/docs/pricing",
        },
        "product_summary": product_summary,
        "provider_request_cardinality_attested": False,
        "requested_model_cost_status": (
            "definitive_api_equivalent_estimate"
            if total["cost_definitive"]
            else "partial_or_scenario_only"
        ),
        "terminal": terminal,
        "total": total,
        "v": 1,
    }


def recover_failure(producer_root: Path, error: BaseException) -> dict[str, object]:
    """Seal parseable usage while making the incomplete global total explicitly unknown."""

    terminal: object = None
    errors = [f"{type(error).__name__}: {error}"]
    inventory: list[dict[str, object]] = []
    try:
        terminal = json.loads(_read(producer_root / "terminal.json"))
    except (AnalysisError, OSError, json.JSONDecodeError) as exc:
        errors.append(f"terminal: {type(exc).__name__}: {exc}")
    recovered: dict[str, tuple[str, UsageEvidence, str]] = {}
    root_snapshot_cells: set[Path] = set()

    def admit(model: str, usage: UsageEvidence, *, kind: str) -> None:
        prior = recovered.get(usage.transcript_sha256)
        status = "recovered"
        if prior is not None:
            status = "ambiguous_duplicate_digest"
            errors.append(
                f"duplicate transcript digest: {usage.transcript_sha256}: "
                f"{prior[1].source} and {usage.source}"
            )
        else:
            recovered[usage.transcript_sha256] = (model, usage, kind)
        inventory.append(
            {
                "kind": kind,
                "source": usage.source,
                "status": status,
                "transcript_sha256": usage.transcript_sha256,
            }
        )

    round_root = producer_root / "round"
    for path in sorted(round_root.glob("*/root-transcript.jsonl")):
        try:
            usage = parse_native_transcript(
                _read(path),
                source=str(path.relative_to(producer_root)),
                allow_visible_failure=True,
            )
            admit("gpt-5.5", usage, kind="root_transcript")
            root_snapshot_cells.add(path.parent)
            if usage.visible_failure:
                inventory[-1]["status"] = "recovered_with_visible_failure"
                errors.append(f"visible provider failure after recoverable usage: {usage.source}")
        except (AnalysisError, OSError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            inventory.append(
                {"kind": "root_transcript", "source": str(path), "status": "malformed"}
            )
    for path in sorted(round_root.glob("*/root-events.jsonl")):
        try:
            usage = parse_codex_exec_transcript(
                _read(path), source=str(path.relative_to(producer_root))
            )
            inventory.append(
                {
                    "kind": "root_cli_log",
                    "source": usage.source,
                    "status": (
                        "supplemental_after_stop"
                        if path.parent in root_snapshot_cells
                        else "recovered"
                    ),
                    "transcript_sha256": usage.transcript_sha256,
                }
            )
            if path.parent not in root_snapshot_cells:
                admit("gpt-5.5", usage, kind="root_cli_log")
        except (AnalysisError, OSError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            inventory.append({"kind": "root_cli_log", "source": str(path), "status": "malformed"})
    for path in sorted(round_root.glob("*/hook-events.jsonl")):
        try:
            for index, row in enumerate(_jsonl(path), 1):
                inventory.append(
                    {
                        "event": row.get("event"),
                        "kind": "hook_event",
                        "source": f"{path.relative_to(producer_root)}:{index}",
                        "status": "observed",
                        "tool": row.get("tool"),
                    }
                )
        except (AnalysisError, OSError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            inventory.append({"kind": "hook_event", "source": str(path), "status": "malformed"})
    for path in sorted((round_root / "cancellation/children").glob("*.json")):
        try:
            child = json.loads(_read(path))
            if not isinstance(child, dict):
                raise AnalysisError("child registry row is not an object")
            state = child.get("state")
            returncode = child.get("returncode")
            inventory.append(
                {
                    "argv_sha256": child.get("argv_sha256"),
                    "kind": "launched_child",
                    "pid": child.get("pid"),
                    "returncode": returncode,
                    "source": str(path.relative_to(producer_root)),
                    "status": state,
                }
            )
            if state != "terminal" or returncode != 0:
                errors.append(f"launched child is not a clean terminal: {path.name}")
        except (AnalysisError, OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            inventory.append({"kind": "launched_child", "source": str(path), "status": "malformed"})
    for path in sorted(round_root.glob("*/rrc-model-events.jsonl")):
        try:
            rows = _jsonl(path)
        except (AnalysisError, OSError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        for index, row in enumerate(rows, 1):
            try:
                usage = parse_planner_row(row, source=f"{path.relative_to(producer_root)}:{index}")
                model = row.get("model")
                if not isinstance(model, str):
                    raise AnalysisError("recovered planner model is missing")
                admit(model, usage, kind="planner_transcript")
            except AnalysisError as exc:
                errors.append(f"{path.name}:{index}: {type(exc).__name__}: {exc}")
                inventory.append(
                    {
                        "kind": "planner_transcript",
                        "source": f"{path}:{index}",
                        "status": "malformed",
                    }
                )
    database = round_root / "rrcv2.sqlite3"
    event_digests: dict[str, list[str]] = {}
    events_by_digest: dict[str, list[CostEventV1]] = {}
    if database.is_file():
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                worker_raws = [
                    cast(bytes, row[0])
                    for row in connection.execute(
                        "SELECT transcript FROM rrcv2p_attempts_bindings WHERE transcript IS NOT NULL"
                    )
                ]
                raw_events = [
                    cast(bytes, row[0])
                    for row in connection.execute(
                        "SELECT root_cost_event FROM rrcv2p_cells WHERE root_cost_event IS NOT NULL"
                    )
                ]
                raw_events.extend(
                    cast(bytes, row[0])
                    for row in connection.execute(
                        "SELECT cost_event FROM rrcv2_calls WHERE cost_event IS NOT NULL"
                    )
                )
                call_rows = [
                    (cast(str, row[0]), cast(str, row[1]), row[2] is not None)
                    for row in connection.execute(
                        "SELECT call_id,state,cost_event FROM rrcv2_calls ORDER BY call_id"
                    )
                ]
                root_rows = [
                    (cast(str, row[0]), cast(str, row[1]), row[2] is not None)
                    for row in connection.execute(
                        "SELECT root_call_id,state,root_cost_event FROM rrcv2p_cells "
                        "ORDER BY root_call_id"
                    )
                ]
                combined_rows = [
                    cast(bytes, row[0])
                    for row in connection.execute(
                        "SELECT combined_session FROM rrcv2p_cells "
                        "WHERE combined_session IS NOT NULL ORDER BY cell_id"
                    )
                ]
            finally:
                connection.close()
            for raw in worker_raws:
                usage = parse_native_transcript(
                    raw,
                    source="sqlite:recovered-worker",
                    allow_visible_failure=True,
                )
                admit("gpt-5.6-luna", usage, kind="worker_transcript")
                if usage.visible_failure:
                    inventory[-1]["status"] = "recovered_with_visible_failure"
                    errors.append(
                        f"visible provider failure after recoverable usage: {usage.source}"
                    )
            for call_id, state, quantified in [*root_rows, *call_rows]:
                inventory.append(
                    {
                        "call_id": call_id,
                        "kind": "durable_call",
                        "source": "sqlite",
                        "state": state,
                        "status": "quantified" if quantified else "launched_or_unquantified",
                    }
                )
                if not quantified and state not in {"call_prepared", "root_prepared"}:
                    errors.append(f"durable launched call lacks exact usage: {call_id}:{state}")
            for index, raw in enumerate(combined_rows, 1):
                try:
                    record = _combined_record(raw)
                    inventory.append(
                        {
                            "cell_id": record["cell_id"],
                            "kind": "combined_session",
                            "source": f"sqlite:combined-session:{index}",
                            "status": "committed",
                            "cost_event_ids": record["all_cost_event_ids"],
                        }
                    )
                except AnalysisError as exc:
                    errors.append(f"combined-session:{index}: {type(exc).__name__}: {exc}")
                    inventory.append(
                        {
                            "kind": "combined_session",
                            "source": f"sqlite:combined-session:{index}",
                            "status": "malformed",
                        }
                    )
            for index, raw in enumerate(raw_events, 1):
                try:
                    event = _cost_event(raw)
                    event_digests.setdefault(event.transcript_sha256, []).append(
                        event.cost_event_id
                    )
                    events_by_digest.setdefault(event.transcript_sha256, []).append(event)
                    inventory.append(
                        {
                            "cost_event_id": event.cost_event_id,
                            "kind": "cost_event",
                            "source": f"sqlite:cost-event:{index}",
                            "status": "committed",
                            "transcript_sha256": event.transcript_sha256,
                        }
                    )
                except AnalysisError as exc:
                    errors.append(f"cost-event:{index}: {type(exc).__name__}: {exc}")
                    inventory.append(
                        {
                            "kind": "cost_event",
                            "source": f"sqlite:cost-event:{index}",
                            "status": "malformed",
                        }
                    )
        except (AnalysisError, sqlite3.Error) as exc:
            errors.append(f"database: {type(exc).__name__}: {exc}")
            inventory.append({"kind": "database", "source": str(database), "status": "malformed"})
    else:
        inventory.append({"kind": "database", "source": str(database), "status": "absent"})
    for digest, event_ids in event_digests.items():
        if digest not in recovered:
            errors.append(f"launched/committed usage is missing: {','.join(sorted(event_ids))}")
    for digest, (_model, usage, _kind) in recovered.items():
        if digest not in event_digests:
            errors.append(f"orphan provider usage: {usage.source}:{digest}")
    rows: list[dict[str, object]] = []
    source_tasks = {
        "miss": "rrcv2-cli-miss-001",
        "hit": "rrcv2-cli-hit-001",
        "near": "rrcv2-cli-near-001",
    }
    for model, usage, kind in recovered.values():
        matching = events_by_digest.get(usage.transcript_sha256, [])
        event = matching[0] if len(matching) == 1 else None
        if len(matching) > 1:
            errors.append(f"multiple CostEvents reuse transcript: {usage.transcript_sha256}")
        if event is not None:
            try:
                _match(event, usage)
            except AnalysisError as exc:
                errors.append(f"CostEvent usage mismatch: {event.cost_event_id}: {exc}")
        source_parts = Path(usage.source.split(":", 1)[0]).parts
        inferred_case = next(
            (source_tasks[part] for part in source_parts if part in source_tasks), None
        )
        task_id = event.task_id if event is not None else inferred_case or "unknown"
        stage = (
            event.stage
            if event is not None
            else (
                "contextmesh_root_session"
                if kind in {"root_transcript", "root_cli_log"}
                else "unknown"
            )
        )
        role = (
            "root"
            if stage == "contextmesh_root_session"
            else (
                "native_worker"
                if stage == "implement" or kind == "worker_transcript"
                else "planner"
            )
        )
        requested_model = event.requested_model if event is not None else model
        price, price_error = price_call(model=requested_model, usage=usage)
        rows.append(
            {
                "api_equivalent_fast_dollars": (
                    None if price is None else _format_nanodollars(price)
                ),
                "api_equivalent_fast_nanodollars": price,
                "cache_write_input_tokens": usage.cache_write_input_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "cost_scenarios": price_scenarios(model=requested_model, usage=usage),
                "cost_scenario_nanodollars": _price_scenario_nanodollars(
                    model=requested_model, usage=usage
                ),
                "cost_unknown_reason": price_error,
                "effective_model": (
                    event.effective_model
                    if event is not None
                    else usage.effective_model or "unattested"
                ),
                "effective_provider": event.effective_provider
                if event is not None
                else "unattested",
                "effective_reasoning": (
                    event.effective_reasoning
                    if event is not None
                    else usage.effective_reasoning or "unattested"
                ),
                "effective_service_tier": (
                    event.effective_service_tier if event is not None else "unattested"
                ),
                "grouped_effective_model": (
                    usage.effective_model
                    or (event.effective_model if event is not None else "unattested")
                ),
                "identity_attestation": (
                    event.identity_attestation if event is not None else "unattested"
                ),
                "input_tokens": usage.input_tokens,
                "ordinary_input_tokens": usage.ordinary_input_tokens,
                "output_tokens": usage.output_tokens,
                "provider_visible_tokens": usage.total_tokens,
                "reasoning_output_tokens": usage.reasoning_output_tokens,
                "requested_model": requested_model,
                "requested_provider": event.requested_provider if event is not None else "openai",
                "requested_reasoning": (
                    event.requested_reasoning if event is not None else "unattested"
                ),
                "requested_service_tier": (
                    event.requested_service_tier if event is not None else "priority"
                ),
                "role": role,
                "solved_status": "failure_or_incomplete",
                "source": usage.source,
                "stage": stage,
                "task_id": task_id,
                "transcript_sha256": usage.transcript_sha256,
            }
        )
    by_model = _group_rows(rows, "requested_model")
    by_effective_model = _group_rows(rows, "grouped_effective_model")
    by_stage = _group_rows(rows, "stage")
    by_case = _group_rows(rows, "task_id")
    known = _sum_rows(rows)
    authorities: dict[str, str] | None = None
    try:
        authorities = _authority_hashes(producer_root)
    except (AnalysisError, OSError) as exc:
        errors.append(f"authorities: {type(exc).__name__}: {exc}")
    return {
        "analysis_status": "failure_or_incomplete",
        "authority_hashes": authorities,
        "billing_attested": False,
        "by_requested_model": by_model,
        "by_effective_model": by_effective_model,
        "by_case": by_case,
        "by_stage": by_stage,
        "calls": rows,
        "kind": "rrcv2_v21_model_economics",
        "known_subtotal": known,
        "price_basis": {
            "claim": "api_equivalent_requested_fast_estimate_only",
            "service_tier": "priority/fast",
            "source": "https://developers.openai.com/api/docs/pricing",
        },
        "provider_request_cardinality_attested": False,
        "recovery_inventory": inventory,
        "terminal": terminal,
        "total": {
            "api_equivalent_fast_dollars": None,
            "api_equivalent_fast_nanodollars": None,
            "cache_write_input_tokens": None,
            "cached_input_tokens": None,
            "input_tokens": None,
            "known_api_equivalent_fast_dollars": known["known_api_equivalent_fast_dollars"],
            "known_api_equivalent_fast_nanodollars": known["known_api_equivalent_fast_nanodollars"],
            "known_provider_visible_tokens": known["provider_visible_tokens"],
            "ordinary_input_tokens": None,
            "output_tokens": None,
            "provider_visible_tokens": None,
            "reasoning_output_tokens": None,
            "scenario_api_equivalent_fast_dollars": known["scenario_api_equivalent_fast_dollars"],
            "unquantified_consumption": True,
        },
        "unquantified_components": errors,
        "v": 1,
    }


def render_markdown(report: Mapping[str, object]) -> str:
    models = cast(Mapping[str, Mapping[str, object]], report.get("by_requested_model", {}))
    lines = [
        "# RRCv2 V21 live credibility result",
        "",
        f"Status: **{report.get('analysis_status', 'unknown')}**",
        "",
        "Dollar values are requested Fast-mode API-equivalent estimates from the official OpenAI table, not invoice or billing attestations.",
        "",
        "## By requested model",
        "",
        "| Requested model | Calls | Provider-visible tokens | Total input | Ordinary input | Cached input | Cache writes | Output | Reasoning subset | Estimated cost |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model, row in models.items():
        cost = row.get("api_equivalent_fast_dollars")
        scenario = row.get("scenario_api_equivalent_fast_dollars")
        if isinstance(cost, str):
            cost_text = f"${cost}"
        elif isinstance(scenario, dict):
            low = scenario.get("cache_write_priced_as_cached_input_dollars")
            high = scenario.get("cache_write_priced_as_uncached_input_dollars")
            cost_text = f"scenario ${low}–${high}"
        else:
            cost_text = "unknown"
        lines.append(
            f"| {model} | {row.get('calls', 0)} | {row.get('provider_visible_tokens', 0):,} | "
            f"{row.get('input_tokens', 0):,} | {row.get('ordinary_input_tokens', 0):,} | "
            f"{row.get('cached_input_tokens', 0):,} | "
            f"{row.get('cache_write_input_tokens', 0):,} | {row.get('output_tokens', 0):,} | "
            f"{row.get('reasoning_output_tokens', 0):,} | {cost_text} |"
        )
    total = report.get("total")
    if isinstance(total, dict):
        lines.extend(
            [
                "",
                "## Overall",
                "",
                f"- Provider-visible tokens: {total.get('provider_visible_tokens')}",
                f"- API-equivalent Fast cost: {total.get('api_equivalent_fast_dollars')}",
                f"- Known cost subtotal: {total.get('known_api_equivalent_fast_dollars')}",
                f"- Named cost sensitivity: {total.get('scenario_api_equivalent_fast_dollars')}",
            ]
        )
    authorities = report.get("authority_hashes")
    if isinstance(authorities, dict):
        lines.extend(["", "## Sealed authorities", ""])
        lines.extend(f"- {name}: `{value}`" for name, value in sorted(authorities.items()))
    descriptive = report.get("descriptive_efficiency")
    if isinstance(descriptive, dict) and descriptive:
        lines.extend(
            [
                "",
                "## Descriptive efficiency",
                "",
                f"- HIT tokens: {descriptive.get('hit_tokens')}",
                f"- seeded MISS tokens: {descriptive.get('miss_tokens')}",
                f"- HIT vs MISS reduction fraction: {descriptive.get('hit_vs_miss_token_reduction_fraction')}",
                f"- Limitation: {descriptive.get('warning')}",
            ]
        )
    lines.extend(
        [
            "",
            "## Claim limits",
            "",
            "This is one dependent three-case credibility sequence. It does not establish general savings, invoice charges, provider request counts, or production reliability.",
            "",
            "Pricing source: https://developers.openai.com/api/docs/pricing",
            "",
        ]
    )
    return "\n".join(lines)


def render_svg(report: Mapping[str, object]) -> str:
    models = cast(Mapping[str, Mapping[str, object]], report["by_requested_model"])
    calls = cast(Sequence[Mapping[str, object]], report.get("calls", []))
    width, height = 1080, 600
    left, chart_width = 220, 760
    colors = {
        "ordinary_input_tokens": "#2563eb",
        "cached_input_tokens": "#22c55e",
        "cache_write_input_tokens": "#f59e0b",
        "output_tokens": "#ef4444",
    }
    maximum = max([cast(int, row["provider_visible_tokens"]) for row in models.values()] or [1])
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        "<style>text{font-family:ui-sans-serif,system-ui,sans-serif;fill:#111827}.title{font-size:24px;font-weight:700}.label{font-size:16px;font-weight:600}.small{font-size:13px;fill:#4b5563}</style>",
        '<text x="40" y="42" class="title">V21 provider-visible tokens and Fast-mode cost by requested model</text>',
        '<text x="40" y="68" class="small">Reasoning is a subset of output. Cost is API-equivalent, not a billing attestation.</text>',
    ]
    y = 125
    for model, row in models.items():
        parts.append(f'<text x="40" y="{y + 23}" class="label">{html.escape(model)}</text>')
        x = left
        for field in colors:
            value = cast(int, row[field])
            segment = chart_width * value / maximum
            parts.append(
                f'<rect x="{x:.2f}" y="{y}" width="{segment:.2f}" height="32" fill="{colors[field]}"/>'
            )
            x += segment
        total = cast(int, row["provider_visible_tokens"])
        raw_dollars = row.get("api_equivalent_fast_dollars")
        scenario = row.get("scenario_api_equivalent_fast_dollars")
        if isinstance(raw_dollars, str):
            dollars = f"${html.escape(raw_dollars)}"
        elif isinstance(scenario, dict):
            low = scenario.get("cache_write_priced_as_cached_input_dollars", "unknown")
            high = scenario.get("cache_write_priced_as_uncached_input_dollars", "unknown")
            dollars = f"scenario ${html.escape(str(low))}–${html.escape(str(high))}"
        else:
            dollars = "cost unknown; known subtotal $" + html.escape(
                str(row.get("known_api_equivalent_fast_dollars", "0"))
            )
        attestations = sorted(
            {
                str(call.get("identity_attestation", "uncommitted"))
                + ":"
                + str(
                    call.get("transcript_observed_model")
                    or call.get("effective_model")
                    or "unknown"
                )
                for call in calls
                if call.get("requested_model") == model
            }
        )
        parts.append(
            f'<text x="{left}" y="{y + 53}" class="small">{total:,} tokens | {dollars} | {row["calls"]} calls</text>'
        )
        parts.append(
            f'<text x="{left}" y="{y + 72}" class="small">effective evidence: {html.escape(", ".join(attestations) or "unknown")}</text>'
        )
        y += 105
    total = report.get("total")
    total_y = height - 120
    if isinstance(total, dict):
        raw_cost = total.get("api_equivalent_fast_dollars")
        scenario = total.get("scenario_api_equivalent_fast_dollars")
        if isinstance(raw_cost, str):
            cost_label = f"${raw_cost}"
        elif isinstance(scenario, dict):
            low = scenario.get("cache_write_priced_as_cached_input_dollars", "unknown")
            high = scenario.get("cache_write_priced_as_uncached_input_dollars", "unknown")
            cost_label = f"scenario ${low}–${high}"
        else:
            cost_label = "unknown; known subtotal $" + str(
                total.get("known_api_equivalent_fast_dollars", "0")
            )
        parts.append(
            f'<rect x="40" y="{total_y - 25}" width="1000" height="52" rx="8" fill="#f3f4f6"/>'
        )
        parts.append(
            f'<text x="60" y="{total_y + 7}" class="label">Overall API-equivalent Fast cost: {html.escape(cost_label)}</text>'
        )
    authority = report.get("authority_hashes")
    if isinstance(authority, dict):
        price_hash = str(authority.get("price_authority_sha256", "unknown"))
        launch_hash = str(authority.get("launch_manifest_sha256", "unknown"))
        parts.append(
            f'<text x="40" y="{height - 78}" class="small">price authority {html.escape(price_hash)} | launch {html.escape(launch_hash)}</text>'
        )
    legend_y = height - 48
    x = 40
    names = {
        "ordinary_input_tokens": "ordinary input",
        "cached_input_tokens": "cached input",
        "cache_write_input_tokens": "cache write",
        "output_tokens": "output",
    }
    for field, color in colors.items():
        parts.append(f'<rect x="{x}" y="{legend_y}" width="14" height="14" fill="{color}"/>')
        parts.append(f'<text x="{x + 21}" y="{legend_y + 12}" class="small">{names[field]}</text>')
        x += 185
    parts.append("</svg>")
    return "".join(parts)


def _write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        existing = _read(path)
        if stat.S_IMODE(os.lstat(path).st_mode) != 0o600 or existing != raw:
            raise AnalysisError(f"conflicting immutable analysis artifact: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("short analysis write")
            view = view[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path, follow_symlinks=False)
    except FileExistsError:
        existing = _read(path)
        if stat.S_IMODE(os.lstat(path).st_mode) != 0o600 or existing != raw:
            raise AnalysisError(f"conflicting immutable analysis artifact: {path}") from None
    finally:
        temporary.unlink(missing_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("producer_root", type=Path)
    args = parser.parse_args(argv)
    root = args.producer_root.resolve(strict=True)
    try:
        report = analyze(root)
    except (AnalysisError, OSError, sqlite3.Error, ValueError) as exc:
        report = recover_failure(root, exc)
    report_path = root / "v21-analysis.v1.json"
    svg_path = root / "v21-model-economics.svg"
    markdown_path = root / "v21-report.md"
    _write(report_path, canonical_json_bytes(report))
    _write(svg_path, render_svg(report).encode("utf-8"))
    _write(markdown_path, render_markdown(report).encode("utf-8"))
    inventory = {
        "artifacts": [
            {
                "bytes": path.stat().st_size,
                "mode": stat.S_IMODE(path.stat().st_mode),
                "path": path.name,
                "sha256": _sha(_read(path)),
            }
            for path in (report_path, svg_path, markdown_path)
        ],
        "kind": "rrcv2_v21_analysis_inventory",
        "v": 1,
    }
    inventory_path = root / "v21-analysis-inventory.v1.json"
    _write(inventory_path, canonical_json_bytes(inventory))
    print(
        json.dumps(
            {
                "analysis": str(report_path),
                "diagram": str(svg_path),
                "inventory": str(inventory_path),
                "report": str(markdown_path),
            },
            sort_keys=True,
        )
    )
    return 0 if report["analysis_status"] == "functional_success" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AnalysisError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"rrcv2 V21 analyzer: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
