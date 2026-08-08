from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from rrc.contract import Config
from rrc.everos import EverOSClient
from rrc.memory import EverOSRetrieval
from rrc.pipeline.stubs import FakeModel
from rrc.run import run_proof
from rrc.store import SQLiteTemplateStore


class FakeEverOS(EverOSClient):
    def __init__(self) -> None:
        super().__init__("http://fake.invalid")
        self.indexed_ref: str | None = None
        self.search_args: list[tuple[str, int | None, float | None]] = []

    def index(self, case_shape: str, external_ref: str) -> None:
        assert "return_two" not in case_shape
        assert "return_three" not in case_shape
        assert "RRC_SLOT_VALUES" not in case_shape
        self.indexed_ref = external_ref

    def search(
        self,
        case_shape: str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[tuple[str, float]]:
        assert "return_two" not in case_shape
        assert "return_three" not in case_shape
        assert "RRC_SLOT_VALUES" not in case_shape
        self.search_args.append((case_shape, top_k, min_score))
        return [] if self.indexed_ref is None else [(self.indexed_ref, 0.91)]

    def wait_for_index(self, timeout: float = 30.0, poll_interval: float = 0.5) -> None:
        del timeout, poll_interval


def _spec_json() -> str:
    return json.dumps(
        {
            "plan": "Implement return_two and always return 2.",
            "signature": "def return_two(value: int) -> int",
            "contract": "Return the constant 2 for every integer input.",
            "tests": ["def test_behavior():\n    assert return_two(99) == 2"],
            "slots": {
                "entity": None,
                "identifiers": ["return_two"],
                "types": [],
                "fields": [],
                "constants": ["2"],
                "edge_values": [],
                "values": {"function": "return_two", "number": "2"},
            },
        },
        separators=(",", ":"),
    )


def _load_live_meter() -> object:
    path = Path(__file__).resolve().parents[1] / "contextmesh" / "scripts" / "live_meter.py"
    spec = importlib.util.spec_from_file_location("full_pipeline_live_meter", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_full_pipeline_reduces_tokens_across_lane_b_and_contextmesh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeEverOS()
    store = SQLiteTemplateStore(tmp_path / "templates.sqlite")
    retrieval = EverOSRetrieval(store, client)
    model = FakeModel(
        {
            "spec": [_spec_json()],
            "implement": [
                "def return_two(value: int) -> int:\n    return 2",
                "def return_three(value: int) -> int:\n    return 3",
            ],
        }
    )

    evidence = run_proof(
        client,
        retrieval,
        model,
        tmp_path / "evidence.json",
        cfg=Config(top_k=2, tau_floor=0.4),
    )

    assert evidence["pass"] is True
    assert evidence["outcomes"][0]["branch"] == "miss"
    assert evidence["outcomes"][1]["branch"] == "reuse"
    assert evidence["stored_external_ref"] == evidence["retrieved_external_ref"]
    assert evidence["stored_external_ref"] is not None
    assert [call[3] for call in model.calls] == ["spec", "implement", "implement"]
    assert all(top_k == 2 and score == 0.4 for _, top_k, score in client.search_args)

    root = tmp_path
    runs = root / "runs"
    (runs / "demo-tui" / "b").mkdir(parents=True)
    (runs / "demo-tui" / "round").write_text("r1\n", encoding="utf-8")
    (runs / "tokens.jsonl").write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "session": "demo-r1-a",
                        "input_tokens": 700,
                        "output_tokens": 300,
                        "measurement_state": "exact",
                    }
                ),
                json.dumps(
                    {
                        "session": "demo-r1-b",
                        "input_tokens": 400,
                        "output_tokens": 200,
                        "measurement_state": "exact",
                    }
                ),
                json.dumps(
                    {
                        "session": "demo-r1-b-summarizer",
                        "input_tokens": 60,
                        "output_tokens": 40,
                        "measurement_state": "exact",
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (runs / "demo-tui" / "b" / "metrics-r1.jsonl").write_text(
        json.dumps({"event": "digest_hit", "savedTokens": 400}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("CONTEXTMESH_ROOT", str(root))
    live_meter = _load_live_meter()
    report = live_meter.render()  # type: ignore[attr-defined]

    assert "MEMORY LAYER REMOVED 400" in report
    assert "B 300 tok below A" in report
    assert "summarizer 100" in report


def test_tui_context_metric_reports_warm_total_700() -> None:
    if shutil.which("bun") is None:
        pytest.skip("Install Bun and add it to PATH to run this test.")

    app = Path(__file__).resolve().parents[1] / "opencode" / "packages" / "app"
    script = """
const { getSessionContext } = await import("./src/components/session/session-context-metrics.ts")
const messages = [{
  role: "assistant",
  providerID: "openai",
  modelID: "gpt-4.1",
  tokens: { input: 400, output: 100, reasoning: 100, cache: { read: 50, write: 50 } },
}]
const context = getSessionContext(messages)
if (context?.total !== 700) throw new Error(`warm total was ${context?.total}`)
console.log(context.total)
"""
    result = subprocess.run(
        ["bun", "-e", script],
        cwd=app,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip().splitlines()[-1] == "700"
