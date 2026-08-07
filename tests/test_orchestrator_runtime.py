import json
from typing import Any

from rrc.contract import PlanSpecPacket, Task
from rrc.everos import EverOSClient
from rrc.orchestrator_policy import POLICY
from rrc.orchestrator_runtime import OrchestratorRuntime
from rrc.store import SQLiteTemplateStore


class FakeCompletion:
    def __init__(self, outputs: list[tuple[str, int]], label: str, events: list[str]) -> None:
        self.outputs = outputs
        self.label = label
        self.events = events
        self.calls: list[tuple[str, str]] = []

    def __call__(self, prompt: str, model: str) -> tuple[str, int]:
        self.calls.append((prompt, model))
        self.events.append(self.label)
        return self.outputs[len(self.calls) - 1]


class RecordingStore(SQLiteTemplateStore):
    def __init__(self, database: str, events: list[str]) -> None:
        super().__init__(database)
        self.events = events

    def put_plan_spec(self, template: Any) -> None:
        self.events.append("store")
        super().put_plan_spec(template)


class FakeCaseIndex(EverOSClient):
    def __init__(self, events: list[str]) -> None:
        super().__init__(base_url="http://fake.invalid")
        self.events = events
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.indexed_ref: str | None = None

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, payload))
        self.events.append("everos:" + path)
        if path == "/api/v2/memory/add":
            external_ref = payload.get("external_ref")
            assert isinstance(external_ref, str)
            self.indexed_ref = external_ref
            return {}
        if path == "/api/v2/memory/search" and self.indexed_ref is not None:
            return {"data": {"episodes": [{"external_ref": self.indexed_ref, "score": 0.91}]}}
        return {}


def _lean_packet() -> PlanSpecPacket:
    return PlanSpecPacket.from_dict(
        {
            "signature": "GENERIC_SIGNATURE_{source}",
            "slot_names": ["source", "kind", "failure"],
            "plan": {
                "steps": [
                    "Define the {source} to {kind} contract.",
                    "Handle the {failure} edge without changing normal behavior.",
                ],
                "invariants": [],
                "edges": ["Report the {failure} condition clearly."],
                "constraints": [],
            },
            "specification": "GENERIC_SPECIFICATION_{source} produces {kind}.",
            "acceptance": [
                "Normal {source} input produces {kind}.",
                "The {failure} edge is reported.",
            ],
            "non_goals": ["Do not redesign unrelated behavior."],
            "write_paths": ["rrc/product.py"],
            "read_first": ["rrc/contract.py", "rrc/product.py", "tests/test_product.py"],
        }
    )


def _detailed_packet() -> PlanSpecPacket:
    return PlanSpecPacket.from_dict(
        {
            "signature": "def build_{source}({schema}) -> {output}",
            "slot_names": ["source", "schema", "output", "errors", "mode"],
            "plan": {
                "steps": [
                    "Normalize {source} according to {schema}.",
                    "Produce {output} while preserving {errors}.",
                    "Apply {mode} constraints before publishing the result.",
                ],
                "invariants": ["The {output} preserves the declared {schema} contract."],
                "edges": ["Reject malformed {source} with the {errors} behavior."],
                "constraints": ["The {mode} path must not change the {schema} contract."],
            },
            "specification": "GENERIC_DETAILED_SPEC for {source} and {output}.",
            "acceptance": [
                "Valid {source} follows {schema} and yields {output}.",
                "Invalid input follows the {errors} rule.",
            ],
            "non_goals": ["Do not add an unrelated migration."],
            "write_paths": ["rrc/product.py", "tests/test_product.py"],
            "read_first": ["rrc/contract.py", "rrc/product.py", "rrc/policy.py"],
        }
    )


