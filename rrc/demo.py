"""COLD-vs-WARM runner and meter for the native Codex ReasonRenderCoding demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from rrc.contract import (
    ArmMode,
    Config,
    InlineTaskInputV1,
    ModelPort,
    RetrievalPort,
    SolveOutcome,
    TargetPreimageV1,
    Task,
    seal_task_input,
)
from rrc.journal import SQLiteRRCRepository
from rrc.model import CodexModel
from rrc.pipeline.solve import solve
from rrc.retrieval import SQLiteHybridRetrieval
from rrc.workload import two_task_workload


def demo_workload(round_id: str) -> tuple[Task, Task]:
    """Return an isolated two-task family for one visible demo round."""

    round_id = round_id.strip()
    if not round_id:
        raise ValueError("round_id must be non-empty")
    tasks: list[Task] = []
    for task in two_task_workload():
        tasks.append(
            Task(
                task_id=f"{round_id}-{task.task_id}",
                # This stable, non-slot family tag is shared by the pair but
                # unique across rounds, preventing old EverOS candidates from
                # crowding the fresh proof out of the bounded top-k results.
                text=f"RRC_DEMO_ROUND: {round_id}\n{task.text}",
                oracle_tests=task.oracle_tests,
                family=task.family,
                artifact_path=task.artifact_path,
                public_tests=task.public_tests,
                searchable_public=task.searchable_public,
                verification_profile=task.verification_profile,
                primary=task.primary,
                shape=task.shape,
                slot_values=task.slot_values,
            )
        )
    return tasks[0], tasks[1]


def _event_fields(outcome: SolveOutcome) -> list[dict[str, object]]:
    return [
        {
            "stage": event.stage,
            "model": event.model,
            "provider": event.provider,
            "prompt_tokens": event.usage.prompt_tokens,
            "completion_tokens": event.usage.completion_tokens,
            "total_tokens": event.usage.total_tokens,
        }
        for event in outcome.cost_events
    ]


def _outcome_fields(outcome: SolveOutcome) -> dict[str, object]:
    return {
        "task_id": outcome.task_id,
        "arm": outcome.arm,
        "passed": outcome.passed,
        "pass_at_1": outcome.pass_at_1,
        "branch": outcome.branch.value,
        "repairs": outcome.repairs,
        "escalated": outcome.escalated,
        "template_ref": outcome.template.external_ref if outcome.template is not None else None,
        "cost_events": _event_fields(outcome),
    }


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_demo_arm(
    *,
    round_id: str,
    mode: ArmMode,
    model: ModelPort,
    retrieval: RetrievalPort | None,
    evidence_path: str | Path,
    cfg: Config | None = None,
    after_first: Callable[[], None] | None = None,
    model_events_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run both demo tasks through :func:`rrc.pipeline.solve` and save evidence."""

    if mode not in (ArmMode.COLD, ArmMode.WARM):
        raise ValueError("the RRC demo supports only COLD and WARM")
    config = (
        Config("demo-" + hashlib.sha256(round_id.encode()).hexdigest()[:32]) if cfg is None else cfg
    )
    del retrieval  # Retained only for compatibility with the historical demo API.
    evidence_file = Path(evidence_path)
    evidence_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    input_parent = evidence_file.parent / "inputs"
    input_parent.mkdir(mode=0o700)
    outcomes: list[SolveOutcome] = []
    with SQLiteRRCRepository(evidence_file.parent / "rrcv2-demo.sqlite3") as repository:
        local_retrieval = SQLiteHybridRetrieval(repository)
        for index, task in enumerate(demo_workload(round_id)):
            primary = task.primary
            if primary is None:
                raise ValueError("demo task has no frozen primary")
            envelope = seal_task_input(
                InlineTaskInputV1(
                    task,
                    f"def {primary}(value: int) -> int:\n    raise NotImplementedError",
                    TargetPreimageV1.none(),
                ),
                input_root=(input_parent / f"task-{index + 1}").absolute(),
            )
            outcome = solve(
                envelope,
                mode=mode,
                model=model,
                retrieval=local_retrieval,
                cfg=config,
                journal=repository,
                acceptance=repository,
                operation_key=f"demo-{mode.value}-{index + 1}",
            )
            outcomes.append(outcome)
            if index == 0 and mode is ArmMode.WARM and outcome.passed and after_first is not None:
                after_first()

    branches = [outcome.branch.value for outcome in outcomes]
    stages = [[event.stage for event in outcome.cost_events] for outcome in outcomes]
    all_passed = all(outcome.passed and outcome.pass_at_1 is True for outcome in outcomes)
    if mode is ArmMode.COLD:
        proof_pass = (
            all_passed
            and branches == ["miss", "miss"]
            and all("spec" in task_stages for task_stages in stages)
        )
    else:
        proof_pass = (
            all_passed
            and branches == ["miss", "reuse"]
            and "spec" in stages[0]
            and "spec" not in stages[1]
            and "fallback_spec" not in stages[1]
        )

    events = [event for outcome in outcomes for event in outcome.cost_events]
    failure_reasons: list[str] = []
    for outcome, task_stages in zip(outcomes, stages, strict=True):
        if not outcome.passed and task_stages == ["spec"] and not outcome.code:
            failure_reasons.append(
                f"{outcome.task_id}: SPEC output failed strict schema/task validation"
            )
        elif not outcome.passed and outcome.pass_at_1 is True:
            failure_reasons.append(
                f"{outcome.task_id}: model-authored SPEC tests rejected code that passed "
                "the hidden oracle"
            )
        elif not outcome.passed:
            failure_reasons.append(
                f"{outcome.task_id}: generated implementation failed verification"
            )
        elif outcome.pass_at_1 is not True:
            failure_reasons.append(f"{outcome.task_id}: generated code failed oracle tests")
    evidence: dict[str, Any] = {
        "demo": "ReasonRenderCoding",
        "pipeline": "rrc.pipeline.solve",
        "round_id": round_id,
        "arm": mode.value,
        "proof_pass": proof_pass,
        "branches": branches,
        "stages": stages,
        "model_calls": len(events),
        "prompt_tokens": sum(event.usage.prompt_tokens for event in events),
        "completion_tokens": sum(event.usage.completion_tokens for event in events),
        "total_tokens": sum(event.usage.total_tokens for event in events),
        "failure_reasons": failure_reasons,
        "model_events": str(model_events_path) if model_events_path is not None else None,
        "outcomes": [_outcome_fields(outcome) for outcome in outcomes],
    }
    _write_json(Path(evidence_path), evidence)
    return evidence


