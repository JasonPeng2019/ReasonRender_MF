#!/usr/bin/env python3
"""EverOS digest-KV smoke test.

Proves the production properties ContextMesh depends on:
  1. assistant-only buffers are parked with zero LLM calls (`status: accumulated`)
  2. read-your-write: /memory/search with a top-level {"session_id": key} filter
     returns the buffered message verbatim (byte-identical content)
  3. re-POSTing the same key does not corrupt the stored value
  4. a fresh key misses cleanly (empty unprocessed_messages)

Exits non-zero on any failure. Usage: python3 smoke_everos.py [service_url]
"""

import hashlib
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
APP = "cm-smoke"


def call(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)


def put(key: str, content: str) -> str:
    body = call(
        "/api/v2/memory/add",
        {
            "session_id": key,
            "app_id": APP,
            "project_id": "digests",
            "messages": [
                {
                    "sender_id": "contextmesh",
                    "role": "assistant",
                    "timestamp": int(time.time() * 1000),
                    "content": content,
                }
            ],
        },
    )
    return body["data"]["status"]


def get(key: str):
    body = call(
        "/api/v2/memory/search",
        {
            "user_id": "contextmesh",
            "app_id": APP,
            "project_id": "digests",
            "query": "digest",
            "method": "keyword",
            "filters": {"session_id": key},
        },
    )
    msgs = body["data"].get("unprocessed_messages", [])
    return msgs[0]["content"] if msgs else None


def main() -> int:
    failures = []
    payload = json.dumps(
        {
            "v": 1,
            "path": "src/models.js",
            "digest": "L1-L20: validators for User/Order — exact bytes must round-trip: <>&\"'\n\ttabs and unicode ✓ ünïcodé",
        },
        ensure_ascii=False,
    )
    key = "digest:" + hashlib.sha256(payload.encode()).hexdigest()[:56] + ":v1"

    status = put(key, payload)
    if status != "accumulated":
        failures.append(
            f"write status was {status!r}, expected 'accumulated' (buffer was NOT parked!)"
        )

    got = get(key)
    if got != payload:
        failures.append(f"read-your-write failed:\n  wrote: {payload!r}\n  got:   {got!r}")

    status2 = put(key, payload)
    if status2 != "accumulated":
        failures.append(f"second write status was {status2!r}")
    got2 = get(key)
    if got2 != payload:
        failures.append("value corrupted after duplicate write")

    miss = get("digest:" + "0" * 56 + ":v1")
    if miss is not None:
        failures.append(f"expected miss returned {miss!r}")

    if failures:
        print("EVEROS SMOKE: FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print(f"EVEROS SMOKE: PASS (key={key[:24]}…, verbatim round-trip, parked buffer, clean miss)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
