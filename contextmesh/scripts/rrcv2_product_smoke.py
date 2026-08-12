#!/usr/bin/env python3
"""Run the reviewed three-cell RRCv2 + ContextMesh credibility sequence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from rrc.cell_journal import SQLiteCellJournal
from rrc.contextmesh import CodingAssignmentV1
from rrc.contract import (
    ArtifactRefV1,
    Config,
    ReferencedTaskInputV1,
    StructuralShapeV1,
    TargetPreimageV1,
    Task,
    canonical_json_bytes,
    parse_task_envelope,
    seal_task_input,
    task_envelope_bytes,
)
from rrc.journal import SQLiteRRCRepository, parse_accepted_commit
from rrc.pipeline.template import TemplateError, render
from rrc.retrieval import SQLiteHybridRetrieval
from rrcv2_product_guard import GuardError, load_fixture


class SmokeError(RuntimeError):
    """The product smoke did not produce the frozen terminal evidence."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write(path: Path, raw: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short product authority write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tests(raw: bytes) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError("test artifact is not JSON") from exc
    tests = value.get("tests") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"tests", "v"}
        or value.get("v") != 1
        or not isinstance(tests, list)
        or not tests
        or any(not isinstance(item, str) for item in tests)
        or canonical_json_bytes(value) != raw
    ):
        raise SmokeError("test artifact is not canonical")
    return tuple(cast(list[str], tests))


def _task(row: Mapping[str, object]) -> Task:
    shape = row.get("shape")
    slots = row.get("slot_values")
    if not isinstance(shape, dict) or not isinstance(slots, dict):
        raise SmokeError("fixture task metadata is invalid")
    return Task(
        task_id=cast(str, row["task_id"]),
        text=cast(str, row["task_text"]),
        family=cast(str, row["family"]),
        artifact_path=cast(str, row["artifact_path"]),
        searchable_public=False,
        verification_profile="rrcv2_general_v1",
        primary=cast(str, row["primary"]),
        shape=StructuralShapeV1(
            tuple(cast(list[str], shape["arg_types"])),
            cast(int, shape["arity"]),
            tuple(cast(list[str], shape["fields"])),
        ),
        slot_values=tuple(sorted(cast(dict[str, str], slots).items())),
    )


def _cell_environment(
    base: Mapping[str, str], *, cell: Path, task: Task, prompt: bytes
) -> dict[str, str]:
    env = dict(base)
    round_root = cell.parent
    suffix = task.task_id.rsplit("-", 2)[-2]
    env.update(
        {
            "RRD_TARGET_ROOT": str(cell / "target"),
            "RRD_SEED_MANIFEST": str(cell / "seed-manifest.json"),
            "RRD_HOOK_EVENTS": str(cell / "hook-events.jsonl"),
            "RRD_RAW_RESULTS": str(cell / "raw-results"),
            "RRC_DEMO_LOCK": str(cell / "plan-spec.lock"),
            "RRC_DEMO_EVENTS": str(cell / "rrc-events.jsonl"),
            "RRC_DEMO_MODEL_EVENTS": str(cell / "rrc-model-events.jsonl"),
            "RRCV2_CELL_ID": f"rrcv2-{env['RRC_DEMO_ROUND']}-{suffix}",
            "RRCV2_PRODUCT_TASK_ENVELOPE": str(cell / "task-envelope.v1.json"),
            "RRCV2_ROOT_PROMPT_SHA256": _sha(prompt),
            "RRCV2_ROOT_SENTINEL": "rrcv2-root-" + secrets.token_hex(16),
            "RRCV2_PARENT_HISTORY_SENTINEL": "rrcv2-parent-" + secrets.token_hex(16),
            "RRCV2_CELL_AUTHORITY_ROOT": str(round_root / "rrcv2-cell-authority"),
        }
    )
    return env


