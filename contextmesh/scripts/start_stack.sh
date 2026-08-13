#!/usr/bin/env bash
# Start ContextMesh's local EverOS exact-key store on :8000.
#
# Agent work is performed by locally authenticated Codex/Claude CLIs. EverOS
# only stores assistant-role buffers for keyword lookup, which never invokes its
# extraction LLM. Its required OpenAI-protocol configuration is therefore an
# intentionally unreachable local placeholder, not a provider credential.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
RUNS="$ROOT/runs"
EVROOT="$ROOT/everos-root"
# EverOS imports Linux's fcntl lock implementation. Keep its environment in
# WSL's private filesystem; this script is intentionally run through WSL while
# the checkout and durable EverOS root remain shared on the mounted workspace.
EVEROS_VENV="${CONTEXTMESH_EVEROS_VENV:-$HOME/.cache/contextmesh/everos-venv}"
mkdir -p "$RUNS" "$EVROOT"

if [ -f "$ROOT/.env.local" ]; then
  # shellcheck disable=SC1090
  source <(tr -d '\r' < "$ROOT/.env.local")
fi

: "${CONTEXTMESH_EVEROS_URL:=http://127.0.0.1:8000}"
: "${CONTEXTMESH_SUMMARIZER_COMMAND:=claude}"
: "${CONTEXTMESH_SUMMARIZER_MODEL:=sonnet}"
: "${CONTEXTMESH_SUMMARIZER_MAX_BUDGET_USD:=0.50}"

if curl -sf "$CONTEXTMESH_EVEROS_URL/health" >/dev/null 2>&1; then
  echo "everos: already running at $CONTEXTMESH_EVEROS_URL"
  exit 0
fi

(
  cd "$REPO/EverOS"
  if [ ! -f "$EVROOT/everos.toml" ]; then
    UV_PROJECT_ENVIRONMENT="$EVEROS_VENV" uv run everos init --root "$EVROOT"
  fi
  EVEROS_ROOT="$EVROOT" \
  EVEROS_LLM__API_KEY="contextmesh-keyword-only" \
  EVEROS_LLM__BASE_URL="http://127.0.0.1:9/v1" \
  EVEROS_LLM__MODEL="contextmesh-keyword-only" \
  UV_PROJECT_ENVIRONMENT="$EVEROS_VENV" nohup uv run everos server start --root "$EVROOT" \
    >"$RUNS/everos.log" 2>&1 &
  echo $! > "$RUNS/everos.pid"
)

for _ in $(seq 1 240); do
  curl -sf "$CONTEXTMESH_EVEROS_URL/health" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf "$CONTEXTMESH_EVEROS_URL/health" >/dev/null
echo "everos: started at $CONTEXTMESH_EVEROS_URL (root: $EVROOT)"
