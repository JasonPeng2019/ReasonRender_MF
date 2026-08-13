"""Collect deterministic usage evidence from headless Codex or Qwen JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


class CollectionError(ValueError):
    """Raised when a stream cannot be parsed under the Codex capture contract."""


_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _error(line_number: int, message: str) -> CollectionError:
    return CollectionError(f"line {line_number}: {message}")


def _non_negative_int(value: Any, field: str, line_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error(line_number, f"{field} must be a non-negative integer")
    return value


def _empty_totals() -> dict[str, int]:
    return {field: 0 for field in (*_USAGE_FIELDS, "input_new_tokens", "turns")}


def _turn_usage(event: dict[str, Any], line_number: int) -> dict[str, int]:
    raw_usage = event.get("usage")
    if not isinstance(raw_usage, dict):
        raise _error(line_number, "turn.completed usage must be an object")
    usage = {
        field: _non_negative_int(raw_usage.get(field), field, line_number)
        for field in _USAGE_FIELDS
    }
    if usage["cached_input_tokens"] > usage["input_tokens"]:
        raise _error(line_number, "cached_input_tokens must not exceed input_tokens")
    usage["input_new_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    return usage


def _qwen_turn_usage(event: dict[str, Any], line_number: int) -> dict[str, int]:
    raw_usage = event.get("usage")
    if not isinstance(raw_usage, dict):
        raise _error(line_number, "Qwen result usage must be an object")
    usage = {
        "input_tokens": _non_negative_int(raw_usage.get("input_tokens"), "input_tokens", line_number),
        "cached_input_tokens": _non_negative_int(
            raw_usage.get("cache_read_input_tokens", 0), "cache_read_input_tokens", line_number
        ),
        "cache_write_input_tokens": 0,
        "output_tokens": _non_negative_int(raw_usage.get("output_tokens"), "output_tokens", line_number),
        "reasoning_output_tokens": 0,
    }
    if usage["cached_input_tokens"] > usage["input_tokens"]:
        raise _error(line_number, "cached_input_tokens must not exceed input_tokens")
    usage["input_new_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    return usage


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect_codex_stream(stream_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Collect one persistent Codex exec stream into ``output_dir/usage.json``."""

    source = Path(stream_path)
    thread_id: str | None = None
    turns: list[dict[str, int]] = []
    failed = False
    provider = "codex"
    try:
        raw_stream = source.read_bytes()
    except OSError as exc:
        raise CollectionError(f"could not read stream {source}: {exc}") from exc
    encoding = "utf-16" if raw_stream.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    if encoding == "utf-8" and b"\x00" in raw_stream:
        raise CollectionError(f"stream {source} contains NUL-byte corruption")
    try:
        lines = raw_stream.decode(encoding).splitlines()
    except UnicodeDecodeError as exc:
        raise CollectionError(f"could not decode stream {source} as {encoding}: {exc}") from exc

    for line_number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _error(line_number, f"malformed JSON ({exc.msg})") from exc
        if not isinstance(event, dict):
            raise _error(line_number, "event must be a JSON object")

        event_type = event.get("type")
        if event_type == "thread.started":
            value = event.get("thread_id")
            if not isinstance(value, str):
                raise _error(line_number, "thread.started thread_id must be a string")
            if thread_id is not None and thread_id != value:
                raise _error(line_number, "thread.started thread_id must not change")
            thread_id = value
        elif event_type == "system" and isinstance(event.get("session_id"), str):
            provider = "qwen"
            value = event["session_id"]
            if thread_id is not None and thread_id != value:
                raise _error(line_number, "Qwen session_id must not change")
            thread_id = value
        elif event_type == "turn.completed":
            turns.append(_turn_usage(event, line_number))
        elif event_type == "result":
            provider = "qwen"
            result = event.get("result")
            valid_result = (
                event.get("subtype") == "success"
                and event.get("is_error") is not True
                and isinstance(result, str)
                and not result.lstrip().startswith("[API Error:")
            )
            if valid_result:
                turns.append(_qwen_turn_usage(event, line_number))
            else:
                failed = True
        elif event_type in {"turn.failed", "error"}:
            failed = True

    totals = _empty_totals()
    turn_entries: list[dict[str, int]] = []
    for turn_number, turn in enumerate(turns, start=1):
        turn_entries.append({"turn": turn_number, **turn})
        for field in totals:
            if field != "turns":
                totals[field] += turn[field]
        totals["turns"] += 1

    invalid_reasons: list[str] = []
    if thread_id is None:
        invalid_reasons.append("missing_thread_started")
    if not turns:
        invalid_reasons.append("incomplete")
    if failed:
        invalid_reasons.append("turn_failed")
    usage = {
        "schema_version": 1,
        "provider": provider,
        "completed": bool(turns),
        "valid": not invalid_reasons,
        "invalid_reasons": invalid_reasons,
        "thread_id": thread_id,
        "turns": turn_entries,
        "totals": totals,
    }

    destination = Path(output_dir)
    try:
        destination.mkdir(parents=True, exist_ok=True)
        _write_json(destination / "usage.json", usage)
    except OSError as exc:
        raise CollectionError(f"could not write collection artifacts in {destination}: {exc}") from exc
    return usage


