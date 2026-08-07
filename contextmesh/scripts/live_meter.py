#!/usr/bin/env python3
"""Live side-by-side token meter for the TUI demo.

Tails Tollgate's JSONL and shows running totals for the two demo sessions
(demo-a vs demo-b, including b's summarizer overhead) plus ContextMesh digest
activity from side B's metrics log. Refreshes in place every 2 seconds.

Usage: python3 live_meter.py [--once]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

CM = Path(__file__).resolve().parent.parent
TOKENS = CM / "runs" / "tokens.jsonl"
ROUNDFILE = CM / "runs" / "demo-tui" / "round"


def current_round() -> str:
    return ROUNDFILE.read_text().strip() if ROUNDFILE.exists() else "r0"


ROUND = current_round()
METRICS_B = CM / "runs" / "demo-tui" / "b" / f"metrics-{ROUND}.jsonl"


def totals() -> dict[str, dict[str, int]]:
    out = {
        "a": {"input": 0, "output": 0, "requests": 0},
        "b": {"input": 0, "output": 0, "requests": 0},
        "b-summarizer": {"input": 0, "output": 0, "requests": 0},
    }
    if not TOKENS.exists():
        return out
    # Only this round's sessions: demo-<round>-a, demo-<round>-b, demo-<round>-b-summarizer
    keymap = {
        f"demo-{ROUND}-a": "a",
        f"demo-{ROUND}-b": "b",
        f"demo-{ROUND}-b-summarizer": "b-summarizer",
    }
    for line in TOKENS.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = keymap.get(r.get("session") or "")
        if not key or r.get("measurement_state") != "exact":
            continue
        out[key]["input"] += r.get("input_tokens") or 0
        out[key]["output"] += r.get("output_tokens") or 0
        out[key]["requests"] += 1
    return out


def digest_stats() -> dict[str, int]:
    stats = {"digest_hit": 0, "digest_stored": 0, "escape_hatch": 0, "task_compressed": 0, "saved_tokens": 0}
    if METRICS_B.exists():
        for line in METRICS_B.read_text().splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = d.get("event")
            if ev in stats:
                stats[ev] += 1
            if ev == "digest_hit":
                stats["saved_tokens"] += d.get("savedTokens", 0)
    return stats


WIDTH = 84


def box(line: str = "") -> str:
    return "│ " + line.ljust(WIDTH - 4) + " │"


def subtract(cur: dict, base: dict) -> dict:
    if isinstance(cur, dict):
        return {k: subtract(cur[k], base.get(k, 0) if isinstance(base, dict) else 0) for k in cur}
    return cur - base


def render(t0: dict | None = None, d0: dict | None = None) -> str:
    t = totals()
    d = digest_stats()
    if t0:
        t = subtract(t, t0)
    if d0:
        d = subtract(d, d0)
    a_total = t["a"]["input"] + t["a"]["output"]
    b_all = {k: t["b"][k] + t["b-summarizer"][k] for k in ("input", "output", "requests")}
    b_total = b_all["input"] + b_all["output"]
    delta = a_total - b_total
    pct = f"{100 * delta / a_total:.1f}%" if a_total else "—"
    summ = t["b-summarizer"]["input"] + t["b-summarizer"]["output"]
    title = f" ContextMesh live token meter — round {ROUND} "
    rows = [
        ("", "STOCK (demo-a)", "CONTEXTMESH (demo-b)"),
        ("requests", f"{t['a']['requests']:,}", f"{b_all['requests']:,}"),
        ("input tokens", f"{t['a']['input']:,}", f"{b_all['input']:,}"),
        ("output tokens", f"{t['a']['output']:,}", f"{b_all['output']:,}"),
        ("TOTAL", f"{a_total:,}", f"{b_total:,}"),
    ]
    lines = ["┌" + title.center(WIDTH - 2, "─") + "┐"]
    # HEADLINE — deterministic: tokens the memory layer removed from subagent reads.
    lines.append(box(f"▶ MEMORY LAYER REMOVED {d['saved_tokens']:,} REDUNDANT SUBAGENT TOKENS "
                     f"({d['digest_hit']} cached reads)"))
    lines.append(box("  (deterministic — raw file payload replaced by digests, not re-paid by siblings)"))
    lines.append("├" + "─" * (WIDTH - 2) + "┤")
    for label, av, bv in rows:
        lines.append(box(f"{label:<16}{av:>22}{bv:>26}"))
    lines.append(box())
    # Net P&L — honest label: subject to reasoning-model turn variance per run.
    sign = "below" if delta >= 0 else "ABOVE"
    lines.append(box(f"Net this round: B {abs(delta):,} tok {sign} A ({pct})  "
                     f"[incl. summarizer {summ:,}; net swings with turn count]"))
    lines.append(
        box(
            f"Digests: {d['digest_hit']} hits · {d['digest_stored']} stored · "
            f"{d['escape_hatch']} escape-reads · {d['task_compressed']} compressed"
        )
    )
    lines.append("└" + "─" * (WIDTH - 2) + "┘")
    return "\n".join(lines)


def main() -> None:
    # Default: show deltas since the meter was launched, so each rehearsal
    # starts from zero. --absolute shows all-time totals for the sessions.
    absolute = "--absolute" in sys.argv
    if "--once" in sys.argv:
        print(render())  # one absolute snapshot
        return
    t0 = None if absolute else totals()
    d0 = None if absolute else digest_stats()
    try:
        while True:
            print("\033[2J\033[H" + render(t0, d0), flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
