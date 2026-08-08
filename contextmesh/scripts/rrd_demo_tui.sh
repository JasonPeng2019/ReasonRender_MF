#!/usr/bin/env bash
# Launch one Codex side of the combined ContextMesh + ReasonRenderCoding demo.
set -euo pipefail
umask 077

SIDE="${1:-}"
[[ "$SIDE" =~ ^(a|b|seed|reset)$ ]] || { echo "usage: rrd_demo_tui.sh <reset|seed|a|b>"; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.env.local"

RUNS="$ROOT/runs/rrd-demo"
ROUNDFILE="$RUNS/round"
mkdir -p "$RUNS"
CODEX_BIN="${RRD_CODEX_BIN:-codex}"
MODEL="${CONTEXTMESH_MODEL:?CONTEXTMESH_MODEL is required}"
RRC_MODEL_VALUE="${RRC_STRONG_MODEL:-${RRC_MODEL:-$MODEL}}"

write_home() {
  local home="$1" session="$2" agents_enabled="$3" hooks_enabled="$4" hook_path="$5" port="$6"
  mkdir -p "$home"
  python3 - "$home/config.toml" "$MODEL" "$session" "$agents_enabled" "$hooks_enabled" "$port" <<'PY'
import json,sys
from pathlib import Path
path,model,session,agents,hooks,port=sys.argv[1:]
q=json.dumps
text=f'''model = {q(model)}
model_provider = "ollama_rrd"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
disable_response_storage = true

[analytics]
enabled = false

[feedback]
enabled = false

[model_providers.ollama_rrd]
name = "Ollama through RRD Tollgate"
base_url = {q(f"http://127.0.0.1:{port}/ollama/{session}/v1")}
env_key = "OLLAMA_API_KEY"
wire_api = "responses"
request_max_retries = 1
stream_max_retries = 1
stream_idle_timeout_ms = 120000
supports_websockets = false

[agents]
enabled = {agents}
max_concurrent_threads_per_session = 4
'''
if agents == "true":
    text += '''
[agents.worker]
description = "Audit exactly one HTTP handler using the supplied RRC and ContextMesh context."
'''
text += f'''
[features]
hooks = {hooks}
multi_agent = {agents}
multi_agent_v2 = false
plugins = false
'''
Path(path).write_text(text)
PY
  if [ "$hooks_enabled" = true ]; then
    python3 - "$home/hooks.json" "$hook_path" <<'PY'
import json,shlex,sys
from pathlib import Path
path,hook=sys.argv[1:]
command="python3 "+shlex.quote(hook)
def handler(timeout): return {"type":"command","command":command,"timeout":timeout}
value={"description":"sealed RRD Codex hooks","hooks":{
 "PreToolUse":[{"hooks":[handler(70)]}],
 "PostToolUse":[{"hooks":[handler(15)]}],
 "SubagentStart":[{"hooks":[handler(15)],"matcher":"worker"}],
 "SubagentStop":[{"hooks":[handler(120)],"matcher":"worker"}],
 "Stop":[{"hooks":[handler(10)]}],
}}
Path(path).write_text(json.dumps(value,separators=(",",":"))+"\n")
PY
  fi
}

make_target() {
  local demo="$1"
  if [ ! -d "$demo/target/.git" ]; then
    cp -R "$ROOT/bench/target-template" "$demo/target"
    git -C "$demo/target" init -q
    git -C "$demo/target" add -A
    git -C "$demo/target" -c user.email=demo@reasonrendercoding -c user.name=demo \
      commit -qm "combined Codex demo workspace"
  fi
}

if [ "$SIDE" = reset ]; then
  ROUND="rrd-$(date +%s)-$$"
  ROUND_DIR="$RUNS/$ROUND"
  mkdir -p "$ROUND_DIR/bundle"
  cp "$ROOT/scripts/rrd_codex_hook.py" "$ROUND_DIR/bundle/rrd_codex_hook.py"
  chmod 700 "$ROUND_DIR/bundle/rrd_codex_hook.py"
  shasum -a 256 "$ROUND_DIR/bundle/rrd_codex_hook.py" | awk '{print $1}' \
    > "$ROUND_DIR/bundle.sha256"
  for arm in a b; do
    demo="$ROUND_DIR/$arm"
    mkdir -p "$demo"
    make_target "$demo"
    write_home "$demo/codex-home" "rrd-demo-$ROUND-$arm-outer" true true \
      "$ROUND_DIR/bundle/rrd_codex_hook.py" 8790
    write_home "$demo/summarizer-home" "rrd-demo-$ROUND-$arm-summarizer" false false "" 8789
    write_home "$demo/seed-home" "rrd-demo-$ROUND-setup-seed-$arm" false false "" 8789
    # Planner traffic has its own session and no demo lifecycle hooks.
    mkdir -p "$demo/planner-home"
    cp "$demo/summarizer-home/config.toml" "$demo/planner-home/config.toml"
    sed "s/$arm-summarizer/$arm-planner/g" "$demo/planner-home/config.toml" \
      > "$demo/planner-home/config.toml.tmp"
    mv "$demo/planner-home/config.toml.tmp" "$demo/planner-home/config.toml"
  done
  touch "$ROUND_DIR/.combined-codex-v1"
  printf '%s\n' "$ROUND" > "$ROUNDFILE"
  echo "new Codex + ContextMesh + ReasonRenderCoding round: $ROUND"
  echo "next: RRDdemo.sh seed, then RRDdemo.sh a / b / meter"
  exit 0
fi

[ -s "$ROUNDFILE" ] || { echo "No combined round. Run: $ROOT/RRDdemo.sh prep" >&2; exit 1; }
ROUND="$(cat "$ROUNDFILE")"
ROUND_DIR="$RUNS/$ROUND"
[ -f "$ROUND_DIR/.combined-codex-v1" ] || {
  echo "Round $ROUND is not a Codex combined-demo round. Run: $ROOT/RRDdemo.sh prep" >&2
  exit 1
}
expected_bundle="$(cat "$ROUND_DIR/bundle.sha256")"
actual_bundle="$(shasum -a 256 "$ROUND_DIR/bundle/rrd_codex_hook.py" | awk '{print $1}')"
[ "$expected_bundle" = "$actual_bundle" ] || { echo "sealed hook bundle changed; run prep" >&2; exit 1; }

require_health() {
  local name="$1" url="$2"
  curl -sf -m 2 "$url" >/dev/null 2>&1 || { echo "$name is not reachable: $url" >&2; exit 1; }
}
require_health "RRD Tollgate" "http://127.0.0.1:8789/healthz"
require_health "RRD response proxy" "http://127.0.0.1:8790/healthz"
require_health "EverOS" "http://127.0.0.1:8000/health"
command -v "$CODEX_BIN" >/dev/null || { echo "codex binary '$CODEX_BIN' not found" >&2; exit 1; }
command -v "${RRC_DEMO_UV_BIN:-uv}" >/dev/null || { echo "uv is not installed" >&2; exit 1; }

export OLLAMA_API_KEY
export RRD_CODEX_BIN="$CODEX_BIN"
export RRD_REPO_ROOT="$REPO"
export RRC_EVEROS_URL="${RRC_EVEROS_URL:-http://127.0.0.1:8000}"
export RRC_DEMO_UV_BIN="${RRC_DEMO_UV_BIN:-uv}"

seed_arm() {
  local arm="$1"
  local demo="$ROUND_DIR/$arm"
  export RRD_TARGET_ROOT="$demo/target"
  export RRD_SUMMARIZER_CODEX_HOME="$demo/seed-home"
  export RRD_SEED_MANIFEST="$demo/seed-manifest.json"
  python3 "$ROUND_DIR/bundle/rrd_codex_hook.py" seed \
    --round-id "$ROUND" --arm "$arm" --target-root "$demo/target" \
    --manifest "$demo/seed-manifest.json"
}

if [ "$SIDE" = seed ]; then
  echo "seeding authenticated ContextMesh digests for both Codex arms…"
  seed_arm a
  seed_arm b
  touch "$ROUND_DIR/.seeded"
  echo "seed complete — both arms have sealed, current shared-file digests"
  exit 0
fi

[ -f "$ROUND_DIR/.seeded" ] || { echo "Round is not seeded. Run: $ROOT/RRDdemo.sh seed" >&2; exit 1; }
ARM="$SIDE"
MODE=cold
[ "$ARM" = b ] && MODE=warm
DEMO="$ROUND_DIR/$ARM"
SESSION="rrd-demo-$ROUND-$ARM"
MODE_LABEL="$(printf '%s' "$MODE" | tr '[:lower:]' '[:upper:]')"

export CODEX_HOME="$DEMO/codex-home"
export RRD_TARGET_ROOT="$DEMO/target"
export RRD_SEED_MANIFEST="$DEMO/seed-manifest.json"
export RRD_HOOK_EVENTS="$DEMO/hook-events.jsonl"
export RRD_RAW_RESULTS="$DEMO/raw-results"
export RRD_SUMMARIZER_CODEX_HOME="$DEMO/summarizer-home"
export RRC_PLANNER_CODEX_HOME="$DEMO/planner-home"
export RRC_DEMO_ROUND="$ROUND"
export RRC_DEMO_MODE="$MODE"
export RRC_DEMO_DATABASE="$DEMO/plan-spec.sqlite"
export RRC_DEMO_LOCK="$DEMO/plan-spec.lock"
export RRC_DEMO_EVENTS="$DEMO/rrc-events.jsonl"
export RRC_DEMO_MODEL_EVENTS="$DEMO/rrc-model-events.jsonl"
export RRC_STRONG_MODEL="$RRC_MODEL_VALUE"
export RRC_PLANNER_TIMEOUT="${RRC_PLANNER_TIMEOUT:-30}"
export RRC_LOCK_TIMEOUT="${RRC_LOCK_TIMEOUT:-50}"
export RRC_VISIBILITY_TIMEOUT="${RRC_VISIBILITY_TIMEOUT:-10}"
export RRC_BRIDGE_TIMEOUT="${RRC_BRIDGE_TIMEOUT:-60}"
export RRD_SUMMARIZER_TIMEOUT="${RRD_SUMMARIZER_TIMEOUT:-90}"

cp "$ROOT/RRD-demo-prompt.txt" "$ROOT/runs/RRD-demo-prompt.txt"
cd "$DEMO/target"
echo "=== ContextMesh + ReasonRenderCoding demo TUI — side $ARM ($MODE_LABEL, session: $SESSION) ==="
echo "    client: Codex $($CODEX_BIN --version 2>/dev/null)"
echo "    provider: Ollama Responses through fail-open adapter :8790 → Tollgate :8789"
echo "    credential: inherited from OLLAMA_API_KEY (never written to generated config)"
echo "    workers: four native Codex worker subagents; multi_agent_v2=false (tested v1 hooks)"
echo "    ContextMesh: authenticated shared digests + bounded result compression"
echo "    RRC: $MODE packet mode; planner=$RRC_MODEL_VALUE (bounded, fail-open)"
echo "    meter: $ROOT/RRDdemo.sh meter   (third terminal)"
echo "    prompt: pbcopy < $ROOT/runs/RRD-demo-prompt.txt   then paste (⌘V)"
sleep 1
exec "$CODEX_BIN" --dangerously-bypass-hook-trust
