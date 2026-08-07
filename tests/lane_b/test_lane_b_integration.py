from __future__ import annotations

import json
from pathlib import Path

from rrc.contract import Config
from rrc.everos import EverOSClient
from rrc.memory import EverOSRetrieval, task_case_shape
from rrc.model import parse_codex_jsonl
from rrc.pipeline.stubs import FakeModel
from rrc.run import run_proof
from rrc.store import SQLiteTemplateStore
from rrc.workload import two_task_workload


class FakeEverOS(EverOSClient):
    def __init__(self) -> None:
        super().__init__("http://fake.invalid")
        self.indexed_ref: str | None = None
        self.indexed_shapes: list[str] = []
        self.search_args: list[tuple[str, int | None, float | None]] = []
        self.wait_calls = 0

    def index(self, case_shape: str, external_ref: str) -> None:
        self.indexed_ref = external_ref
        self.indexed_shapes.append(case_shape)

    def search(
        self,
        case_shape: str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[tuple[str, float]]:
        self.search_args.append((case_shape, top_k, min_score))
        return [] if self.indexed_ref is None else [(self.indexed_ref, 0.91)]

    def wait_for_index(self, timeout: float = 30.0, poll_interval: float = 0.5) -> None:
        del timeout, poll_interval
        self.wait_calls += 1


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


def test_lane_b_two_store_proof_misses_then_reuses_without_spec(tmp_path: Path) -> None:
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
    assert evidence["stored_external_ref"] == evidence["retrieved_external_ref"]
    assert [call[3] for call in model.calls] == ["spec", "implement", "implement"]
    assert client.wait_calls == 1
    assert all(top_k == 2 and score == 0.4 for _, top_k, score in client.search_args)
    assert len(set(client.indexed_shapes)) == 1


def test_case_shape_removes_values_but_is_stable_across_instances() -> None:
    first, second = two_task_workload()
    first_shape = task_case_shape(first)
    second_shape = task_case_shape(second)

    assert first_shape == second_shape
    assert "return_two" not in first_shape
    assert "return_three" not in second_shape
    assert "RRC_SLOT_VALUES" not in first_shape


def test_codex_jsonl_parser_preserves_split_usage() -> None:
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "artifact"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 7,
                        "total_tokens": 18,
                    },
                }
            ),
        )
    )

    text, usage = parse_codex_jsonl(stdout)

    assert text == "artifact"
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (11, 7, 18)
