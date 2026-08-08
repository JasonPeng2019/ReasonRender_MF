#!/usr/bin/env bash
# Launch an interactive opencode TUI wired to one demo side:
#
#   demo_tui.sh a            stock opencode  (terminal 1)
#   demo_tui.sh b            opencode + ContextMesh plugin (terminal 2)
#   demo_tui.sh seed         headless warm-up: populate EverOS digests for the
#                            demo workspace so side B hits memory from read one
#   demo_tui.sh a --global   use the globally installed `opencode` binary for
#                            side A instead of the pinned submodule (version
#                            confound: global may differ from v1.18.15 — say so)
#
# Both sides run the SAME opencode binary (installed `opencode`, v1.18.15 — the
# real CLI, which renders the TUI correctly); the ONLY difference is arm-b.json
# loading the ContextMesh plugin. Type the same prompt into both TUIs and watch
# scripts/live_meter.py for the token race.
#
# (We use the installed binary, not `bun run src/index.ts`, because the
# from-source TUI renderer does not draw — that was the blank-screen bug.)
#
# The canonical website/demo prompt is contextmesh/demo-prompt.txt. Both TUI
# arms and the headless benchmark read that same file so their workloads cannot
# drift apart.
set -euo pipefail

SIDE="${1:-}"
[[ "$SIDE" =~ ^(a|b|seed|reset)$ ]] || { echo "usage: demo_tui.sh <reset|seed|a|b>"; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.env.local"

ROUNDFILE="$ROOT/runs/demo-tui/round"
mkdir -p "$ROOT/runs/demo-tui"

# `reset` starts a fresh demo round: new session namespace so the live meter
# counts only this round's traffic (tokens.jsonl accumulates across all runs).
if [ "$SIDE" = "reset" ]; then
  echo "r$(date +%s)" > "$ROUNDFILE"
  echo "new demo round: $(cat "$ROUNDFILE")  — now: demo_tui.sh seed, then side a / side b, then live_meter.py"
  exit 0
fi

[ -f "$ROUNDFILE" ] || echo "r$(date +%s)" > "$ROUNDFILE"
ROUND="$(cat "$ROUNDFILE")"

# Do not enter OpenCode's long retry loop when the local services were not
# started (or died after prep). Keep this check here as well as in demo.sh so a
# direct demo_tui.sh invocation still fails with an actionable command.
require_health() {
  local name="$1" url="$2"
  if ! curl -sf -m 2 "$url" >/dev/null 2>&1; then
    echo "$name is not reachable on ${url%/health*}. Run: $ROOT/demo.sh prep" >&2
    exit 1
  fi
}
require_health "Tollgate" "http://127.0.0.1:8788/healthz"
if [ "$SIDE" = "b" ] || [ "$SIDE" = "seed" ]; then
  require_health "EverOS" "http://127.0.0.1:8000/health"
fi

# One shared demo workspace per side keeps DBs/sessions separate but content identical.
ARM="$SIDE"; [ "$SIDE" = "seed" ] && ARM="b"
DEMO="$ROOT/runs/demo-tui/$ARM"
if [ ! -d "$DEMO/target/.git" ]; then
  mkdir -p "$DEMO/config-dir"
  cp -R "$ROOT/bench/target-template" "$DEMO/target"
  git -C "$DEMO/target" init -q
  git -C "$DEMO/target" add -A
  git -C "$DEMO/target" -c user.email=demo@contextmesh -c user.name=demo commit -qm "demo workspace"
fi

SESSION="demo-$ROUND-$ARM"                     # round-scoped so the meter counts only this round
[ "$SIDE" = "seed" ] && SESSION="demo-$ROUND-seed"   # keep seeding traffic out of the live meter
export OPENCODE_DB="$DEMO/opencode.db"
export OPENCODE_CONFIG="$ROOT/configs/arm-$ARM.json"
export OPENCODE_CONFIG_DIR="$DEMO/config-dir"
export OPENCODE_DISABLE_PROJECT_CONFIG=1
export OPENCODE_DISABLE_AUTOCOMPACT=1
export OPENCODE_DISABLE_AUTOUPDATE=1
export CONTEXTMESH_PROXY_BASE="http://127.0.0.1:8788/ollama/$SESSION/v1"
export OLLAMA_API_KEY

if [ "$ARM" = "b" ]; then
  export CONTEXTMESH_PLUGIN_PATH="file://$ROOT/plugin/contextmesh.ts"
  export CONTEXTMESH_LOG="$DEMO/metrics-$ROUND.jsonl"
  export CONTEXTMESH_EVEROS_URL="http://127.0.0.1:8000"
  export CONTEXTMESH_APP_ID="cm-demo"          # stable namespace → seed once, warm forever
  export CONTEXTMESH_SUMMARIZER_URL="http://127.0.0.1:8788/ollama/$SESSION-summarizer/v1/chat/completions"
  export CONTEXTMESH_SUMMARIZER_MODEL="$CONTEXTMESH_MODEL"
  export CONTEXTMESH_MAX_DIGEST_RATIO="0.45"
  # Turn-parity enforcement: authoritative digests (no re-read invitation) +
  # hard block on redundant ranged re-reads of already-digested files, so side B
  # cannot spend extra turns/tokens that would push it above side A.
  export CONTEXTMESH_AUTHORITATIVE_DIGEST="1"
  export CONTEXTMESH_BLOCK_REREAD="1"
fi

cd "$DEMO/target"
export PWD="$DEMO/target"

TASK="$(cat "$ROOT/demo-prompt.txt")"
printf '%s\n' "$TASK" > "$ROOT/runs/demo-prompt.txt"

# The real opencode CLI. Must be v1.18.15+ (matches the pinned submodule).
OC="${CONTEXTMESH_OPENCODE_BIN:-opencode}"
command -v "$OC" >/dev/null || { echo "opencode binary '$OC' not found on PATH"; exit 1; }

if [ "$SIDE" = "seed" ]; then
  export CONTEXTMESH_SYNC_SUMMARIZE=1
  echo "seeding EverOS digests for the shared files (fast single-agent read)…"
  # A minimal read of exactly the shared files digests them once; no fan-out,
  # so the summarizer is called ~3 times and this returns in well under a minute.
  SEED_PROMPT='Read these three files in full, then reply with just the word DONE: src/models.js, src/utils.js, src/middleware.js'
  "$OC" run --format json --auto --agent build \
    --model "ollama/$CONTEXTMESH_MODEL" "$SEED_PROMPT" >/dev/null
  touch "$ROOT/runs/demo-tui/.seeded"
  echo "seed complete — side B will now hit digests from the first read."
  exit 0
fi

echo "=== ContextMesh demo TUI — side $ARM (session: $SESSION) ==="
echo "    opencode: $("$OC" --version 2>/dev/null)   agent: orchestrator"
[ "$ARM" = "b" ] && echo "    plugin: ContextMesh (EverOS digests, namespace cm-demo)"
echo "    meter: python3 $ROOT/scripts/live_meter.py   (third terminal)"
echo "    prompt: pbcopy < $ROOT/runs/demo-prompt.txt   then paste (⌘V) after selecting the orchestrator agent"
sleep 1
exec "$OC"