def test_lean_miss_hit_privacy_and_everos_boundary(tmp_path) -> None:
    case_shape = "Implement {source} conversion into {kind} and report {failure} consistently"
    first_values = {
        "source": "SRC_UNIQUE_ASTERISM_7F2C",
        "kind": "KIND_UNIQUE_CINNABAR_91D4",
        "failure": "FAIL_UNIQUE_TUNGSTEN_5A8E",
    }
    second_values = {
        "source": "SRC_UNIQUE_MOONSTONE_3B6E",
        "kind": "KIND_UNIQUE_VERDIGRIS_82A1",
        "failure": "FAIL_UNIQUE_SAFFRON_4D9C",
    }
    raw_oracle = "def test_oracle_secret(): assert 'ORACLE_RAW_NEVER_SENT_6E1B'"
    task_one = Task(
        task_id="lean-one",
        family="conversion",
        case_shape=case_shape,
        slot_values=first_values,
        oracle_tests=raw_oracle,
    )
    task_two = Task(
        task_id="lean-two",
        family="conversion",
        case_shape=case_shape,
        slot_values=second_values,
        oracle_tests=raw_oracle,
    )
    packet = _lean_packet()
    events: list[str] = []
    planner = FakeCompletion([(json.dumps(packet.to_dict()), 17)], "planner", events)
    worker = FakeCompletion([("worker-first", 23), ("worker-second", 29)], "worker", events)
    store = RecordingStore(str(tmp_path / "templates.sqlite3"), events)
    everos = FakeCaseIndex(events)
    runtime = OrchestratorRuntime(planner, "fake-planner", worker, "fake-worker", store, everos)

    miss = runtime.run(task_one)

    decision = POLICY.decide(task_one)
    assert miss.hit is False
    assert miss.profile == "lean"
    assert miss.planner_tokens == 17
    assert miss.packet == packet
    assert POLICY.validate_packet(task_one, miss.packet, decision)
    assert miss.packet_token_budget == decision.estimated_implementation_tokens // 4
    assert events.index("worker") < events.index("store") < events.index("everos:/api/v2/memory/add")
    assert events.index("everos:/api/v2/memory/add") < events.index("everos:/api/v2/memory/flush")
    stored = store.get_plan_spec(miss.external_ref)
    assert stored is not None
    assert stored.packet == packet

    planner_prompt = planner.calls[0][0]
    assert "The controller supplies only the stable task shape" in planner_prompt
    assert "Stable case_shape:\n" + case_shape in planner_prompt
    assert 'Declared slot names:\n["failure","kind","source"]' in planner_prompt
    assert "Oracle-check count:\n1" in planner_prompt
    assert raw_oracle not in planner_prompt
    for value in first_values.values():
        assert value not in planner_prompt

    worker_prompt_one = worker.calls[0][0]
    for value in first_values.values():
        assert value in worker_prompt_one
    for value in second_values.values():
        assert value not in worker_prompt_one

    hit = runtime.run(task_two)

    assert hit.hit is True
    assert hit.external_ref == miss.external_ref
    assert hit.score == 0.91
    assert hit.packet == miss.packet
    assert hit.worker_output == "worker-second"
    assert len(planner.calls) == 1
    worker_prompt_two = worker.calls[1][0]
    for value in second_values.values():
        assert value in worker_prompt_two
    for value in first_values.values():
        assert value not in worker_prompt_two

    search_posts = [payload for path, payload in everos.posts if path == "/api/v2/memory/search"]
    add_posts = [payload for path, payload in everos.posts if path == "/api/v2/memory/add"]
    flush_posts = [payload for path, payload in everos.posts if path == "/api/v2/memory/flush"]
    assert len(search_posts) == 2
    assert len(add_posts) == len(flush_posts) == 1
    assert all(payload["query"] == case_shape for payload in search_posts)
    add_payload = add_posts[0]
    assert add_payload["external_ref"] == miss.external_ref
    assert add_payload["messages"][0]["content"] == case_shape
    assert "packet" not in add_payload
    for payload in (*search_posts, *add_posts, *flush_posts):
        serialized = json.dumps(payload, sort_keys=True)
        for value in (*first_values.values(), *second_values.values()):
            assert value not in serialized
        assert "GENERIC_SIGNATURE" not in serialized
        assert "GENERIC_SPECIFICATION" not in serialized


def test_detailed_miss_packet_shape_and_budget(tmp_path) -> None:
    task = Task(
        task_id="detailed-one",
        family="normalization",
        case_shape=(
            "Implement {source} using {schema} to produce {output} while preserving "
            "{errors} under {mode} behavior"
        ),
        slot_values={
            "source": "SRC_UNIQUE_BASALT_17A2",
            "schema": "SCHEMA_UNIQUE_TOPAZ_28B3",
            "output": "OUTPUT_UNIQUE_LICHEN_39C4",
            "errors": "ERROR_UNIQUE_PLATINUM_40D5",
            "mode": "MODE_UNIQUE_COPPER_51E6",
        },
        oracle_tests="def test_one(): pass\ndef test_two(): pass\ndef test_three(): pass",
    )
    packet = _detailed_packet()
    events: list[str] = []
    planner = FakeCompletion([(json.dumps(packet.to_dict()), 31)], "planner", events)
    worker = FakeCompletion([("detailed-worker", 37)], "worker", events)
    store = RecordingStore(str(tmp_path / "detailed.sqlite3"), events)
    everos = FakeCaseIndex(events)
    result = OrchestratorRuntime(
        planner, "fake-planner", worker, "fake-worker", store, everos
    ).run(task)

    decision = POLICY.decide(task)
    assert result.hit is False
    assert result.profile == "detailed"
    assert decision.oracle_count == 3
    assert decision.packet_token_budget == decision.estimated_implementation_tokens // 4
    assert result.packet_token_budget == decision.packet_token_budget
    assert 2 <= len(result.packet.plan.steps) <= 4
    assert result.packet.plan.invariants
    assert result.packet.plan.edges
    assert result.packet.plan.constraints
    assert 2 <= len(result.packet.acceptance) <= 4
    assert POLICY.packet_token_count(task, result.packet) <= decision.packet_token_budget
    assert POLICY.validate_packet(task, result.packet, decision)
