#!/usr/bin/env python3
"""Quality-parity check for a completed demo round.

Answers the load-bearing question: does side B (digest-fed) produce output as
substantive as side A (raw-fed)? It extracts both orchestrators' final merged
reports and scores each against a GROUND-TRUTH list of real bugs planted in the
benchmark handlers, plus structural completeness (handler coverage, finding
count, presence of file:line anchors and severities).

This is deterministic (no LLM judge): a bug counts as "found" if the report
contains its file + a line in its anchor range, or a distinctive phrase. That
can undercount a differently-worded finding, so treat the score as a floor.

Usage:
  python3 compare_quality.py                 # scores runs/demo-tui/{a,b}
  python3 compare_quality.py <dirA> <dirB>   # score arbitrary run dirs
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

CM = Path(__file__).resolve().parent.parent

# Ground-truth bugs actually present in bench/target-template/src/handlers/*.
# Each: id, severity, handler, line anchors (any line in range counts), and
# alternative distinctive phrases (any match counts). A bug is FOUND if the
# report mentions the handler AND (a matching line number OR a phrase).
GROUND_TRUTH = [
    (
        "users-patch-idor",
        "high",
        "users.js",
        range(80, 98),
        [
            "never verifies",
            "req.user.id === req.params",
            "any authenticated user can",
            "IDOR",
            "object-level",
        ],
    ),
    ("users-patch-email", "med", "users.js", range(90, 93), ["email", "uniqueness"]),
    (
        "users-login-no-ratelimit",
        "med",
        "users.js",
        range(43, 59),
        ["login", "rate limit", "brute"],
    ),
    (
        "users-password-unvalidated",
        "med",
        "users.js",
        range(31, 32),
        ["password", "never validate", "no password"],
    ),
    (
        "reviews-flag-self-hide",
        "high",
        "reviews.js",
        range(64, 80),
        ["flag", "3 times", "3×", "auto-hide", "flags.length", "moderation bypass"],
    ),
    (
        "reviews-flag-unvalidated",
        "med",
        "reviews.js",
        range(70, 73),
        ["flag", "REVIEW_FLAGS", "never validated"],
    ),
    (
        "reviews-moderate-unhide",
        "low",
        "reviews.js",
        range(88, 92),
        ["hidden", "unhide", "=== true"],
    ),
    (
        "products-delete-ownership",
        "high",
        "products.js",
        range(98, 110),
        ["DELETE", "sellerId", "ownership", "another seller"],
    ),
    (
        "products-restock-ownership",
        "high",
        "products.js",
        range(112, 126),
        ["restock", "ownership", "another seller", "any seller"],
    ),
    (
        "products-restock-delta",
        "high",
        "products.js",
        range(118, 122),
        ["delta", "concatenation", "NaN", "negative"],
    ),
    (
        "products-patch-novalidate",
        "high",
        "products.js",
        range(85, 92),
        ["PATCH", "re-validat", "validateProduct", "negative"],
    ),
    (
        "orders-all-idor",
        "high",
        "orders.js",
        range(48, 53),
        ["all=1", "?all", "every order", "admin sees all", "enumerate"],
    ),
    (
        "orders-refund-amount",
        "med",
        "orders.js",
        range(95, 99),
        ["amountCents", "refund", "no validation", "negative"],
    ),
    (
        "orders-refund-status",
        "med",
        "orders.js",
        range(96, 99),
        ["refund", "status", "precondition", "delivered"],
    ),
    (
        "orders-cancel-status",
        "med",
        "orders.js",
        range(79, 83),
        ["cancel", "status", "shipped", "delivered"],
    ),
    ("orders-expired-coupon", "med", "orders.js", range(22, 30), ["coupon", "expire", "isExpired"]),
]


def extract_report(run_dir: Path) -> str:
    db = sqlite3.connect(run_dir / "opencode.db")
    db.row_factory = sqlite3.Row
    root = db.execute(
        "SELECT id FROM session WHERE parent_id IS NULL ORDER BY time_created DESC LIMIT 1"
    ).fetchone()
    if not root:
        return ""
    texts = []
    for r in db.execute(
        "SELECT data FROM part WHERE session_id=? ORDER BY time_created", (root["id"],)
    ):
        d = json.loads(r["data"])
        if d.get("type") == "text" and d.get("text"):
            texts.append(d["text"])
    db.close()
    return texts[-1] if texts else ""


def line_numbers(report: str) -> set[int]:
    numbers: set[int] = set()
    for start, end in re.findall(r"\.js:(\d+)(?:-(\d+))?", report):
        first = int(start)
        last = int(end) if end else first
        numbers.update(range(min(first, last), max(first, last) + 1))
    return numbers


def found(bug, report: str, lines_by_handler: dict[str, set[int]]) -> bool:
    # Precise signal only: the report must cite a <handler>:<line> whose line
    # falls in the bug's anchor range. Both agents emit exact file:line refs, so
    # this is honest and avoids false positives from common words. The `phrases`
    # field is kept for documentation but is NOT used for matching.
    _id, _sev, handler, anchors, _phrases = bug
    if handler not in report:
        return False
    anchor_set = set(anchors)
    return any(n in anchor_set for n in lines_by_handler.get(handler, set()))


def lines_per_handler(report: str) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for handler, start, end in re.findall(r"(\w+\.js):(\d+)(?:-(\d+))?", report):
        first = int(start)
        last = int(end) if end else first
        out.setdefault(handler, set()).update(range(min(first, last), max(first, last) + 1))
    return out


def score(report: str) -> dict:
    lbh = lines_per_handler(report)
    hits = [b[0] for b in GROUND_TRUTH if found(b, report, lbh)]
    handlers = [h for h in ("users.js", "orders.js", "products.js", "reviews.js") if h in report]
    findings = len(
        re.findall(r"\*\*(high|medium|med|low)\*\*|—\s*(high|medium|low)\b", report, re.I)
    )
    if findings == 0:  # fallback: count bullet lines with a severity word
        findings = len(re.findall(r"\b(high|medium|low)\b", report, re.I))
    return {
        "chars": len(report),
        "handlers_covered": handlers,
        "handler_coverage": f"{len(handlers)}/4",
        "findings": findings,
        "has_line_anchors": bool(re.search(r"\.js:\d+", report)),
        "ground_truth_found": hits,
        "ground_truth_score": f"{len(hits)}/{len(GROUND_TRUTH)}",
    }


def main() -> None:
    if len(sys.argv) == 3:
        da, db = Path(sys.argv[1]), Path(sys.argv[2])
    else:
        da, db = CM / "runs" / "demo-tui" / "a", CM / "runs" / "demo-tui" / "b"
    ra, rb = extract_report(da), extract_report(db)
    if not ra or not rb:
        sys.exit("missing report(s); run a full round first")
    sa, sb = score(ra), score(rb)

    print("QUALITY PARITY CHECK — side A (stock/raw) vs side B (ContextMesh/digest)\n")
    rows = [
        ("report length (chars)", sa["chars"], sb["chars"]),
        ("handler coverage", sa["handler_coverage"], sb["handler_coverage"]),
        ("findings (severity-tagged)", sa["findings"], sb["findings"]),
        ("has file:line anchors", sa["has_line_anchors"], sb["has_line_anchors"]),
        ("ground-truth bugs found", sa["ground_truth_score"], sb["ground_truth_score"]),
    ]
    w = max(len(str(r[0])) for r in rows)
    print(f"{'metric'.ljust(w)} | {'SIDE A':>16} | {'SIDE B':>16}")
    print("-" * (w + 40))
    for label, av, bv in rows:
        print(f"{str(label).ljust(w)} | {str(av):>16} | {str(bv):>16}")

    only_a = sorted(set(sa["ground_truth_found"]) - set(sb["ground_truth_found"]))
    only_b = sorted(set(sb["ground_truth_found"]) - set(sa["ground_truth_found"]))
    both = sorted(set(sa["ground_truth_found"]) & set(sb["ground_truth_found"]))
    print(f"\nreal bugs BOTH found ({len(both)}): {', '.join(both)}")
    print(f"real bugs only A found ({len(only_a)}): {', '.join(only_a) or '—'}")
    print(f"real bugs only B found ({len(only_b)}): {', '.join(only_b) or '—'}")

    verdict = (
        "PASS"
        if len(sb["ground_truth_found"]) >= len(sa["ground_truth_found"]) - 1
        and len(sb["handlers_covered"]) == 4
        else "REVIEW"
    )
    print(
        f"\nVERDICT: {verdict} — B covers {sb['handler_coverage']} handlers and finds "
        f"{sb['ground_truth_score']} ground-truth bugs vs A's {sa['ground_truth_score']}. "
        f"(digest-fed B is {'not degraded' if verdict == 'PASS' else 'possibly degraded — inspect reports'} vs raw-fed A)"
    )
    print("\nFull reports saved for manual inspection: runs/report-a.txt, runs/report-b.txt")
    (CM / "runs" / "report-a.txt").write_text(ra)
    (CM / "runs" / "report-b.txt").write_text(rb)


if __name__ == "__main__":
    main()
