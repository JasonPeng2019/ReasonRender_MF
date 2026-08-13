#!/usr/bin/env bash
# Launch one visible arm of the interactive RRCv2 comparison.
#
#   demo_tui.sh reset                 create a fresh three-arm round
#   demo_tui.sh prep                  materialize RuleForge + real Lane B packets
#   demo_tui.sh raw                   raw OpenCode: rebuild the long spec
#   demo_tui.sh raw-recovery          resume one failed Raw slice in its TUI
#   demo_tui.sh raw-worker-recovery   resume one failed cheap worker slice
#   demo_tui.sh raw-alt-worker        resume it with a validated cheap fallback
#   demo_tui.sh contextmesh           Raw + ContextMesh: rebuild the long spec
#   demo_tui.sh full                  ContextMesh + RRCv2: packets + code digests
#
# The arms use isolated workspaces/sessions but the identical RuleForge code
# and four semantically repeated policy tasks. Each TUI starts its task
# immediately; watch `demo.sh meter3` in a fourth terminal.
set -euo pipefail

SIDE="${1:-}"
case "$SIDE" in
  a) SIDE="raw" ;; b) SIDE="full" ;; seed) SIDE="prep" ;;
esac
[[ "$SIDE" =~ ^(reset|prep|raw|raw-recovery|raw-worker-recovery|raw-alt-worker|contextmesh|full)$ ]] || {
  echo "usage: demo_tui.sh <reset|prep|raw|raw-recovery|raw-worker-recovery|raw-alt-worker|contextmesh|full>"
  exit 1
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
if [ -f "$ROOT/.env.local" ]; then
  # shellcheck disable=SC1091
  source <(tr -d '\r' < "$ROOT/.env.local")
fi
if [ -z "${OLLAMA_API_KEY:-}" ]; then
  PROXY_PID="$(pgrep -f '/root/.cache/contextmesh/token-tracker-venv/bin/python.*token_tracker_proxy.py serve' | tail -n 1 || true)"
  if [ -n "$PROXY_PID" ] && [ -r "/proc/$PROXY_PID/environ" ]; then
    export "$(tr '\0' '\n' < "/proc/$PROXY_PID/environ" | grep '^OLLAMA_API_KEY=' || true)"
  fi
fi
: "${OLLAMA_API_KEY:?Set OLLAMA_API_KEY in contextmesh/.env.local or the WSL environment.}"
: "${CONTEXTMESH_MODEL:=deepseek-v4-flash:preview}"
export OLLAMA_API_KEY CONTEXTMESH_MODEL

ROUNDFILE="$ROOT/runs/demo-tui/round"
mkdir -p "$ROOT/runs/demo-tui"
if [ "$SIDE" = "reset" ]; then
  echo "r$(date +%s)" > "$ROUNDFILE"
  echo "new three-arm round: $(cat "$ROUNDFILE")"
  echo "next: demo_tui.sh prep, then open raw / contextmesh / full and demo.sh meter3"
  exit 0
fi

[ -f "$ROUNDFILE" ] || echo "r$(date +%s)" > "$ROUNDFILE"
ROUND="$(cat "$ROUNDFILE")"
ROUNDROOT="$ROOT/runs/demo-tui/$ROUND"
RRCROOT="$ROUNDROOT/rrc"

if [ "$SIDE" = "prep" ]; then
  bash "$ROOT/scripts/start_stack.sh"
  CACHE_DIR="$RRCROOT/workspace/.rrc-cache"
  if [ ! -f "$RRCROOT/manifest.json" ] || \
     [ ! -f "$RRCROOT/baseline_prompt.md" ] || \
     [ ! -f "$RRCROOT/cached_prompt.md" ] || \
     [ ! -f "$RRCROOT/lane_b_proof.json" ] || \
     [ ! -f "$CACHE_DIR/template.json" ] || \
     [ ! -f "$CACHE_DIR/bindings.json" ]; then
    python3 "$ROOT/bench/rrc_long_spec_demo.py" --out "$RRCROOT" --prepare-tui
  fi
  echo "three-arm TUI material is ready: $ROUNDROOT"
  echo "open: demo_tui.sh raw | contextmesh | full, then demo.sh meter3"
  exit 0
fi

[ -f "$RRCROOT/manifest.json" ] || {
  echo "run demo_tui.sh prep before opening an arm"
  exit 1
}

if [ -z "${CONTEXTMESH_OPENCODE_BIN:-}" ] && [ -x "$HOME/.opencode/bin/opencode" ]; then
  export CONTEXTMESH_OPENCODE_BIN="$HOME/.opencode/bin/opencode"
fi
OC="${CONTEXTMESH_OPENCODE_BIN:-opencode}"
command -v "$OC" >/dev/null || { echo "opencode binary '$OC' not found on PATH"; exit 1; }

DEMO="$ROUNDROOT/$SIDE"
if [ "$SIDE" = "raw-recovery" ] || [ "$SIDE" = "raw-worker-recovery" ] || [ "$SIDE" = "raw-alt-worker" ]; then
  DEMO="$ROUNDROOT/raw"
fi
if [ ! -d "$DEMO/target/.git" ]; then
  mkdir -p "$DEMO/config-dir" "$DEMO/target"
  cp -R "$RRCROOT/workspace/." "$DEMO/target/"
  git -C "$DEMO/target" init -q
  git -C "$DEMO/target" add -A
  git -C "$DEMO/target" -c user.email=demo@contextmesh -c user.name=demo commit -qm "three-arm demo workspace"
fi

case "$SIDE" in
  raw)
    ARM="a"
    PROMPT="$RRCROOT/baseline_prompt.md"
    LABEL="RAW — rebuild long spec from source"
    ;;
  raw-recovery)
    ARM="a"
    PROMPT="$DEMO/recovery-risk-prompt.md"
    LABEL="RAW recovery"
    ;;
  raw-worker-recovery)
    ARM="a"
    PROMPT="$DEMO/recovery-risk-worker-prompt.md"
    LABEL="RAW cheap worker recovery"
    ;;
  raw-alt-worker)
    ARM="a"
    PROMPT="$DEMO/recovery-risk-worker-prompt.md"
    LABEL="RAW cheap fallback worker recovery"
    ;;
  contextmesh)
    ARM="b"
    PROMPT="$RRCROOT/baseline_prompt.md"
    LABEL="RAW + CONTEXTMESH — rebuild long spec from source"
    ;;
  full)
    ARM="b"
    PROMPT="$RRCROOT/cached_prompt.md"
    LABEL="CONTEXTMESH + RRCv2 — Lane B packet plus code digests"
    ;;
