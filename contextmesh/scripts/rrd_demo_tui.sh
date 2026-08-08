#!/usr/bin/env bash
# Launch one side of the combined ContextMesh + ReasonRenderCoding demo:
#
#   rrd_demo_tui.sh a       COLD RRC packets + ContextMesh + four workers
#   rrd_demo_tui.sh b       WARM RRC packet reuse + ContextMesh + four workers
#   rrd_demo_tui.sh seed    pre-seed shared-file ContextMesh digests
#   rrd_demo_tui.sh reset   start a round shared by both sides and the meter
set -euo pipefail

SIDE="${1:-}"
[[ "$SIDE" =~ ^(a|b|seed|reset)$ ]] || { echo "usage: rrd_demo_tui.sh <reset|seed|a|b>"; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.env.local"

RUNS="$ROOT/runs/rrd-demo"
ROUNDFILE="$RUNS/round"
mkdir -p "$RUNS"

if [ "$SIDE" = "reset" ]; then
  ROUND="rrd-$(date +%s)"
  printf '%s\n' "$ROUND" > "$ROUNDFILE"
  mkdir -p "$RUNS/$ROUND"
  touch "$RUNS/$ROUND/.combined-multiagent-v1"
  echo "new combined ContextMesh + ReasonRenderCoding round: $ROUND"
  echo "next: RRDdemo.sh seed, then RRDdemo.sh a / b / meter"
  exit 0
fi

[ -s "$ROUNDFILE" ] || {
  echo "No combined demo round exists. Run: $ROOT/RRDdemo.sh prep" >&2
  exit 1
}
ROUND="$(cat "$ROUNDFILE")"
[ -f "$RUNS/$ROUND/.combined-multiagent-v1" ] || {
  echo "Round $ROUND predates the combined multi-agent demo. Run: $ROOT/RRDdemo.sh prep" >&2
  exit 1
}

require_health() {
  local name="$1" url="$2"
  if ! curl -sf -m 2 "$url" >/dev/null 2>&1; then
    echo "$name is not reachable on ${url%/health*}. Run: $ROOT/RRDdemo.sh prep" >&2
    exit 1
  fi
}
require_health "Tollgate" "http://127.0.0.1:8788/healthz"
require_health "EverOS" "http://127.0.0.1:8000/health"

ARM="$SIDE"
MODE="cold"
if [ "$SIDE" = "b" ] || [ "$SIDE" = "seed" ]; then MODE="warm"; fi
if [ "$SIDE" = "seed" ]; then ARM="seed"; fi
MODE_LABEL="$(printf '%s' "$MODE" | tr '[:lower:]' '[:upper:]')"
DEMO="$RUNS/$ROUND/$ARM"
if [ ! -d "$DEMO/target/.git" ]; then
  mkdir -p "$DEMO/config-dir"
  cp -R "$ROOT/bench/target-template" "$DEMO/target"
  git -C "$DEMO/target" init -q
  git -C "$DEMO/target" add -A
  git -C "$DEMO/target" -c user.email=demo@reasonrendercoding -c user.name=demo \
    commit -qm "combined demo workspace"
fi

configured_model=""
if [ -z "${RRC_STRONG_MODEL:-${RRC_MODEL:-}}" ] && [ -f "$HOME/.codex/config.toml" ]; then
  configured_model="$(sed -nE 's/^[[:space:]]*model[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$HOME/.codex/config.toml" | head -1 || true)"
fi
export RRC_STRONG_MODEL="${RRC_STRONG_MODEL:-${RRC_MODEL:-$configured_model}}"
if [ -z "$RRC_STRONG_MODEL" ]; then
  echo "No Codex planner model configured. Set RRC_MODEL or RRC_STRONG_MODEL." >&2
  exit 1
fi

SESSION="rrd-demo-$ROUND-$ARM"
export OPENCODE_DB="$DEMO/opencode.db"
if [ "$SIDE" = "seed" ]; then
  export OPENCODE_CONFIG="$ROOT/configs/rrd-arm-b.json"
else
  export OPENCODE_CONFIG="$ROOT/configs/rrd-arm-$ARM.json"
