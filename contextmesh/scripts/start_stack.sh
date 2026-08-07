#!/usr/bin/env bash
# Start the ContextMesh measurement + memory stack:
#   1. Tollgate token proxy on :8787  (route ollama=openai@https://ollama.com)
#   2. EverOS memory server on :8000  (LLM slot pointed through Tollgate)
# Idempotent: skips a component whose port is already serving.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
RUNS="$ROOT/runs"
mkdir -p "$RUNS"

# shellcheck disable=SC1091
source "$ROOT/.env.local"

# --- Tollgate ---
if curl -sf http://127.0.0.1:8788/healthz >/dev/null 2>&1; then
  echo "tollgate: already running on :8788"
else
  (
    cd "$REPO/TokenTracker"
    nohup uv run python token_tracker_proxy.py serve --port 8788 \
      --log "$RUNS/tokens.jsonl" \
      --route "ollama=openai@https://ollama.com" \
      >"$RUNS/tollgate.log" 2>&1 &
    echo $! > "$RUNS/tollgate.pid"
  )
  for _ in $(seq 1 30); do
    curl -sf http://127.0.0.1:8788/healthz >/dev/null 2>&1 && break
    sleep 0.5
  done
  curl -sf http://127.0.0.1:8788/healthz >/dev/null
  echo "tollgate: started on :8788 (log: $RUNS/tollgate.log, sink: $RUNS/tokens.jsonl)"
fi

# --- EverOS ---
if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "everos: already running on :8000"
else
  EVROOT="$ROOT/everos-root"
  mkdir -p "$EVROOT"
  (
    cd "$REPO/EverOS"
    if [ ! -f "$EVROOT/everos.toml" ]; then
      uv run everos init --root "$EVROOT"
    fi
    # LLM slot is OpenAI-protocol; route it through Tollgate so EverOS's own
    # extraction cost is metered under the session "everos-extraction".
    EVEROS_ROOT="$EVROOT" \
    EVEROS_LLM__API_KEY="$OLLAMA_API_KEY" \
    EVEROS_LLM__BASE_URL="http://127.0.0.1:8788/ollama/everos-extraction/v1" \
    EVEROS_LLM__MODEL="$CONTEXTMESH_MODEL" \
    nohup uv run everos server start --root "$EVROOT" \
      >"$RUNS/everos.log" 2>&1 &
    echo $! > "$RUNS/everos.pid"
  )
  for _ in $(seq 1 60); do
    curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && break
    sleep 0.5
  done
  curl -sf http://127.0.0.1:8000/health >/dev/null
  echo "everos: started on :8000 (root: $EVROOT, log: $RUNS/everos.log)"
fi

echo "stack ready"