def _materialize_case(
    *, fixture_root: Path, row: Mapping[str, object], cell: Path, repo: Path
) -> tuple[Task, bytes, str]:
    files = cast(dict[str, dict[str, object]], row["files"])
    starter = (fixture_root / cast(str, files["starter.py"]["path"])).read_bytes()
    public = (fixture_root / cast(str, files["public_tests.json"]["path"])).read_bytes()
    oracle = (fixture_root / cast(str, files["oracle_tests.json"]["path"])).read_bytes()
    _tests(public)
    _tests(oracle)
    task = _task(row)
    target = cell / "target"
    target.mkdir(parents=True, mode=0o700)
    source_path = target / task.artifact_path
    _write(source_path, starter, 0o644)
    public_rel = f"rrcv2_demo/{row['kind']}.public.v1.json"
    oracle_rel = f"rrcv2_demo/{row['kind']}.oracle.v1.json"
    _write(target / public_rel, public, 0o600)
    _write(target / oracle_rel, oracle, 0o600)
    subprocess.run(["/usr/bin/git", "init", "-q", str(target)], check=True, env=dict(os.environ))
    subprocess.run(
        ["/usr/bin/git", "-C", str(target), "add", "-A"], check=True, env=dict(os.environ)
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(target),
            "-c",
            "user.email=demo@reasonrendercoding",
            "-c",
            "user.name=demo",
            "commit",
            "-qm",
            "rrcv2 credibility starter",
        ],
        check=True,
        env=dict(os.environ),
    )
    assignment = CodingAssignmentV1(
        mode="warm",
        task=task,
        source_path=task.artifact_path,
        public_test_path=public_rel,
        oracle_test_path=oracle_rel,
        owned_paths=(task.artifact_path,),
        target_preimage=TargetPreimageV1.regular(
            task.artifact_path, _sha(starter), len(starter), 0o644
        ),
    )
    incoming_root = Path(tempfile.mkdtemp(prefix="rrcv2-smoke-input-", dir=cell))
    try:
        incoming = incoming_root / "incoming"
        _write(incoming / task.artifact_path, starter, 0o644)
        _write(incoming / ".rrcv2/public-tests.v1.json", public, 0o600)
        _write(incoming / ".rrcv2/oracle-tests.v1.json", oracle, 0o600)
        envelope = seal_task_input(
            ReferencedTaskInputV1(
                task=task,
                sealed_root=incoming,
                source_ref=ArtifactRefV1(_sha(starter), len(starter), task.artifact_path),
                public_test_ref=ArtifactRefV1(
                    _sha(public), len(public), ".rrcv2/public-tests.v1.json"
                ),
                oracle_ref=ArtifactRefV1(_sha(oracle), len(oracle), ".rrcv2/oracle-tests.v1.json"),
                target_preimage=assignment.target_preimage,
            ),
            input_root=incoming_root / "sealed",
        )
        envelope_raw = task_envelope_bytes(envelope)
        parse_task_envelope(envelope_raw)
        _write(cell / "task-envelope.v1.json", envelope_raw, 0o600)
    finally:
        shutil.rmtree(incoming_root, ignore_errors=True)
    reader = (
        f"{base_quote(os.environ['RRC_DEMO_UV_BIN'])} run --locked --project "
        f"{base_quote(str(repo / 'pyproject.toml'))} python "
        f"{base_quote(str(repo / 'contextmesh/scripts/rrd_result_reader.py'))} apply "
        "--attempt-id ID --receipt RECEIPT"
    )
    prompt = "\n".join(
        (
            "Run the canonical ReasonRenderCoding + ContextMesh coding demo.",
            "Do not inspect or read source, tests, oracle data, or result files directly.",
            "Spawn exactly one worker with agent_type=worker and fork_context=false.",
            "Give it exactly the complete marker below and do not alter it.",
            "Wait for the worker; if rrc_pending remains, wait again only for that agent.",
            "For the rrc_accepted row run the exact result-reader command after substituting ID and RECEIPT:",
            reader,
            "Report only the task ID and hash-only apply response.",
            "",
            assignment.marker(),
            "",
        )
    )
    return task, prompt.encode(), assignment.marker()