def _integer(evidence: Mapping[str, object], key: str) -> int:
    value = evidence.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _branch_label(evidence: Mapping[str, object]) -> str:
    value = evidence.get("branches")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return "pending"
    return " → ".join(str(item).upper() for item in value)


METER_WIDTH = 84


def _box(line: str = "") -> str:
    return "│ " + line.ljust(METER_WIDTH - 4) + " │"


def format_meter(
    cold: Mapping[str, object],
    warm: Mapping[str, object],
    *,
    round_id: str = "current",
) -> str:
    """Render the RRC comparison in the same box style as the TUI demo meter."""

    cold_tokens = _integer(cold, "total_tokens")
    warm_tokens = _integer(warm, "total_tokens")
    saved = cold_tokens - warm_tokens
    percent = (saved / cold_tokens * 100.0) if cold_tokens else 0.0
    cold_status = "PASS" if cold.get("proof_pass") is True else "FAIL"
    warm_status = "PASS" if warm.get("proof_pass") is True else "FAIL"
    calls_saved = _integer(cold, "model_calls") - _integer(warm, "model_calls")
    title = f" ReasonRenderCoding live token meter — round {round_id} "
    rows = (
        ("", "COLD (demo-a)", "WARM (demo-b)"),
        ("model calls", f"{_integer(cold, 'model_calls'):,}", f"{_integer(warm, 'model_calls'):,}"),
        ("total tokens", f"{cold_tokens:,}", f"{warm_tokens:,}"),
        ("branch", _branch_label(cold), _branch_label(warm)),
        ("proof", cold_status, warm_status),
    )
    lines = ["┌" + title.center(METER_WIDTH - 2, "─") + "┐"]
    lines.append(
        _box(
            f"▶ SPEC REUSE REMOVED {saved:,} TOKENS "
            f"({calls_saved:,} fewer model {'call' if calls_saved == 1 else 'calls'})"
        )
    )
    lines.append(
        _box("  (WARM task 2 instantiates the stored spec instead of synthesizing it again)")
    )
    lines.append("├" + "─" * (METER_WIDTH - 2) + "┤")
    for label, cold_value, warm_value in rows:
        lines.append(_box(f"{label:<16}{cold_value:>22}{warm_value:>26}"))
    lines.append(_box())
    lines.append(_box(f"Net this round: WARM saved {saved:,} tokens ({percent:.1f}%)"))
    lines.append(_box(f"WARM transition: {_branch_label(warm)}"))
    lines.append("└" + "─" * (METER_WIDTH - 2) + "┘")
    return "\n".join(lines)


def format_arm_report(evidence: Mapping[str, object]) -> str:
    """Format one tool result for direct display inside the native Codex TUI."""

    arm = str(evidence.get("arm", "unknown")).upper()
    proof = "PASS" if evidence.get("proof_pass") is True else "FAIL"
    branches = evidence.get("branches")
    stages = evidence.get("stages")
    branch_values = branches if isinstance(branches, list) else []
    stage_values = stages if isinstance(stages, list) else []
    lines = [
        f"ReasonRenderCoding {arm} — {proof}",
        "=" * (len(arm) + 28),
    ]
    for index in range(max(len(branch_values), len(stage_values))):
        branch = str(branch_values[index]).upper() if index < len(branch_values) else "UNKNOWN"
        task_stages = stage_values[index] if index < len(stage_values) else []
        stage_label = " → ".join(str(stage).upper() for stage in task_stages)
        lines.append(f"Task {index + 1}: {branch}  [{stage_label}]")
    lines.extend(
        (
            f"Model calls: {_integer(evidence, 'model_calls'):,}",
            f"Tokens: {_integer(evidence, 'total_tokens'):,}",
            "Pipeline: rrc.pipeline.solve",
        )
    )
    reasons = evidence.get("failure_reasons")
    if isinstance(reasons, list):
        lines.extend(f"ERROR: {reason}" for reason in reasons)
    model_events = evidence.get("model_events")
    if isinstance(model_events, str) and model_events:
        lines.append(f"Raw model events: {model_events}")
    return "\n".join(lines)


