"""Direct two-task COLD/MISS -> WARM/HIT proof runner."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from rrc.contract import Complete, Outcome, Spec, Template
from rrc.everos import EverOSClient
from rrc.memory import EverOSRetrieval
from rrc.model import CodexModel
from rrc.pipeline.solve import render, solve
from rrc.store import SQLiteTemplateStore
from rrc.workload import two_task_workload


def _spec_fields(specification: Spec) -> dict[str, str]:
    return {
        "signature": specification.signature,
        "template": specification.template,
        "tests": specification.tests,
    }


def _template_fields(template: Template | None) -> dict[str, Any] | None:
    if template is None:
        return None
    return {
        "external_ref": template.external_ref,
        "spec": _spec_fields(template.spec),
        "slot_names": list(template.slot_names),
    }


def _outcome_fields(outcome: Outcome) -> dict[str, Any]:
    return {
        "task_id": outcome.task_id,
        "warm": outcome.warm,
        "passed": outcome.passed,
        "oracle_passed": outcome.oracle_passed,
        "reused": outcome.reused,
        "spec_tokens": outcome.spec_tokens,
        "impl_tokens": outcome.impl_tokens,
        "repair_tokens": outcome.repair_tokens,
        "total": outcome.total,
    }


def run_proof(
    client: EverOSClient,
    retrieval: EverOSRetrieval,
    complete: Complete,
    evidence_path: str | Path,
    *,
    strong: str = "gpt-5",
    cheap: str = "gpt-5",
) -> dict[str, Any]:
    """Run one warm miss attempt followed by one warm reuse attempt."""

    first_task, second_task = two_task_workload()
    first = solve(
        first_task,
        warm=True,
        complete=complete,
        memory=retrieval,
        strong=strong,
        cheap=cheap,
    )

    stored_ref = retrieval.last_stored_ref
    if stored_ref is not None:
        client.wait_for_index()

    second = solve(
        second_task,
        warm=True,
        complete=complete,
        memory=retrieval,
        strong=strong,
        cheap=cheap,
    )
    selected = retrieval.selected_template
    rendered_second = render(selected.spec, second_task.params) if selected is not None else None

    evidence = {
        "stored_external_ref": stored_ref,
        "retrieved_external_ref": retrieval.last_retrieved_ref,
        "selected_template": _template_fields(selected),
        "rendered_second_spec": (
            _spec_fields(rendered_second) if rendered_second is not None else None
        ),
        "outcomes": [_outcome_fields(first), _outcome_fields(second)],
        "result": "HIT" if second.reused else "MISS",
        "pass": (
            first.passed
            and first.oracle_passed is True
            and not first.reused
            and second.passed
            and second.oracle_passed is True
            and second.reused
            and second.spec_tokens == 0
            and retrieval.last_retrieved_ref == stored_ref
            and selected is not None
            and rendered_second is not None
        ),
    }
    path = Path(evidence_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return evidence


def main() -> None:
    model = os.environ.get("RRC_MODEL") or "gpt-5"
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
        complete=CodexModel(),
        evidence_path=run_dir / "evidence.json",
        strong=model,
        cheap=model,
    )
    print(run_dir)


if __name__ == "__main__":
    main()