esac

SESSION="demo-$ROUND-$SIDE"
if [ "$SIDE" = "raw-recovery" ] || [ "$SIDE" = "raw-worker-recovery" ] || [ "$SIDE" = "raw-alt-worker" ]; then
  SESSION="demo-$ROUND-raw"
fi
export OPENCODE_DB="$DEMO/opencode.db"
export OPENCODE_CONFIG="$ROOT/configs/arm-$ARM.json"
export OPENCODE_CONFIG_DIR="$DEMO/config-dir"
export OPENCODE_DISABLE_PROJECT_CONFIG=1
export OPENCODE_DISABLE_AUTOCOMPACT=1
export OPENCODE_DISABLE_AUTOUPDATE=1
export CONTEXTMESH_PROXY_BASE="http://127.0.0.1:8788/ollama/$SESSION/v1"

if [ "$SIDE" = "contextmesh" ] || [ "$SIDE" = "full" ]; then
  export CONTEXTMESH_PLUGIN_PATH="file://$ROOT/plugin/contextmesh.ts"
  export CONTEXTMESH_LOG="$DEMO/metrics.jsonl"
  export CONTEXTMESH_EVEROS_URL="http://127.0.0.1:8000"
  export CONTEXTMESH_APP_ID="cm-three-$ROUND-$SIDE"
  export CONTEXTMESH_SUMMARIZER_URL="http://127.0.0.1:8788/ollama/$SESSION-summarizer/v1/chat/completions"
  export CONTEXTMESH_SUMMARIZER_MODEL="$CONTEXTMESH_MODEL"
  export CONTEXTMESH_SYNC_SUMMARIZE=1
  # Ordinary source digests can exceed 45%; accept realistic summaries while
  # reread blocking turns the four workers' repeated reads into cache hits.
  export CONTEXTMESH_MAX_DIGEST_RATIO=0.80
  export CONTEXTMESH_AUTHORITATIVE_DIGEST=1
  # Keep repeated source reads interceptable; raw arm never sets this plugin.
  export CONTEXTMESH_BLOCK_REREAD=1
else
  unset CONTEXTMESH_PLUGIN_PATH CONTEXTMESH_LOG CONTEXTMESH_EVEROS_URL \
    CONTEXTMESH_APP_ID CONTEXTMESH_SUMMARIZER_URL CONTEXTMESH_SUMMARIZER_MODEL \
    CONTEXTMESH_SYNC_SUMMARIZE CONTEXTMESH_MAX_DIGEST_RATIO \
    CONTEXTMESH_AUTHORITATIVE_DIGEST CONTEXTMESH_BLOCK_REREAD
fi

cp "$PROMPT" "$DEMO/prompt.md"
cd "$DEMO/target"
export PWD="$DEMO/target"

echo "=== $LABEL ==="
echo "session: $SESSION"
echo "prompt: $DEMO/prompt.md"
echo "meter: bash $ROOT/demo.sh meter3"
echo
if [ "${RRC_TUI_AUTORUN:-1}" = "1" ]; then
  AGENT="orchestrator"
  MODEL="ollama/deepseek-v4-pro"
  if [ "$SIDE" = "raw-recovery" ]; then
    AGENT="recovery_orchestrator"
  fi
  if [ "$SIDE" = "raw-worker-recovery" ]; then
    AGENT="recovery_worker"
    MODEL="ollama/deepseek-v4-flash:preview"
  fi
  if [ "$SIDE" = "raw-alt-worker" ]; then
    AGENT="recovery_worker"
    MODEL="ollama/gpt-oss:20b"
  fi
  exec "$OC" run --interactive --auto --agent "$AGENT" \
    --model "$MODEL" "$(cat "$DEMO/prompt.md")"
fi
exec "$OC"
