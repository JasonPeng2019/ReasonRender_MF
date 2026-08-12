#!/bin/bash
# Validate the native Codex ContextMesh/RRC demo without reading local secret files.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="${RRD_MEMORY_BACKEND:-}"
case "$BACKEND" in everos|sqlite) ;; *) echo "FAIL  invalid RRD_MEMORY_BACKEND" >&2; exit 2 ;; esac

CODEX_CANDIDATE="${RRD_CODEX_BIN:-$(command -v codex || true)}"
[ -n "$CODEX_CANDIDATE" ] || { echo "FAIL  Codex is not installed" >&2; exit 1; }
CODEX_BIN="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CODEX_CANDIDATE")"
MODEL="${RRD_CODEX_MODEL:-gpt-5.5}"
WORKER_MODEL="${RRD_WORKER_MODEL:-gpt-5.6-luna}"
WORKER_REASONING="${RRD_WORKER_REASONING:-low}"
CODEX_COMMAND=("$CODEX_BIN")
if [ "$(uname -s)" = Darwin ]; then
  [ -x /usr/bin/sandbox-exec ] || { echo "FAIL  macOS sandbox-exec is unavailable" >&2; exit 1; }
  CODEX_COMMAND=(/usr/bin/sandbox-exec -f "$ROOT/.codex-rrd-native/credential-deny.sb" \
    "$CODEX_BIN" --dangerously-bypass-approvals-and-sandbox)
fi

python3 "$ROOT/scripts/rrd_native_config.py" check --root "$ROOT" \
  --codex-bin "$CODEX_BIN" --model "$MODEL" --worker-model "$WORKER_MODEL" \
  --worker-reasoning "$WORKER_REASONING"

if [ "$BACKEND" = everos ]; then
  /usr/bin/curl -sf -m 5 http://127.0.0.1:8000/health >/dev/null
  echo "PASS  EverOS :8000 is healthy"
else
  echo "PASS  local SQLite mode requires no service"
fi

if [ "${1:-}" = --canary ]; then
  echo "Running one native Codex canary…"
  /usr/bin/env -i HOME="$HOME" CODEX_HOME="$ROOT/.codex-rrd-native" \
    PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    LANG="${LANG:-en_US.UTF-8}" LC_ALL="${LC_ALL:-en_US.UTF-8}" \
    "${CODEX_COMMAND[@]}" exec --strict-config --json --ephemeral --ignore-rules \
    --skip-git-repo-check --model "$MODEL" \
    "Reply with exactly OK." | python3 -c '
import json,sys
message=""; usage=None
for line in sys.stdin:
    event=json.loads(line)
    item=event.get("item",{})
    if event.get("type")=="item.completed" and item.get("type")=="agent_message": message=item.get("text","").strip()
    if event.get("type")=="turn.completed": usage=event.get("usage")
if message != "OK" or not isinstance(usage,dict): raise SystemExit("native canary failed")
print("PASS  native Codex model canary", json.dumps(usage,separators=(",",":")))'
fi

echo "ALL GREEN — native Codex authentication and model configuration are ready."
