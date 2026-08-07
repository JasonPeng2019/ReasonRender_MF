#!/usr/bin/env python3
"""ContextMesh bench analyzer — A/B comparison, redundancy, meter cross-check.

Reads runs/<runid>/*/run_summary.json produced by run_bench.py and writes
runs/<runid>/report.md + report.json.

Token accounting notes (kept honest):
- "opencode" numbers come from the per-run sqlite session table (opencode's own
  accounting; input excludes cache read/write, output excludes reasoning — with
  this provider both cache fields are 0 and reasoning is folded into output).
- "proxy" numbers come from Tollgate's durable JSONL (provider-reported usage,
  independent meter). The proxy additionally sees session-title generation and
  Arm B's summarizer calls, so it is the SUPERSET meter; summarizer cost is
  reported separately and included in Arm B totals (nothing is hidden).
- Dollar figures are MODELED at reference per-1M rates for illustration; token
  counts are the ground truth.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CM = Path(__file__).resolve().parent.parent


def load(runid: str) -> dict[str, dict]:
    out = {}
    for f in sorted((CM / "runs" / runid).glob("*/run_summary.json")):
        s = json.loads(f.read_text())
        out[s["label"]] = s
    if not out:
        raise SystemExit(f"no run_summary.json under runs/{runid}/")
    return out


def arm_tokens(s: dict) -> dict:
    """Headline per-run token totals from the proxy (independent, superset meter)."""
    p = s["proxy"]
    oc = s["opencode"]["totals"]
    return {
        "proxy_input": p["input_tokens"],
        "proxy_output": p["output_tokens"],
        "proxy_total": p["input_tokens"] + p["output_tokens"],
        "proxy_requests": p["requests"],
        "summarizer_total": p["summarizer_input_tokens"] + p["summarizer_output_tokens"],
        "oc_input": oc["input"] + oc["cache_read"] + oc["cache_write"],
        "oc_output": oc["output"] + oc["reasoning"],
        "oc_total": oc["input"] + oc["cache_read"] + oc["cache_write"] + oc["output"] + oc["reasoning"],
    }


def pct(base: float, new: float) -> float:
    return round(100.0 * (base - new) / base, 1) if base else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runid", required=True)
    ap.add_argument("--rate-in", type=float, default=3.0, help="modeled $/1M input tokens")
    ap.add_argument("--rate-out", type=float, default=15.0, help="modeled $/1M output tokens")
    args = ap.parse_args()

    runs = load(args.runid)
    tok = {label: arm_tokens(s) for label, s in runs.items()}

    report: dict = {"runid": args.runid, "runs": {}, "comparisons": {}}
    lines: list[str] = [
        f"# ContextMesh bench report — run `{args.runid}`",
        "",
        "Headline meter: Tollgate proxy (provider-reported usage, independent of opencode).",
        f"Modeled cost rates: ${args.rate_in}/1M input, ${args.rate_out}/1M output (illustrative reference rates; token counts are the measurement).",
        "",
        "## Per-run totals",
        "",
        "| run | proxy input | proxy output | proxy total | requests | of which summarizer tokens | opencode total | subagents | full reads | dup full reads | digest hits | wall s | exit |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for label, s in runs.items():
        t = tok[label]
        oc = s["opencode"]
        hits = s["plugin_events"].get("digest_hit", 0)
        cost = (t["proxy_input"] * args.rate_in + t["proxy_output"] * args.rate_out) / 1e6
        report["runs"][label] = {
            **t,
            "modeled_cost_usd": round(cost, 4),
            "subagent_count": oc["subagent_count"],
            "read_calls_full": oc["read_calls_full"],
            "duplicate_full_reads": oc["duplicate_full_reads"],
            "plugin_events": s["plugin_events"],
            "exit_code": s["exit_code"],
            "wall_seconds": s["wall_seconds"],
        }
        lines.append(
            f"| {label} | {t['proxy_input']:,} | {t['proxy_output']:,} | {t['proxy_total']:,} | "
            f"{t['proxy_requests']} | {t['summarizer_total']:,} | {t['oc_total']:,} | {oc['subagent_count']} | "
            f"{oc['read_calls_full']} | {oc['duplicate_full_reads']} | {hits} | {s['wall_seconds']} | {s['exit_code']} |"
        )

    lines += ["", "## Comparisons (proxy totals, summarizer included — nothing netted out)", ""]
    base = tok.get("a-cold")
    for label in ("b-cold", "b-warm"):
        if base and label in tok:
            t = tok[label]
            cmp = {
                "input_reduction_pct": pct(base["proxy_input"], t["proxy_input"]),
                "total_reduction_pct": pct(base["proxy_total"], t["proxy_total"]),
                "input_tokens_saved": base["proxy_input"] - t["proxy_input"],
                "total_tokens_saved": base["proxy_total"] - t["proxy_total"],
            }
            report["comparisons"][f"{label}_vs_a-cold"] = cmp
            lines.append(
                f"- **{label} vs a-cold**: input {cmp['input_reduction_pct']}% ↓ "
                f"({cmp['input_tokens_saved']:,} tokens), total {cmp['total_reduction_pct']}% ↓ "
                f"({cmp['total_tokens_saved']:,} tokens)"
            )

    lines += ["", "## Meter cross-check (proxy vs opencode sqlite)", ""]
    for label in runs:
        t = tok[label]
        delta = t["proxy_total"] - t["oc_total"]
        s = runs[label]
        extra = t["summarizer_total"]
        lines.append(
            f"- {label}: proxy {t['proxy_total']:,} vs opencode {t['oc_total']:,} "
            f"(Δ {delta:,}; expected extras in proxy: summarizer {extra:,} tokens + session-title calls)"
        )
        report["runs"][label]["meter_delta"] = delta

    lines += ["", "## Mechanism-level savings (measured per digest-served read)", ""]
    for label, s in runs.items():
        mfile = CM / "runs" / args.runid / label / "metrics.jsonl"
        saved = hits = 0
        if mfile.exists():
            for line in mfile.read_text().splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("event") == "digest_hit":
                    hits += 1
                    saved += d.get("savedTokens", 0)
        report["runs"].setdefault(label, {})["digest_saved_tokens"] = saved
        lines.append(
            f"- {label}: {hits} digest-served reads replaced ~{saved:,} tokens of raw file payload "
            f"(direct per-read measurement, independent of run-to-run model variance)"
        )

    lines += [
        "",
        "## Redundancy (why the optimization exists)",
        "",
    ]
    for label, s in runs.items():
        paths = s["opencode"]["reads_by_path"]
        shared = {p: c for p, c in paths.items() if c > 1}
        lines.append(f"- {label}: full reads by path with count>1: {json.dumps(shared) if shared else 'none'}")

    outdir = CM / "runs" / args.runid
    (outdir / "report.json").write_text(json.dumps(report, indent=2))
    (outdir / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwritten: {outdir / 'report.md'}, {outdir / 'report.json'}")


if __name__ == "__main__":
    main()
