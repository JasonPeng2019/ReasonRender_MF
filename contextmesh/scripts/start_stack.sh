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
# TokenTracker refuses logs on the Windows-mounted repo because DrvFS reports
# those directories as group/world-writable. Keep the token log private in WSL
# and leave a harmless path pointer in the repo for the meter and harness.
TOKEN_LOG="${CONTEXTMESH_TOKENS_PATH:-$HOME/.local/state/contextmesh/tokens.jsonl}"
mkdir -p "$(dirname "$TOKEN_LOG")"
chmod 700 "$(dirname "$TOKEN_LOG")"
touch "$TOKEN_LOG"
chmod 600 "$TOKEN_LOG"
printf '%s\n' "$TOKEN_LOG" > "$RUNS/token-log-path"

# shellcheck disable=SC1091
# Prefer the gitignored file, but also permit a transient WSL environment so a
# live demo never requires writing a provider key to disk.
if [ -f "$ROOT/.env.local" ]; then
  source <(tr -d '\r' < "$ROOT/.env.local")
fi
: "${OLLAMA_API_KEY:?Set OLLAMA_API_KEY in contextmesh/.env.local or the WSL environment.}"
: "${CONTEXTMESH_MODEL:=deepseek-v4-flash:preview}"

# --- Tollgate ---
if curl -sf http://127.0.0.1:8788/healthz >/dev/null 2>&1; then
  echo "tollgate: already running on :8788"
else
  (
    cd "$REPO/TokenTracker"
    nohup uv run python token_tracker_proxy.py serve --port 8788 \
      --log "$TOKEN_LOG" \
      --route "ollama=openai@https://ollama.com" \
      >"$RUNS/tollgate.log" 2>&1 &
    echo $! > "$RUNS/tollgate.pid"
  )
  # First WSL run may need to create uv's environment and download packages.
  for _ in $(seq 1 240); do
    curl -sf http://127.0.0.1:8788/healthz >/dev/null 2>&1 && break
    sleep 0.5
  done
  curl -sf http://127.0.0.1:8788/healthz >/dev/null
  echo "tollgate: started on :8788 (log: $RUNS/tollgate.log, sink: $TOKEN_LOG)"
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
  # EverOS may likewise need a first-run environment build.
  for _ in $(seq 1 240); do
    curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && break
    sleep 0.5
  done
  curl -sf http://127.0.0.1:8000/health >/dev/null
  echo "everos: started on :8000 (root: $EVROOT, log: $RUNS/everos.log)"
fi

echo "stack ready"
