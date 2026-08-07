#!/usr/bin/env python3
"""Load ContextMesh telemetry into Snowflake.

Sources:
  - contextmesh/runs/tokens.jsonl            → AGENT_TOKEN_EVENTS
  - contextmesh/runs/<runid>/*/run_summary.json → BENCH_RUN_SUMMARY

Session naming convention parsed into dimensions:
  '<runid>-<arm>-<mode>'              → agent traffic
  '<runid>-<arm>-<mode>-summarizer'   → optimizer overhead (IS_SUMMARIZER)
  'everos-extraction'                 → EverOS memory-layer LLM traffic

Usage:
  python3 load.py --dry-run                 # validate + print row counts, no connection
  python3 load.py --account ... --user ... --password-env SNOWFLAKE_PASSWORD \
                  --database DB --schema CONTEXTMESH --warehouse WH
Requires `pip install snowflake-connector-python` for a real load.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

CM = Path(__file__).resolve().parent.parent
SESSION_RE = re.compile(r"^(?P<runid>.+)-(?P<arm>[ab])-(?P<mode>cold|warm)(?P<summ>-summarizer)?$")


def parse_session(session: str | None) -> tuple[str | None, str | None, str | None, bool]:
    if not session:
        return None, None, None, False
    m = SESSION_RE.match(session)
    if not m:
        return None, None, None, session.endswith("-summarizer")
    return m["runid"], m["arm"], m["mode"], bool(m["summ"])


def token_event_rows() -> list[dict]:
    rows = []
    path = CM / "runs" / "tokens.jsonl"
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        runid, arm, mode, summ = parse_session(r.get("session"))
        rows.append(
            {
                "RECORD_ID": r.get("record_id"),
                "TS": r.get("ts"),
                "COMPLETED_TS": r.get("completed_ts"),
                "SESSION": r.get("session"),
                "RUN_ID": runid,
                "ARM": arm,
                "MODE": mode,
                "IS_SUMMARIZER": summ,
                "PROVIDER": r.get("provider"),
                "KIND": r.get("kind"),
                "MODEL": r.get("model"),
                "ENDPOINT": r.get("endpoint"),
                "STREAM": r.get("stream"),
                "STATUS": r.get("status"),
                "MEASUREMENT_STATE": r.get("measurement_state"),
                "INPUT_TOKENS": r.get("input_tokens"),
                "OUTPUT_TOKENS": r.get("output_tokens"),
                "REASONING_TOKENS": r.get("reasoning_tokens"),
                "CACHE_READ_TOKENS": r.get("cache_read_input_tokens"),
                "CACHE_WRITE_TOKENS": r.get("cache_creation_input_tokens"),
                "TOTAL_TOKENS": r.get("total_tokens"),
                "LATENCY_MS": r.get("latency_ms"),
                "REQUEST_ID": r.get("request_id"),
                "RAW": r,
            }
        )
    return rows


def summary_rows() -> list[dict]:
    rows = []
    for f in sorted((CM / "runs").glob("*/*/run_summary.json")):
        s = json.loads(f.read_text())
        runid = f.parent.parent.name
        oc = s["opencode"]["totals"]
        ev = s.get("plugin_events", {})
        rows.append(
            {
                "RUN_ID": runid,
                "LABEL": s["label"],
                "SESSION": s["session"],
                "EXIT_CODE": s["exit_code"],
                "WALL_SECONDS": s["wall_seconds"],
                "SUBAGENT_COUNT": s["opencode"]["subagent_count"],
                "FULL_READS": s["opencode"]["read_calls_full"],
                "DUP_FULL_READS": s["opencode"]["duplicate_full_reads"],
                "OC_INPUT": oc["input"] + oc["cache_read"] + oc["cache_write"],
                "OC_OUTPUT": oc["output"] + oc["reasoning"],
                "DIGEST_HITS": ev.get("digest_hit", 0),
                "DIGEST_STORED": ev.get("digest_stored", 0),
                "TASKS_COMPRESSED": ev.get("task_compressed", 0),
                "ESCAPE_HATCHES": ev.get("escape_hatch", 0),
                "RAW": s,
            }
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--account")
    ap.add_argument("--user")
    ap.add_argument("--password-env", default="SNOWFLAKE_PASSWORD")
    ap.add_argument("--database")
    ap.add_argument("--schema", default="CONTEXTMESH")
    ap.add_argument("--warehouse")
    args = ap.parse_args()

    events = token_event_rows()
    summaries = summary_rows()
    exact = sum(1 for e in events if e["MEASUREMENT_STATE"] == "exact")
    attributed = sum(1 for e in events if e["RUN_ID"])
    print(f"AGENT_TOKEN_EVENTS: {len(events)} rows ({exact} exact, {attributed} run-attributed)")
    print(f"BENCH_RUN_SUMMARY:  {len(summaries)} rows: {[ (r['RUN_ID'], r['LABEL']) for r in summaries ]}")

    if args.dry_run:
        for r in events[:2]:
            print("sample event:", json.dumps({k: v for k, v in r.items() if k != "RAW"}, default=str))
        print("dry run complete — no connection made")
        return

    try:
        import snowflake.connector  # type: ignore
    except ImportError:
        sys.exit("pip install snowflake-connector-python (or use --dry-run)")

    conn = snowflake.connector.connect(
        account=args.account,
        user=args.user,
        password=os.environ[args.password_env],
        database=args.database,
        schema=args.schema,
        warehouse=args.warehouse,
    )
    cur = conn.cursor()
    cur.execute(open(Path(__file__).parent / "ddl.sql").read(), num_statements=0)
    for r in events:
        cur.execute(
            "MERGE INTO AGENT_TOKEN_EVENTS t USING (SELECT %(RECORD_ID)s AS RECORD_ID) s ON t.RECORD_ID = s.RECORD_ID "
            "WHEN NOT MATCHED THEN INSERT (RECORD_ID, TS, COMPLETED_TS, SESSION, RUN_ID, ARM, MODE, IS_SUMMARIZER, PROVIDER, KIND, MODEL, ENDPOINT, STREAM, STATUS, MEASUREMENT_STATE, INPUT_TOKENS, OUTPUT_TOKENS, REASONING_TOKENS, CACHE_READ_TOKENS, CACHE_WRITE_TOKENS, TOTAL_TOKENS, LATENCY_MS, REQUEST_ID, RAW) "
            "VALUES (%(RECORD_ID)s, %(TS)s, %(COMPLETED_TS)s, %(SESSION)s, %(RUN_ID)s, %(ARM)s, %(MODE)s, %(IS_SUMMARIZER)s, %(PROVIDER)s, %(KIND)s, %(MODEL)s, %(ENDPOINT)s, %(STREAM)s, %(STATUS)s, %(MEASUREMENT_STATE)s, %(INPUT_TOKENS)s, %(OUTPUT_TOKENS)s, %(REASONING_TOKENS)s, %(CACHE_READ_TOKENS)s, %(CACHE_WRITE_TOKENS)s, %(TOTAL_TOKENS)s, %(LATENCY_MS)s, %(REQUEST_ID)s, PARSE_JSON(%(RAW_JSON)s))",
            {**{k: v for k, v in r.items() if k != "RAW"}, "RAW_JSON": json.dumps(r["RAW"])},
        )
    for r in summaries:
        cur.execute("DELETE FROM BENCH_RUN_SUMMARY WHERE RUN_ID = %(RUN_ID)s AND LABEL = %(LABEL)s", r)
        cur.execute(
            "INSERT INTO BENCH_RUN_SUMMARY (RUN_ID, LABEL, SESSION, EXIT_CODE, WALL_SECONDS, SUBAGENT_COUNT, FULL_READS, DUP_FULL_READS, OC_INPUT, OC_OUTPUT, DIGEST_HITS, DIGEST_STORED, TASKS_COMPRESSED, ESCAPE_HATCHES, RAW) "
            "SELECT %(RUN_ID)s, %(LABEL)s, %(SESSION)s, %(EXIT_CODE)s, %(WALL_SECONDS)s, %(SUBAGENT_COUNT)s, %(FULL_READS)s, %(DUP_FULL_READS)s, %(OC_INPUT)s, %(OC_OUTPUT)s, %(DIGEST_HITS)s, %(DIGEST_STORED)s, %(TASKS_COMPRESSED)s, %(ESCAPE_HATCHES)s, PARSE_JSON(%(RAW_JSON)s)",
            {**{k: v for k, v in r.items() if k != "RAW"}, "RAW_JSON": json.dumps(r["RAW"])},
        )
    conn.commit()
    print("loaded.")


if __name__ == "__main__":
    main()
