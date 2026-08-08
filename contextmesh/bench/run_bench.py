#!/usr/bin/env python3
"""ContextMesh A/B bench harness.

Runs the same fan-out audit task on:
  arm A — stock opencode (submodule source, no plugin)
  arm B — identical config + the ContextMesh plugin (EverOS digest memory)
and optionally arm B again "warm" (same EverOS namespace, so digests hit from
the first read).

Every run is hermetic: fresh workspace copy, fresh OPENCODE_DB, explicit
OPENCODE_CONFIG, isolated OPENCODE_CONFIG_DIR, autocompact disabled, and a
per-run Tollgate session encoded in the provider baseURL path.

Usage:
  python3 run_bench.py --runid demo1                 # A, B-cold, B-warm
  python3 run_bench.py --runid demo1 --arms a        # just arm A
  python3 run_bench.py --runid demo1 --arms b --warm # B-cold then B-warm
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CM = HERE.parent
REPO = CM.parent
OPENCODE_ENTRY = REPO / "opencode" / "packages" / "opencode" / "src" / "index.ts"
PROXY = "http://127.0.0.1:8788"
EVEROS = "http://127.0.0.1:8000"
MODEL = "ollama/deepseek-v4-flash:cloud"

TASK = (CM / "demo-prompt.txt").read_text().strip()


def load_env_local() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (CM / ".env.local").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def check_stack() -> None:
    for name, url in [("tollgate", f"{PROXY}/healthz"), ("everos", f"{EVEROS}/health")]:
        try:
            with urllib.request.urlopen(url, timeout=5):
                pass
        except Exception as e:
            sys.exit(
                f"{name} is not reachable at {url} ({e}). Start it: contextmesh/scripts/start_stack.sh"
            )


def run_arm(runid: str, arm: str, mode: str, model: str, task: str, timeout: int) -> Path:
    label = f"{arm}-{mode}"
    rundir = CM / "runs" / runid / label
    if rundir.exists():
        shutil.rmtree(rundir)
    rundir.mkdir(parents=True)
    workspace = rundir / "target"
    shutil.copytree(HERE / "target-template", workspace)
    # Make the workspace its own git project so opencode does not walk up to the
    # enclosing ReasonRender_MF repo and treat it as the worktree.
    for gitcmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=bench@contextmesh",
            "-c",
            "user.name=bench",
            "commit",
            "-qm",
            "bench workspace",
        ],
    ):
        subprocess.run(gitcmd, cwd=workspace, check=True, capture_output=True)
    (rundir / "config-dir").mkdir()  # empty OPENCODE_CONFIG_DIR → no global/plugin leakage

    session = f"{runid}-{label}"
    env = dict(os.environ)
    env.update(load_env_local())
    env.update(
        {
            # opencode's run command roots the project at $PWD when set
            # (packages/opencode/src/cli/cmd/run.ts:333) — Popen(cwd=...) does
            # not update PWD, so pin it to the workspace explicitly.
            "PWD": str(workspace),
            "OPENCODE_DB": str(rundir / "opencode.db"),
            "OPENCODE_CONFIG": str(CM / "configs" / f"arm-{arm}.json"),
            "OPENCODE_CONFIG_DIR": str(rundir / "config-dir"),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_AUTOCOMPACT": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "CONTEXTMESH_PROXY_BASE": f"{PROXY}/ollama/{session}/v1",
        }
    )
    if arm == "b":
        env.update(
            {
                "CONTEXTMESH_PLUGIN_PATH": f"file://{CM}/plugin/contextmesh.ts",
                "CONTEXTMESH_LOG": str(rundir / "metrics.jsonl"),
                "CONTEXTMESH_EVEROS_URL": EVEROS,
                # cold + warm share the runid namespace: cold populates, warm hits
                "CONTEXTMESH_APP_ID": f"cm-{runid}",
                "CONTEXTMESH_SUMMARIZER_URL": f"{PROXY}/ollama/{session}-summarizer/v1/chat/completions",
                "CONTEXTMESH_SUMMARIZER_MODEL": env.get(
                    "CONTEXTMESH_MODEL", "deepseek-v4-flash:cloud"
                ),
                "CONTEXTMESH_SYNC_SUMMARIZE": "1",
                # 0.45 still guards against useless digests while capturing dense
                # utility modules (a 0.45 digest saves 55% on every repeat read).
                "CONTEXTMESH_MAX_DIGEST_RATIO": "0.45",
            }
        )

    cmd = [
        "bun",
        "run",
        "--conditions=browser",
        str(OPENCODE_ENTRY),
        "run",
        "--format",
        "json",
        "--auto",
        "--agent",
        "orchestrator",
        "--model",
        model,
        task,
    ]
    print(f"[{label}] starting (session={session})")
    started = time.time()
    with open(rundir / "events.ndjson", "wb") as out, open(rundir / "run.log", "wb") as errlog:
        proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdout=out, stderr=errlog)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = -9
    wall = round(time.time() - started, 1)
    print(f"[{label}] finished in {wall}s (exit {code})")

    summary = collect(rundir, session, label, code, wall)
    (rundir / "run_summary.json").write_text(json.dumps(summary, indent=2))
    return rundir


def collect(rundir: Path, session: str, label: str, exit_code: int, wall: float) -> dict:
    # --- opencode sqlite (the per-run DB contains only this run's sessions) ---
    db = sqlite3.connect(rundir / "opencode.db")
    db.row_factory = sqlite3.Row
    sessions = [
        dict(r)
        for r in db.execute(
            "SELECT id, parent_id, title, agent, cost, tokens_input, tokens_output, "
            "tokens_reasoning, tokens_cache_read, tokens_cache_write FROM session"
        )
    ]
    totals = dict(
        db.execute(
            "SELECT COALESCE(SUM(tokens_input),0) AS input, COALESCE(SUM(tokens_output),0) AS output, "
            "COALESCE(SUM(tokens_reasoning),0) AS reasoning, COALESCE(SUM(tokens_cache_read),0) AS cache_read, "
            "COALESCE(SUM(tokens_cache_write),0) AS cache_write, COALESCE(SUM(cost),0) AS cost FROM session"
        ).fetchone()
    )
    reads = [
        dict(r)
        for r in db.execute(
            "SELECT session_id, json_extract(data,'$.state.input.filePath') AS path, "
            "json_extract(data,'$.state.status') AS status, "
            "json_extract(data,'$.state.input.offset') AS offset, "
            "json_extract(data,'$.state.input.limit') AS lim, "
            "json_extract(data,'$.state.metadata.contextmesh.digest') AS was_digest "
            "FROM part WHERE json_extract(data,'$.type')='tool' AND json_extract(data,'$.tool')='read'"
        )
    ]
    tasks = [
        dict(r)
        for r in db.execute(
            "SELECT session_id, json_extract(data,'$.state.status') AS status, "
            "json_extract(data,'$.state.input.subagent_type') AS subagent_type "
            "FROM part WHERE json_extract(data,'$.type')='tool' AND json_extract(data,'$.tool')='task'"
        )
    ]
    db.close()

    completed_reads = [r for r in reads if r["status"] == "completed"]
    full_reads = [r for r in completed_reads if r["offset"] is None and r["lim"] is None]
    by_path: dict[str, int] = {}
    for r in full_reads:
        if r["path"]:
            by_path[r["path"]] = by_path.get(r["path"], 0) + 1
    dup_reads = sum(c - 1 for c in by_path.values() if c > 1)
    subagents = [s for s in sessions if s["parent_id"]]

    # --- Tollgate slice (independent meter) ---
    proxy_rows = []
    tokens_path = CM / "runs" / "tokens.jsonl"
    if tokens_path.exists():
        for line in tokens_path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("session") in (session, f"{session}-summarizer"):
                proxy_rows.append(rec)
    proxy = {
        "requests": len(proxy_rows),
        "exact": sum(1 for r in proxy_rows if r.get("measurement_state") == "exact"),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in proxy_rows),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in proxy_rows),
        "summarizer_requests": sum(
            1 for r in proxy_rows if r.get("session") == f"{session}-summarizer"
        ),
        "summarizer_input_tokens": sum(
            r.get("input_tokens") or 0
            for r in proxy_rows
            if r.get("session") == f"{session}-summarizer"
        ),
        "summarizer_output_tokens": sum(
            r.get("output_tokens") or 0
            for r in proxy_rows
            if r.get("session") == f"{session}-summarizer"
        ),
    }

    # --- plugin metrics (arm B) ---
    metrics: dict[str, int] = {}
    mfile = rundir / "metrics.jsonl"
    if mfile.exists():
        for line in mfile.read_text().splitlines():
            try:
                ev = json.loads(line).get("event")
            except json.JSONDecodeError:
                continue
            if ev:
                metrics[ev] = metrics.get(ev, 0) + 1

    return {
        "label": label,
        "session": session,
        "exit_code": exit_code,
        "wall_seconds": wall,
        "opencode": {
            "totals": totals,
            "sessions": sessions,
            "subagent_count": len(subagents),
            "read_calls_full": len(full_reads),
            "read_calls_ranged": len(completed_reads) - len(full_reads),
            "reads_by_path": by_path,
            "duplicate_full_reads": dup_reads,
            "task_calls": tasks,
        },
        "proxy": proxy,
        "plugin_events": metrics,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runid", required=True)
    ap.add_argument("--arms", default="a,b", help="comma list from {a,b}")
    ap.add_argument(
        "--warm", action="store_true", default=True, help="also run arm B warm (default)"
    )
    ap.add_argument("--no-warm", dest="warm", action="store_false")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    if not OPENCODE_ENTRY.exists():
        sys.exit(f"opencode entry not found: {OPENCODE_ENTRY}")
    check_stack()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for arm in arms:
        run_arm(args.runid, arm, "cold", args.model, TASK, args.timeout)
    if args.warm and "b" in arms:
        run_arm(args.runid, "b", "warm", args.model, TASK, args.timeout)
    print(f"done. Analyze with: python3 {HERE / 'analyze.py'} --runid {args.runid}")


if __name__ == "__main__":
    main()
