from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.collect_codex import CollectionError, collect_codex_pilot, collect_codex_stream


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "codex_stream"


def write_events(path: Path, events: list[object]) -> None:
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def test_complete_stream_records_raw_and_derived_codex_usage(tmp_path: Path) -> None:
    usage = collect_codex_stream(FIXTURES / "complete.jsonl", tmp_path)

    assert usage["valid"] is True
    assert usage["thread_id"] == "thread-complete"
    assert usage["totals"] == {
        "input_tokens": 100,
        "cached_input_tokens": 30,
        "cache_write_input_tokens": 5,
        "output_tokens": 7,
        "reasoning_output_tokens": 3,
        "input_new_tokens": 70,
        "turns": 1,
    }
    assert json.loads((tmp_path / "usage.json").read_text(encoding="utf-8")) == usage


def test_zero_cached_codex_usage_is_valid(tmp_path: Path) -> None:
    source = tmp_path / "zero.jsonl"
    write_events(
        source,
        [
            {"type": "thread.started", "thread_id": "zero-cache"},
            {"type": "turn.completed", "usage": {field: 0 for field in (
                "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens",
            )}},
        ],
    )

    usage = collect_codex_stream(source, tmp_path / "output")

    assert usage["valid"] is True
    assert usage["totals"]["cached_input_tokens"] == 0


def test_qwen_stream_records_session_completion_and_usage(tmp_path: Path) -> None:
    source = tmp_path / "qwen.jsonl"
    write_events(
        source,
        [
            {"type": "system", "subtype": "init", "session_id": "qwen-session"},
            {
                "type": "result",
                "subtype": "success",
                "session_id": "qwen-session",
                "is_error": False,
                "result": "done",
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 9,
                    "cache_read_input_tokens": 20,
                },
            },
        ],
    )

    usage = collect_codex_stream(source, tmp_path / "output")

    assert usage["provider"] == "qwen"
    assert usage["thread_id"] == "qwen-session"
    assert usage["valid"] is True
    assert usage["totals"] == {
        "input_tokens": 120,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 0,
        "output_tokens": 9,
        "reasoning_output_tokens": 0,
        "input_new_tokens": 100,
        "turns": 1,
    }


def test_qwen_api_error_is_not_a_completed_turn(tmp_path: Path) -> None:
    source = tmp_path / "qwen-error.jsonl"
    write_events(
        source,
        [
            {"type": "system", "subtype": "init", "session_id": "qwen-session"},
            {
                "type": "result",
                "subtype": "success",
                "session_id": "qwen-session",
                "is_error": False,
                "result": "[API Error: provider rejected request]",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        ],
    )

    usage = collect_codex_stream(source, tmp_path / "output")

    assert usage["valid"] is False
    assert "incomplete" in usage["invalid_reasons"]
    assert "turn_failed" in usage["invalid_reasons"]


def test_utf16_powershell_redirect_stream_is_collected(tmp_path: Path) -> None:
    source = tmp_path / "powershell-stream.jsonl"
    source.write_text(
        (FIXTURES / "complete.jsonl").read_text(encoding="utf-8"),
        encoding="utf-16",
    )

    usage = collect_codex_stream(source, tmp_path / "output")

    assert usage["valid"] is True
    assert usage["thread_id"] == "thread-complete"


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [("incomplete.jsonl", "incomplete"), ("failed.jsonl", "turn_failed")],
)
def test_readable_invalid_streams_write_usage(tmp_path: Path, fixture: str, reason: str) -> None:
    usage = collect_codex_stream(FIXTURES / fixture, tmp_path / fixture)

    assert usage["valid"] is False
    assert reason in usage["invalid_reasons"]
    assert (tmp_path / fixture / "usage.json").exists()


def test_malformed_stream_has_line_specific_error(tmp_path: Path) -> None:
    with pytest.raises(CollectionError, match=r"line 2: malformed JSON"):
        collect_codex_stream(FIXTURES / "malformed.jsonl", tmp_path)


def test_sparse_or_corrupt_stream_is_rejected_before_usage_collection(tmp_path: Path) -> None:
    source = tmp_path / "corrupt.jsonl"
    source.write_bytes(b'{"type":"thread.started"}\n\x00{"type":"turn.completed"}\n')

    with pytest.raises(CollectionError, match="NUL-byte corruption"):
        collect_codex_stream(source, tmp_path / "output")


def test_cached_tokens_cannot_exceed_input(tmp_path: Path) -> None:
    source = tmp_path / "invalid.jsonl"
    write_events(
        source,
        [
            {"type": "thread.started", "thread_id": "bad"},
            {"type": "turn.completed", "usage": {
                "input_tokens": 1, "cached_input_tokens": 2,
                "cache_write_input_tokens": 0, "output_tokens": 0,
                "reasoning_output_tokens": 0,
            }},
        ],
    )
    with pytest.raises(CollectionError, match="cached_input_tokens must not exceed input_tokens"):
        collect_codex_stream(source, tmp_path / "output")


def test_pilot_writes_one_plus_four_isolated_usage_records(tmp_path: Path) -> None:
    workers = [FIXTURES / "complete.jsonl"] * 4
    pilot = collect_codex_pilot(FIXTURES / "complete.jsonl", workers, tmp_path)

    assert pilot["label"] == "Codex-pilot"
    assert pilot["topology"] == "1+4"
    assert pilot["valid"] is True
    assert len(pilot["worker_streams"]) == 4
    assert pilot["totals"]["input_tokens"] == 500
    assert (tmp_path / "orchestrator" / "usage.json").exists()
    assert all((tmp_path / "workers" / f"worker-{index:02d}" / "usage.json").exists() for index in range(1, 5))
    assert json.loads((tmp_path / "pilot-usage.json").read_text(encoding="utf-8")) == pilot


def test_pilot_requires_exactly_four_workers(tmp_path: Path) -> None:
    with pytest.raises(CollectionError, match="exactly four worker streams"):
        collect_codex_pilot(FIXTURES / "complete.jsonl", [FIXTURES / "complete.jsonl"] * 3, tmp_path)


def test_pilot_invalid_worker_is_retained_and_invalidates_aggregate(tmp_path: Path) -> None:
    pilot = collect_codex_pilot(
        FIXTURES / "complete.jsonl",
        [FIXTURES / "complete.jsonl"] * 3 + [FIXTURES / "failed.jsonl"],
        tmp_path,
    )
    assert pilot["valid"] is False
    assert pilot["invalid_roles"] == ["worker-04"]
    assert (tmp_path / "workers" / "worker-04" / "usage.json").exists()


def test_cli_exit_statuses(tmp_path: Path) -> None:
    command = [sys.executable, str(ROOT / "harness" / "collect_codex.py"), "stream"]
    good = subprocess.run(
        [*command, str(FIXTURES / "complete.jsonl"), str(tmp_path / "good")],
        capture_output=True,
        text=True,
        check=False,
    )
    bad = subprocess.run(
        [*command, str(FIXTURES / "incomplete.jsonl"), str(tmp_path / "bad")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert good.returncode == 0
    assert bad.returncode == 2
