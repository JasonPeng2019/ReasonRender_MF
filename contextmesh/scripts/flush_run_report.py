#!/usr/bin/env python3
"""Flush a bench run report into EverOS's agent track (cases/skills).

This is the sponsor-native memory path: unlike the digest KV (parked buffers,
never extracted), this deliberately calls /memory/add + /memory/flush so
EverOS's real extraction pipeline (LLM routed through Tollgate under session
'everos-extraction') turns the run into an agent Case owned by
agent_id 'contextmesh-storefront'. Artifacts land as markdown under
contextmesh/everos-root/ — human-readable, git-diffable memory.

Usage: python3 flush_run_report.py --runid v2 [--label b-warm]
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

CM = Path(__file__).resolve().parent.parent
EVEROS = "http://127.0.0.1:8000"
AGENT_SENDER = "contextmesh-storefront"  # PathSafeId: no colons allowed


def call(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{EVEROS}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        return json.load(res)


def report_text(runid: str, label: str) -> str:
    s = json.loads((CM / "runs" / runid / label / "run_summary.json").read_text())
    oc = s["opencode"]
    ev = s.get("plugin_events", {})
    shared = {p.split("/")[-1]: c for p, c in oc["reads_by_path"].items() if c > 1}
    return (
        f"ContextMesh bench run {runid}/{label} on the storefront-api repository. "
        f"Task: parallel audit of 4 HTTP handlers via {oc['subagent_count']} worker subagents. "
        f"Shared files read repeatedly: {json.dumps(shared)}. "
        f"Optimization behavior: {ev.get('digest_hit', 0)} digest hits, {ev.get('digest_stored', 0)} digests stored, "
        f"{ev.get('escape_hatch', 0)} escape-hatch ranged reads, {ev.get('task_compressed', 0)} task results compressed. "
        f"Token usage (proxy meter): {s['proxy']['input_tokens']} input / {s['proxy']['output_tokens']} output across "
        f"{s['proxy']['requests']} requests. Exit code {s['exit_code']} in {s['wall_seconds']}s. "
        f"Key insight: the shared core modules (models.js, utils.js, middleware.js) dominate redundant reads; "
        f"digesting them preserves audit quality while cutting repeat-read cost."
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runid", required=True)
    ap.add_argument("--label", default=None, help="default: every label present in the run")
    args = ap.parse_args()

    labels = [args.label] if args.label else [p.name for p in sorted((CM / "runs" / args.runid).glob("*")) if (p / "run_summary.json").exists()]
    for label in labels:
        session = f"runreport-{args.runid}-{label}"
        text = report_text(args.runid, label)
        add = call(
            "/api/v2/memory/add",
            {
                "session_id": session,
                "app_id": "contextmesh-agent",
                "project_id": "runs",
                "messages": [
                    {"sender_id": "bench", "role": "user", "timestamp": int(time.time() * 1000) - 1,
                     "content": f"Run the {args.runid}/{label} audit bench and report what happened."},
                    {"sender_id": AGENT_SENDER, "role": "assistant", "timestamp": int(time.time() * 1000),
                     "content": text},
                ],
            },
        )
        flush = call("/api/v2/memory/flush", {"session_id": session, "app_id": "contextmesh-agent", "project_id": "runs"})
        print(f"{label}: add={add['data']['status']} flush={flush['data']['status']}")
    print("check contextmesh/everos-root/ markdown for extracted cases (async cascade may lag a few seconds)")


if __name__ == "__main__":
    main()