fi
export OPENCODE_CONFIG_DIR="$DEMO/config-dir"
export OPENCODE_DISABLE_PROJECT_CONFIG=1
export OPENCODE_DISABLE_AUTOCOMPACT=1
export OPENCODE_DISABLE_AUTOUPDATE=1
export CONTEXTMESH_PROXY_BASE="http://127.0.0.1:8788/ollama/$SESSION/v1"
export OLLAMA_API_KEY

# ContextMesh is active in BOTH experimental arms so the COLD-vs-WARM delta
# isolates RRC packet reuse while retaining shared-file/result optimization.
export CONTEXTMESH_PLUGIN_PATH="file://$ROOT/plugin/contextmesh.ts"
export CONTEXTMESH_LOG="$DEMO/contextmesh.jsonl"
export CONTEXTMESH_EVEROS_URL="http://127.0.0.1:8000"
export CONTEXTMESH_APP_ID="rrd-combined-demo"
export CONTEXTMESH_SUMMARIZER_URL="http://127.0.0.1:8788/ollama/$SESSION-summarizer/v1/chat/completions"
export CONTEXTMESH_SUMMARIZER_MODEL="$CONTEXTMESH_MODEL"
export CONTEXTMESH_MAX_DIGEST_RATIO="0.45"
export CONTEXTMESH_AUTHORITATIVE_DIGEST="1"
export CONTEXTMESH_BLOCK_REREAD="1"

# ReasonRenderCoding augments each OpenCode worker task at tool.execute.before.
export RRC_PLUGIN_PATH="file://$ROOT/plugin/reasonrendercoding.ts"
export RRC_DEMO_REPO="$REPO"
export RRC_DEMO_ROUND="$ROUND"
export RRC_DEMO_MODE="$MODE"
export RRC_DEMO_DATABASE="$DEMO/plan-spec.sqlite"
export RRC_DEMO_LOCK="$DEMO/plan-spec.lock"
export RRC_DEMO_EVENTS="$DEMO/rrc-events.jsonl"
export RRC_DEMO_MODEL_EVENTS="$DEMO/rrc-model-events.jsonl"
export RRC_EVEROS_URL="${RRC_EVEROS_URL:-http://127.0.0.1:8000}"
export RRC_DEMO_UV_BIN="${RRC_DEMO_UV_BIN:-uv}"

cd "$DEMO/target"
export PWD="$DEMO/target"
cp "$ROOT/RRD-demo-prompt.txt" "$ROOT/runs/RRD-demo-prompt.txt"

OC="${CONTEXTMESH_OPENCODE_BIN:-opencode}"
command -v "$OC" >/dev/null || { echo "opencode binary '$OC' not found on PATH"; exit 1; }
command -v codex >/dev/null || { echo "codex CLI not found on PATH"; exit 1; }
command -v "$RRC_DEMO_UV_BIN" >/dev/null || { echo "uv binary '$RRC_DEMO_UV_BIN' not found on PATH"; exit 1; }

if [ "$SIDE" = "seed" ]; then
  export CONTEXTMESH_SYNC_SUMMARIZE=1
  echo "seeding ContextMesh digests for the three shared files…"
  SEED_PROMPT='Read these three files in full, then reply with just the word DONE: src/models.js, src/utils.js, src/middleware.js'
  "$OC" run --format json --auto --agent build \
    --model "ollama/$CONTEXTMESH_MODEL" "$SEED_PROMPT" >/dev/null
  touch "$RUNS/$ROUND/.seeded"
  echo "seed complete — both combined arms will start with the same ContextMesh digests"
  exit 0
fi

echo "=== ContextMesh + ReasonRenderCoding demo TUI — side $ARM ($MODE_LABEL, session: $SESSION) ==="
echo "    opencode: $("$OC" --version 2>/dev/null)   agent: orchestrator"
echo "    plugins: ContextMesh + RRC Plan/Spec task augmentation"
echo "    workload: four parallel OpenCode worker subagents auditing HTTP handlers"
echo "    RRC mode: $MODE_LABEL   planner model: $RRC_STRONG_MODEL"
echo "    meter: $ROOT/RRDdemo.sh meter   (third terminal)"
echo "    prompt: pbcopy < $ROOT/runs/RRD-demo-prompt.txt   then paste (⌘V)"
sleep 1
exec "$OC"
