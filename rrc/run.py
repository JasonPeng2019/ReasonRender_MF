"""Direct two-task WARM MISS-to-REUSE proof runner."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from rrc.contract import ArmMode, Config, ModelPort, SolveOutcome, Spec, Template
from rrc.everos import EverOSClient
from rrc.memory import EverOSRetrieval
from rrc.model import CodexModel
from rrc.pipeline.solve import solve
from rrc.pipeline.template import parse_task_metadata, render
from rrc.store import SQLiteTemplateStore
from rrc.workload import two_task_workload


def _spec_fields(specification: Spec) -> dict[str, object]:
    slots = specification.slots
    return {
        "plan": specification.plan,
        "signature": specification.signature,
        "contract": specification.contract,
        "tests": list(specification.tests),
        "slots": {
            "entity": slots.entity,
            "identifiers": list(slots.identifiers),
            "types": list(slots.types),
            "fields": list(slots.fields),
            "constants": list(slots.constants),
            "edge_values": list(slots.edge_values),
            "values": dict(slots.values),
        },
    }


def _template_fields(template: Template | None) -> dict[str, Any] | None:
    if template is None:
        return None
    return {
        "external_ref": template.external_ref,
        "spec_skeleton": _spec_fields(template.spec_skeleton),
        "slot_names": list(template.slot_names),
    }


def _outcome_fields(outcome: SolveOutcome) -> dict[str, Any]:
    return {
        "task_id": outcome.task_id,
        "arm": outcome.arm,
        "passed": outcome.passed,
        "pass_at_1": outcome.pass_at_1,
        "branch": outcome.branch.value,
        "repairs": outcome.repairs,
        "escalated": outcome.escalated,
        "cost_events": [
            {
                "stage": event.stage,
                "model": event.model,
                "provider": event.provider,
                "prompt_tokens": event.usage.prompt_tokens,
                "completion_tokens": event.usage.completion_tokens,
                "total_tokens": event.usage.total_tokens,
            }
            for event in outcome.cost_events
        ],
    }


def run_proof(
    client: EverOSClient,
    retrieval: EverOSRetrieval,
    model: ModelPort,
    evidence_path: str | Path,
    *,
    cfg: Config | None = None,
) -> dict[str, Any]:
    """Run one WARM miss followed by one structurally exact WARM reuse."""

    cfg = Config() if cfg is None else cfg
    first_task, second_task = two_task_workload()
    first = solve(
        first_task,
        mode=ArmMode.WARM,
        model=model,
        retrieval=retrieval,
        cfg=cfg,
    )

    first_ref = retrieval.last_stored_ref
    if first_ref is not None:
        client.wait_for_index()

    second = solve(
        second_task,
        mode=ArmMode.WARM,
        model=model,
        retrieval=retrieval,
        cfg=cfg,
    )
    selected = retrieval.selected_template
    _, second_values = parse_task_metadata(second_task)
    rendered_second = render(selected, second_values) if selected is not None else None
    second_stages = [event.stage for event in second.cost_events]

    evidence = {
        "stored_external_ref": first_ref,
        "retrieved_external_ref": retrieval.last_retrieved_ref,
        "selected_template": _template_fields(selected),
        "rendered_second_spec": (
            _spec_fields(rendered_second) if rendered_second is not None else None
        ),
        "outcomes": [_outcome_fields(first), _outcome_fields(second)],
        "result": second.branch.value.upper(),
        "pass": (
            first.passed
            and first.pass_at_1 is True
            and first.branch.value == "miss"
            and second.passed
            and second.pass_at_1 is True
            and second.branch.value == "reuse"
            and "spec" not in second_stages
            and "fallback_spec" not in second_stages
            and retrieval.last_retrieved_ref == first_ref
            and selected is not None
            and rendered_second is not None
        ),
    }
    path = Path(evidence_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return evidence


def main() -> None:
    strong = os.environ.get("RRC_STRONG_MODEL") or os.environ.get("RRC_MODEL") or "gpt-5"
    small = os.environ.get("RRC_SMALL_MODEL") or strong
    everos_url = os.environ.get("RRC_EVEROS_URL") or "http://127.0.0.1:8000"
    runtime_root = Path(os.environ.get("RRC_RUNTIME_DIR") or "runtime/rrc")
    try:
        runtime_root.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="rrc-proof-", dir=runtime_root))
    except OSError as exc:
        raise SystemExit(f"could not create RRC run directory: {exc}") from exc

    store = SQLiteTemplateStore(run_dir / "templates.sqlite")
    client = EverOSClient(everos_url)
    retrieval = EverOSRetrieval(store, client)
    run_proof(
        client=client,
        retrieval=retrieval,
        model=CodexModel(strong_model=strong, small_model=small),
        evidence_path=run_dir / "evidence.json",
    )
    print(run_dir)


if __name__ == "__main__":
    main()
