from __future__ import annotations

import argparse
import json
from pathlib import Path

from contextmesh.scripts.rrcv2_demo_prompt import render
from contextmesh.scripts.rrcv2_product_cell import prepare_root
from rrc.attempts import AttemptRepository
from rrc.cell_journal import SQLiteCellJournal
from rrc.contextmesh import coding_assignment_from_message
from rrc.contextmesh_runtime import ContextMeshController
from rrc.contract import (
    Candidate,
    Completion,
    Config,
    ModelRole,
    RunContext,
    ScoreV1,
    Slots,
    Spec,
    Usage,
)
from rrc.dispatch_permit import AuthorityRef
from rrc.journal import SQLiteRRCRepository
from rrc.pipeline.template import templatize
from rrc.product_runtime import ContextMeshProductModel, product_authority_refs
from rrc.retrieval import SQLiteHybridRetrieval


class _PrepareModel:
    provider = "openai"

    def complete(self, role: ModelRole, prompt: str, ctx: RunContext, stage: str) -> Completion:
        del role, prompt, ctx
        if stage == "spec":
            text = json.dumps(
                {
                    "contract": "Return Reading(value) and preserve the public API.",
                    "plan": "Construct the existing dataclass.",
                    "signature": "def normalize_reading(value: int) -> Reading",
                    "slots": {
                        "constants": [],
                        "edge_values": [],
                        "entity": None,
                        "fields": [],
                        "identifiers": [],
                        "types": [],
                    },
                    "tests": ["def test_spec():\n    assert normalize_reading(2) == Reading(2)"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        elif stage == "independent_tests":
            text = json.dumps(
                {
                    "tests": [
                        "def test_independent():\n    assert normalize_reading(5).value == 5"
                    ],
                    "v": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            raise AssertionError(f"unexpected preparation stage {stage}")
        return Completion(text, Usage(10, 2, 12), "test-model")


class _PrimeUnfitPrepareModel(_PrepareModel):
    def __init__(self) -> None:
        self.stages: list[str] = []

    def complete(self, role: ModelRole, prompt: str, ctx: RunContext, stage: str) -> Completion:
        self.stages.append(stage)
        if stage == "metadata_fill":
            text = json.dumps(
                {
                    "authority": "small_model",
                    "family": "reading_normalization",
                    "primary": "normalize_reading",
                    "shape": {"arg_types": ["int"], "arity": 1, "fields": []},
                    "slot_values": {},
                    "v": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            return Completion(text, Usage(10, 2, 12), "test-model")
        if stage == "prime":
            return Completion('{"unfit":true}', Usage(10, 2, 12), "test-model")
        return super().complete(role, prompt, ctx, stage)


class _NearReadingRetrieval:
    def __init__(self, repository: SQLiteRRCRepository) -> None:
        self.authority_id = repository.authority_id
        self.database_uuid = repository.database_uuid
        self.template = templatize(
            Spec(
                "Construct the Reading.",
                "def normalize_reading(value: str) -> Reading",
                "Return Reading(value).",
                ("def test_spec():\n    assert normalize_reading('x') == Reading('x')",),
                Slots(),
            ),
            ("def test_independent():\n    assert normalize_reading('y').value == 'y'",),
            primary="normalize_reading",
        )

    def retrieve(self, task, cfg) -> list[Candidate]:
        del task, cfg
        return [Candidate(self.template.external_ref, ScoreV1(1, 1))]

    def get_template(self, external_ref: str):
        return self.template if external_ref == self.template.external_ref else None

    def classify(self, task, external_ref: str) -> str:
        del task, external_ref
        return "near"


def test_real_root_binding_authorizes_controller_and_native_worker_before_launch(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).parents[1]
    target = tmp_path / "target"
    target.mkdir()
    task_path = tmp_path / "task-envelope.json"
    prompt = render(
        repository=repository_root,
        target=target,
        mode="cold",
        root_sentinel="root-private",
        parent_history_sentinel="parent-private",
        task_envelope_output=task_path,
    )
    assignment = coding_assignment_from_message(
        next(line for line in prompt.splitlines() if line.startswith("RRCV2_CODING_ASSIGNMENT_V1:"))
    )
    assert assignment is not None
    database = tmp_path / "rrcv2.sqlite3"
    authority_root = tmp_path / "cell-authority"
    prepare_root(
        argparse.Namespace(
            repository=repository_root,
            database=database,
            authority_root=authority_root,
            task_envelope=task_path,
            cell_id="demo-cell",
            run_id="demo-run",
            arm="rrc_cold",
            session_id="launch-demo",
        )
    )
    task_raw = task_path.read_bytes()
    task_ref = AuthorityRef(
        task_path, __import__("hashlib").sha256(task_raw).hexdigest(), len(task_raw)
    )
    refs = product_authority_refs(repository_root)
    with SQLiteRRCRepository(database) as repository:
        attempts = AttemptRepository(repository)
        controller = ContextMeshController(
            attempts=attempts,
            model=_PrepareModel(),
            retrieval=SQLiteHybridRetrieval(repository),
            config=Config("owner"),
            target_root=target,
            attempts_root=tmp_path / "attempts",
            route_id="rrcv2-coding-v1",
            round_id="demo-run",
        )

        def model_factory(attempt, envelope):
            del envelope
            cells = SQLiteCellJournal(repository, authority_root=authority_root)
            root = cells.load_root_started("demo-cell")
            bound = cells.bind_attempt(
                root,
                tool_use_id="tool-1",
                attempt_id=attempt.attempt_id,
                task_envelope_sha256=task_ref.sha256,
                expected_generation=root.cell.generation,
            )
            return ContextMeshProductModel(
                controller.model,
                repo=repository_root,
                refs=refs,
                task_envelope_ref=task_ref,
                cell_attempt_binding_ref=bound.binding_ref,
                cell_journal=cells,
                run_id="demo-run",
                replicate_id="interactive",
                arm="rrc_cold",
                cell_id="demo-cell",
            )

        prepared = controller.prepare(assignment, tool_use_id="tool-1", model_factory=model_factory)
        inventory = repository.load_call_inventory(prepared.prepared.attempt.attempt_id)
        assert len(inventory) == 3
        assert all(len(call_id) == 64 for call_id, _state, _cost in inventory)
        assert [state for _call_id, state, _cost in inventory] == [
            "call_committed",
            "call_committed",
            "call_prepared",
        ]
        assert (
            SQLiteCellJournal(
                repository, authority_root=authority_root
            ).load_rooted_attempt_authority(
                cell_id="demo-cell", attempt_id=prepared.prepared.attempt.attempt_id
            )
            is not None
        )


def test_product_authority_prime_unfit_pays_for_prime_then_delivers_fresh_miss(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).parents[1]
    target = tmp_path / "target"
    target.mkdir()
    task_path = tmp_path / "task-envelope.json"
    prompt = render(
        repository=repository_root,
        target=target,
        mode="warm",
        root_sentinel="root-private",
        parent_history_sentinel="parent-private",
        task_envelope_output=task_path,
    )
    assignment = coding_assignment_from_message(
        next(line for line in prompt.splitlines() if line.startswith("RRCV2_CODING_ASSIGNMENT_V1:"))
    )
    assert assignment is not None
    database = tmp_path / "rrcv2.sqlite3"
    authority_root = tmp_path / "cell-authority"
    prepare_root(
        argparse.Namespace(
            repository=repository_root,
            database=database,
            authority_root=authority_root,
            task_envelope=task_path,
            cell_id="demo-warm-cell",
            run_id="demo-warm-run",
            arm="rrc_warm",
            session_id="launch-demo-warm",
        )
    )
    task_raw = task_path.read_bytes()
    task_ref = AuthorityRef(
        task_path, __import__("hashlib").sha256(task_raw).hexdigest(), len(task_raw)
    )
    refs = product_authority_refs(repository_root)
    model = _PrimeUnfitPrepareModel()
    with SQLiteRRCRepository(database) as repository:
        attempts = AttemptRepository(repository)
        controller = ContextMeshController(
            attempts=attempts,
            model=model,
            retrieval=_NearReadingRetrieval(repository),
            config=Config("owner"),
            target_root=target,
            attempts_root=tmp_path / "attempts",
            route_id="rrcv2-coding-v1",
            round_id="demo-warm-run",
        )

        def model_factory(attempt, envelope):
            del envelope
            cells = SQLiteCellJournal(repository, authority_root=authority_root)
            root = cells.load_root_started("demo-warm-cell")
            bound = cells.bind_attempt(
                root,
                tool_use_id="tool-warm",
                attempt_id=attempt.attempt_id,
                task_envelope_sha256=task_ref.sha256,
                expected_generation=root.cell.generation,
            )
            return ContextMeshProductModel(
                model,
                repo=repository_root,
                refs=refs,
                task_envelope_ref=task_ref,
                cell_attempt_binding_ref=bound.binding_ref,
                cell_journal=cells,
                run_id="demo-warm-run",
                replicate_id="interactive",
                arm="rrc_warm",
                cell_id="demo-warm-cell",
            )

        prepared = controller.prepare(
            assignment,
            tool_use_id="tool-warm",
            model_factory=model_factory,
        )
        assert prepared.prepared.branch.value == "miss"
        assert model.stages == ["metadata_fill", "prime", "spec", "independent_tests"]
        inventory = repository.load_call_inventory(prepared.prepared.attempt.attempt_id)
        assert len(inventory) == 5
        assert len({call_id for call_id, _state, _cost in inventory}) == 5
        assert all(len(call_id) == 64 for call_id, _state, _cost in inventory)
        assert [state for _call_id, state, _cost in inventory] == [
            "call_committed",
            "call_committed",
            "call_committed",
            "call_committed",
            "call_prepared",
        ]
        assert [
            (json.loads(cost)["stage"], json.loads(cost)["stage_ordinal"])
            if cost is not None
            else ("implement", 4)
            for _call_id, _state, cost in inventory
        ] == [
            ("metadata_fill", 1),
            ("prime", 2),
            ("spec", 2),
            ("independent_tests", 3),
            ("implement", 4),
        ]
        assert (
            SQLiteCellJournal(
                repository, authority_root=authority_root
            ).load_rooted_attempt_authority(
                cell_id="demo-warm-cell",
                attempt_id=prepared.prepared.attempt.attempt_id,
            )
            is not None
        )