def format_prompt(round_id: str) -> str:
    """Show the exact two coding tasks sent through the pipeline."""

    first, second = demo_workload(round_id)
    return "\n\n".join(
        (
            "ReasonRenderCoding website demo",
            "Task 1 (expected WARM MISS):\n" + first.text,
            "Task 2 (same shape, new slots; expected WARM REUSE):\n" + second.text,
        )
    )


def _load_evidence(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _waiting_meter(round_id: str, missing: Sequence[str]) -> str:
    title = f" ReasonRenderCoding live token meter — round {round_id} "
    lines = ["┌" + title.center(METER_WIDTH - 2, "─") + "┐"]
    lines.append(_box("Waiting for " + " and ".join(missing)))
    lines.append(
        _box("Start side A and side B in their Codex terminals, then paste the shared prompt.")
    )
    lines.append("└" + "─" * (METER_WIDTH - 2) + "┘")
    return "\n".join(lines)


def _print_meter(round_dir: Path, *, round_id: str, watch: bool) -> int:
    cold_path = round_dir / "a" / "evidence.json"
    warm_path = round_dir / "b" / "evidence.json"
    while True:
        cold = _load_evidence(cold_path)
        warm = _load_evidence(warm_path)
        missing = []
        if cold is None:
            missing.append("A/COLD")
        if warm is None:
            missing.append("B/WARM")
        rendered = (
            format_meter(cold, warm, round_id=round_id)
            if cold is not None and warm is not None
            else _waiting_meter(round_id, missing)
        )
        prefix = "\033[2J\033[H" if watch else ""
        print(prefix + rendered, flush=True)
        if not watch:
            if cold is None or warm is None:
                return 2
            return 0 if cold.get("proof_pass") is True and warm.get("proof_pass") is True else 1
        try:
            time.sleep(2.0)
        except KeyboardInterrupt:
            return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prompt = commands.add_parser("prompt", help="print the exact two-task website prompt")
    prompt.add_argument("--round-id", required=True)

    run = commands.add_parser("run", help="run one real ReasonRenderCoding arm")
    run.add_argument("--arm", choices=("cold", "warm"), required=True)
    run.add_argument("--round-id", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--everos-url", default=os.environ.get("RRC_EVEROS_URL", "http://127.0.0.1:8000")
    )
    run.add_argument(
        "--strong-model", default=os.environ.get("RRC_STRONG_MODEL") or os.environ.get("RRC_MODEL")
    )
    run.add_argument("--small-model", default=os.environ.get("RRC_SMALL_MODEL"))

    meter = commands.add_parser("meter", help="compare saved RRC evidence")
    meter.add_argument("--round-dir", type=Path, required=True)
    meter.add_argument("--round-id")
    meter.add_argument("--watch", action="store_true")

    report = commands.add_parser("report", help="format one saved arm for the Codex tool")
    report.add_argument("--evidence", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prompt":
        print(format_prompt(args.round_id))
        return 0
    if args.command == "meter":
        round_id = args.round_id or args.round_dir.name
        return _print_meter(args.round_dir, round_id=round_id, watch=args.watch)
    if args.command == "report":
        evidence = _load_evidence(args.evidence)
        if evidence is None:
            raise SystemExit(f"could not read RRC evidence: {args.evidence}")
        print(format_arm_report(evidence))
        print(f"Evidence: {args.evidence}")
        return 0

    if not args.strong_model:
        raise SystemExit(
            "set RRC_MODEL or RRC_STRONG_MODEL to a Codex model available to this account"
        )
    mode = ArmMode(args.arm)
    model_events_path = args.output.parent / "model-events.jsonl"
    model = CodexModel(
        strong_model=args.strong_model,
        small_model=args.small_model or args.strong_model,
        artifact_log=model_events_path,
    )
    # This compatibility CLI now uses the same authoritative local SQLite
    # retrieval as the canonical pipeline.  Optional EverOS is exercised only
    # by the explicit ContextMesh/EverOS route, never as a demo prerequisite.
    after_first_callback: Callable[[], None] | None = None
    retrieval = None

    evidence = run_demo_arm(
        round_id=args.round_id,
        mode=mode,
        model=model,
        retrieval=retrieval,
        evidence_path=args.output,
        after_first=after_first_callback,
        model_events_path=model_events_path,
    )
    print(format_arm_report(evidence))
    print(f"Evidence: {args.output}")
    if evidence["proof_pass"] is not True:
        for reason in evidence["failure_reasons"]:
            print(f"ERROR: {reason}", file=sys.stderr)
    return 0 if evidence["proof_pass"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
