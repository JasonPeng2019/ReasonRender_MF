#!/usr/bin/env python3
"""Per-subagent token breakdown for a completed demo round.

Separates the SUBAGENT (worker) sessions from the orchestrator on each side and
reports token consumption, so you can compare what the parallel subagents cost
under stock opencode (A) vs ContextMesh (B). Numbers are opencode's own
per-session accounting (input excludes cache; output includes reasoning — both
0 cache here). Titles identify each worker's assigned handler.

Usage:
  python3 subagent_tokens.py                 # runs/demo-tui/{a,b}
  python3 subagent_tokens.py <dirA> <dirB>
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

CM = Path(__file__).resolve().parent.parent


def sessions(run_dir: Path) -> list[dict]:
    db = sqlite3.connect(run_dir / "opencode.db")
    db.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in db.execute(
            "SELECT id, parent_id, title, tokens_input, tokens_output, tokens_reasoning FROM session ORDER BY time_created"
        )
    ]
    db.close()
    return rows


def summarize(run_dir: Path) -> dict:
    ss = sessions(run_dir)
    orch = [s for s in ss if s["parent_id"] is None]
    workers = [s for s in ss if s["parent_id"] is not None]
    def tot(s):
        return s["tokens_input"] + s["tokens_output"] + s["tokens_reasoning"]
    return {
        "orchestrator": {"input": sum(s["tokens_input"] for s in orch), "output": sum(s["tokens_output"] + s["tokens_reasoning"] for s in orch)},
        "workers": [
            {"title": w["title"], "input": w["tokens_input"], "output": w["tokens_output"] + w["tokens_reasoning"], "total": tot(w)}
            for w in workers
        ],
        "worker_total": sum(tot(w) for w in workers),
        "worker_input": sum(w["tokens_input"] for w in workers),
        "worker_output": sum(w["tokens_output"] + w["tokens_reasoning"] for w in workers),
    }


def clean(title: str) -> str:
    return (title or "").replace(" (@worker subagent)", "").strip()[:38]


def main() -> None:
    if len(sys.argv) == 3:
        da, db_ = Path(sys.argv[1]), Path(sys.argv[2])
    else:
        da, db_ = CM / "runs" / "demo-tui" / "a", CM / "runs" / "demo-tui" / "b"
    A, B = summarize(da), summarize(db_)

    print("SUBAGENT (worker) TOKEN CONSUMPTION — side A (stock) vs side B (ContextMesh)\n")
    print(f"{'worker (assigned handler)':40} | {'A total':>10} | {'B total':>10} | {'B vs A':>8}")
    print("-" * 78)
    # pair workers by assigned handler where possible
    a_by = {clean(w["title"]): w for w in A["workers"]}
    b_by = {clean(w["title"]): w for w in B["workers"]}
    for key in sorted(set(a_by) | set(b_by)):
        aw, bw = a_by.get(key), b_by.get(key)
        at = aw["total"] if aw else 0
        bt = bw["total"] if bw else 0
        pct = f"{100*(at-bt)/at:.0f}%" if at else "—"
        print(f"{key:40} | {at:>10,} | {bt:>10,} | {pct:>8}")
    print("-" * 78)
    wt_a, wt_b = A["worker_total"], B["worker_total"]
    pct = f"{100*(wt_a-wt_b)/wt_a:.1f}%" if wt_a else "—"
    print(f"{'ALL SUBAGENTS (workers only)':40} | {wt_a:>10,} | {wt_b:>10,} | {pct:>8}")
    print(f"{'  of which input tokens':40} | {A['worker_input']:>10,} | {B['worker_input']:>10,} |")
    print(f"{'  of which output tokens':40} | {A['worker_output']:>10,} | {B['worker_output']:>10,} |")
    print(f"\n{'orchestrator (coordinator)':40} | "
          f"{A['orchestrator']['input']+A['orchestrator']['output']:>10,} | "
          f"{B['orchestrator']['input']+B['orchestrator']['output']:>10,} |")
    print(f"\nSubagents alone: side B used {wt_b:,} tokens vs side A's {wt_a:,} — "
          f"{pct} {'less' if wt_b<=wt_a else 'MORE'} for the same parallel audit work.")


if __name__ == "__main__":
    main()