def base_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _prepare_root(*, repo: Path, env: Mapping[str, str], cell: Path, task: Task) -> None:
    command = [
        env["RRC_DEMO_UV_BIN"],
        "run",
        "--locked",
        "--project",
        str(repo / "pyproject.toml"),
        "python",
        str(repo / "contextmesh/scripts/rrcv2_product_cell.py"),
        "--repository",
        str(repo),
        "--database",
        env["RRC_DEMO_DATABASE"],
        "--authority-root",
        env["RRCV2_CELL_AUTHORITY_ROOT"],
        "--task-envelope",
        env["RRCV2_PRODUCT_TASK_ENVELOPE"],
        "--cell-id",
        env["RRCV2_CELL_ID"],
        "--run-id",
        env["RRC_DEMO_ROUND"],
        "--arm",
        "rrc_warm",
        "--session-id",
        "root-launch-" + task.task_id,
    ]
    with (cell / "root-permit.json").open("wb") as output:
        subprocess.run(command, check=True, env=dict(env), cwd=repo, stdout=output)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_cell(
    *,
    repo: Path,
    base_env: Mapping[str, str],
    fixture_root: Path,
    row: Mapping[str, object],
    round_root: Path,
) -> dict[str, object]:
    cell = round_root / cast(str, row["kind"])
    cell.mkdir(mode=0o700)
    task, prompt, _marker = _materialize_case(
        fixture_root=fixture_root, row=row, cell=cell, repo=repo
    )
    _write(cell / "root-prompt.txt", prompt, 0o600)
    env = _cell_environment(base_env, cell=cell, task=task, prompt=prompt)
    _prepare_root(repo=repo, env=env, cell=cell, task=task)
    ready = cell / "finisher.ready"
    finisher_command = [
        env["RRC_DEMO_UV_BIN"],
        "run",
        "--locked",
        "--project",
        str(repo / "pyproject.toml"),
        "python",
        str(repo / "contextmesh/scripts/rrc_finisher.py"),
        "--database",
        env["RRC_DEMO_DATABASE"],
        "--owner-scope",
        env["RRCV2_OWNER_SCOPE"],
        "--codex-bin",
        env["RRD_CODEX_BIN"],
        "--strong-model",
        "gpt-5.5",
        "--small-model",
        "gpt-5.6-luna",
        "--memory-backend",
        "sqlite",
        "--ready-file",
        str(ready),
    ]
    with (cell / "finisher.log").open("wb") as finisher_log:
        finisher = subprocess.Popen(
            finisher_command,
            env=env,
            cwd=repo,
            stdout=finisher_log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and finisher.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not ready.exists():
            raise SmokeError("finisher did not become ready")
        command = [
            env["RRD_CODEX_BIN"],
            "--dangerously-bypass-approvals-and-sandbox",
            "--strict-config",
            "--dangerously-bypass-hook-trust",
            "-c",
            'service_tier="priority"',
            "exec",
            "--json",
            "--skip-git-repo-check",
            prompt.decode(),
        ]
        with (
            (cell / "root-events.jsonl").open("wb") as output,
            (cell / "root-stderr.log").open("wb") as error,
        ):
            completed = subprocess.run(
                command,
                env=env,
                cwd=cell / "target",
                stdout=output,
                stderr=error,
                timeout=720,
                check=False,
            )
        if completed.returncode != 0:
            raise SmokeError(f"root Codex failed for {task.task_id}: {completed.returncode}")
    finally:
        _stop(finisher)
    with SQLiteRRCRepository(Path(env["RRC_DEMO_DATABASE"])) as repository:
        cells = SQLiteCellJournal(repository, authority_root=Path(env["RRCV2_CELL_AUTHORITY_ROOT"]))
        combined = cells.load_combined_session(env["RRCV2_CELL_ID"])
        if combined is None or len(combined.attempts) != 1:
            raise SmokeError("cell has no exact combined-session authority")
        attempt = combined.attempts[0]
        if attempt.terminal_kind != "accepted" or attempt.receipt is None:
            raise SmokeError("cell attempt was not accepted")
        terminal = repository.load_terminal_intent(attempt.attempt_id)
        if terminal is None or terminal[0] != "accepted":
            raise SmokeError("accepted terminal authority is missing")
        accepted = parse_accepted_commit(terminal[1]).outcome
        deterministic_stages = tuple(
            row[0]
            for row in repository._connection.execute(  # noqa: SLF001
                "SELECT stage FROM rrcv2_deterministic_steps WHERE attempt_id=? ORDER BY stage",
                (attempt.attempt_id,),
            )
        )
        return {
            "all_cost_event_ids": list(combined.all_cost_event_ids),
            "attempt_id": attempt.attempt_id,
            "branch": accepted.branch,
            "cell_id": env["RRCV2_CELL_ID"],
            "cost_event_ids": list(accepted.cost_event_ids),
            "deterministic_stages": list(deterministic_stages),
            "root_cost_event_id": combined.root_cost_event_id,
            "task_id": task.task_id,
            "terminal_kind": attempt.terminal_kind,
        }


def run(*, fixture: Path, round_root: Path) -> dict[str, object]:
    repo = Path(os.environ["RRD_REPO_ROOT"]).resolve(strict=True)
    fixture_value = load_fixture(fixture.resolve(strict=True))
    cases = cast(list[dict[str, object]], fixture_value["cases"])
    results: list[dict[str, object]] = []
    for index, row in enumerate(cases):
        if index == 2:
            near_task = _task(row)
            with SQLiteRRCRepository(Path(os.environ["RRC_DEMO_DATABASE"])) as repository:
                retrieval = SQLiteHybridRetrieval(repository)
                candidates = retrieval.retrieve(near_task, Config(os.environ["RRCV2_OWNER_SCOPE"]))
                if (
                    len(candidates) != 1
                    or retrieval.classify(near_task, candidates[0].external_ref) != "exact"
                ):
                    raise SmokeError("near task did not retrieve exactly one structural exact row")
                bundle = retrieval.get_template(candidates[0].external_ref)
                if bundle is None or bundle.slot_contexts != (
                    ("entity", ("identifier", "string_content", "text")),
                ):
                    raise SmokeError("cached entity context inventory differs")
                try:
                    render(bundle, dict(near_task.slot_values or ()))
                except TemplateError as exc:
                    if str(exc) != "identifier-context slot value is invalid":
                        raise SmokeError("cached entity rejection differs") from exc
                else:
                    raise SmokeError("cached entity unexpectedly rendered")
        results.append(
            _run_cell(
                repo=repo,
                base_env=os.environ,
                fixture_root=fixture.parent,
                row=row,
                round_root=round_root,
            )
        )
    if [row["branch"] for row in results] != ["miss", "reuse", "miss"]:
        raise SmokeError("credibility branch sequence differs")
    if [len(cast(list[str], row["all_cost_event_ids"])) for row in results] != [4, 2, 4]:
        raise SmokeError("credibility logical call matrix differs")
    if not {"cache_render_rejection", "tier_minus_one"} <= set(
        cast(list[str], results[2]["deterministic_stages"])
    ):
        raise SmokeError("structural render rejection evidence is missing")
    summary = {"cells": results, "kind": "rrcv2_cli_smoke_summary", "v": 1}
    _write(round_root / "summary.json", canonical_json_bytes(summary), 0o600)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--round-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    summary = run(fixture=args.fixture, round_root=args.round_root.resolve(strict=True))
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GuardError, SmokeError, OSError, RuntimeError, ValueError) as exc:
        print(f"rrcv2 product smoke: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
