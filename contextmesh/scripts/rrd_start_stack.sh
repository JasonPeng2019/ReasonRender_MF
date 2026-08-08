#!/usr/bin/env bash
# Start the ordinary EverOS stack plus an RRD-dedicated Tollgate on 127.0.0.1:8789.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
RUNS="$ROOT/runs"
mkdir -p "$RUNS"
# shellcheck disable=SC1091
source "$ROOT/.env.local"

"$ROOT/scripts/start_stack.sh"

origin="$(python3 "$ROOT/scripts/rrd_origin.py" "${OLLAMA_BASE_URL:-https://ollama.com/v1}")"
route_digest="$(printf '%s' "$origin" | shasum -a 256 | awk '{print $1}')"

if curl -sf -m 2 http://127.0.0.1:8789/healthz >/dev/null 2>&1; then
  active="$(cat "$RUNS/rrd-tollgate.route" 2>/dev/null || true)"
  if [ "$active" != "$route_digest" ]; then
    echo "RRD Tollgate :8789 is healthy but has a different upstream route." >&2
    echo "Run: $ROOT/RRDdemo.sh down && $ROOT/RRDdemo.sh up" >&2
    exit 1
  fi
  echo "rrd tollgate: already running on :8789"
  tollgate_ready=1
fi

if [ "${tollgate_ready:-0}" != 1 ]; then (
  cd "$REPO/TokenTracker"
  nohup uv run python token_tracker_proxy.py serve --port 8789 \
    --log "$RUNS/rrd-tokens.jsonl" \
    --route "ollama=openai@$origin" \
    >"$RUNS/rrd-tollgate.log" 2>&1 &
  echo $! > "$RUNS/rrd-tollgate.pid"
)
printf '%s\n' "$route_digest" > "$RUNS/rrd-tollgate.route"
for _ in $(seq 1 40); do
  curl -sf -m 2 http://127.0.0.1:8789/healthz >/dev/null 2>&1 && break
  sleep 0.25
done
if ! curl -sf -m 2 http://127.0.0.1:8789/healthz >/dev/null 2>&1; then
  echo "RRD Tollgate failed to start; recent log output:" >&2
  tail -30 "$RUNS/rrd-tollgate.log" >&2 || true
  exit 1
fi
echo "rrd tollgate: started on :8789 (sink: $RUNS/rrd-tokens.jsonl)"
fi

# The loopback response adapter buffers completed worker streams and replaces
# oversized reports only after a bounded summary succeeds. On any failure it
# forwards the untouched upstream stream.
proxy_digest="$(shasum -a 256 "$ROOT/scripts/rrd_response_proxy.py" | awk '{print $1}')"
if curl -sf -m 2 http://127.0.0.1:8790/healthz >/dev/null 2>&1; then
  active_proxy="$(cat "$RUNS/rrd-response-proxy.version" 2>/dev/null || true)"
  if [ "$active_proxy" != "$proxy_digest" ]; then
    echo "RRD response proxy :8790 is healthy but its code version is stale or unknown." >&2
    echo "Run: $ROOT/RRDdemo.sh down && $ROOT/RRDdemo.sh up" >&2
    exit 1
  fi
  echo "rrd response proxy: already running on :8790"
else
  nohup python3 "$ROOT/scripts/rrd_response_proxy.py" \
    --port 8790 --upstream http://127.0.0.1:8789 --runs "$RUNS" \
    >"$RUNS/rrd-response-proxy.log" 2>&1 &
  echo $! > "$RUNS/rrd-response-proxy.pid"
  printf '%s\n' "$proxy_digest" > "$RUNS/rrd-response-proxy.version"
  for _ in $(seq 1 40); do
    curl -sf -m 2 http://127.0.0.1:8790/healthz >/dev/null 2>&1 && break
    sleep 0.1
  done
  if ! curl -sf -m 2 http://127.0.0.1:8790/healthz >/dev/null 2>&1; then
    echo "RRD response proxy failed to start; recent log output:" >&2
    tail -30 "$RUNS/rrd-response-proxy.log" >&2 || true
    exit 1
  fi
  echo "rrd response proxy: started on :8790"
fi