def _pilot_totals(usages: Sequence[dict[str, Any]]) -> dict[str, int]:
    totals = _empty_totals()
    for usage in usages:
        for field in totals:
            totals[field] += int(usage["totals"][field])
    return totals


def collect_codex_pilot(
    orchestrator_stream: str | Path,
    worker_streams: Sequence[str | Path],
    output_dir: str | Path,
) -> dict[str, Any]:
    """Collect one Terra stream plus exactly four ordered DeepSeek worker streams."""

    if len(worker_streams) != 4:
        raise CollectionError("Codex-pilot requires exactly four worker streams")

    destination = Path(output_dir)
    orchestrator = collect_codex_stream(orchestrator_stream, destination / "orchestrator")
    workers = [
        collect_codex_stream(stream, destination / "workers" / f"worker-{index:02d}")
        for index, stream in enumerate(worker_streams, start=1)
    ]
    roles = (("orchestrator", orchestrator), *(
        (f"worker-{index:02d}", usage) for index, usage in enumerate(workers, start=1)
    ))
    invalid_roles = [role for role, usage in roles if not usage["valid"]]
    pilot = {
        "schema_version": 1,
        "label": "Codex-pilot",
        "provider": "codex",
        "topology": "1+4",
        "orchestrators": 1,
        "workers": 4,
        "valid": not invalid_roles,
        "invalid_roles": invalid_roles,
        "orchestrator": orchestrator,
        "worker_streams": workers,
        "totals": _pilot_totals([orchestrator, *workers]),
    }
    try:
        destination.mkdir(parents=True, exist_ok=True)
        _write_json(destination / "pilot-usage.json", pilot)
    except OSError as exc:
        raise CollectionError(f"could not write pilot artifact in {destination}: {exc}") from exc
    return pilot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    stream_parser = modes.add_parser("stream")
    stream_parser.add_argument("stream_path")
    stream_parser.add_argument("output_dir")
    pilot_parser = modes.add_parser("pilot")
    pilot_parser.add_argument("orchestrator_stream")
    pilot_parser.add_argument("worker_streams", nargs=4)
    pilot_parser.add_argument("output_dir")
    args = parser.parse_args(argv)
    try:
        if args.mode == "stream":
            usage = collect_codex_stream(args.stream_path, args.output_dir)
            valid = usage["valid"]
        else:
            pilot = collect_codex_pilot(
                args.orchestrator_stream, args.worker_streams, args.output_dir
            )
            valid = pilot["valid"]
    except CollectionError as exc:
        print(f"CollectionError: {exc}", file=sys.stderr)
        return 2
    return 0 if valid else 2


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI.
    raise SystemExit(main())
