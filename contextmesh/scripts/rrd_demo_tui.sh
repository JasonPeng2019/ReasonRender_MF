#!/bin/bash
# Launch one native Codex side of the ContextMesh + ReasonRenderCoding demo.
set -euo pipefail
umask 077

SIDE="${1:-}"
[[ "$SIDE" =~ ^(a|b|run-a|run-b|seed|reset)$ ]] || {
  echo "usage: rrd_demo_tui.sh <reset|seed|a|b|run-a|run-b>"
  exit 1
}
BACKEND="${RRD_MEMORY_BACKEND:-}"
case "$BACKEND" in everos|sqlite) ;; *) echo "invalid RRD_MEMORY_BACKEND" >&2; exit 2 ;; esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
RUNS="$ROOT/runs/rrd-demo"
ROUNDFILE="$RUNS/round-$BACKEND"
PUBLIC="$ROOT/RRDdemo-$([ "$BACKEND" = everos ] && printf everos || printf local).sh"
mkdir -p "$RUNS"

CODEX_CANDIDATE="${RRD_CODEX_BIN:-$(command -v codex || true)}"
[ -n "$CODEX_CANDIDATE" ] || { echo "Codex is not installed" >&2; exit 1; }
CODEX_BIN="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CODEX_CANDIDATE")"
MODEL="${RRD_CODEX_MODEL:-gpt-5.5}"
NATIVE_HOME="$ROOT/.codex-rrd-native"
CODEX_COMMAND=("$CODEX_BIN")
if [ "$(uname -s)" = Darwin ]; then
  [ -x /usr/bin/sandbox-exec ] || { echo "macOS sandbox-exec is unavailable" >&2; exit 1; }
  CODEX_COMMAND=(/usr/bin/sandbox-exec -f "$NATIVE_HOME/credential-deny.sb" \
    "$CODEX_BIN" --dangerously-bypass-approvals-and-sandbox)
fi

make_target() {
  local demo="$1"
  if [ ! -d "$demo/target/.git" ]; then
    cp -R "$ROOT/bench/target-template" "$demo/target"
    /usr/bin/git -C "$demo/target" init -q
    /usr/bin/git -C "$demo/target" add -A
    /usr/bin/git -C "$demo/target" -c user.email=demo@reasonrendercoding -c user.name=demo \
      commit -qm "native combined demo workspace"
  fi
}

if [ "$SIDE" = reset ]; then
  python3 "$ROOT/scripts/rrd_native_config.py" init --root "$ROOT" \
    --codex-bin "$CODEX_BIN" --model "$MODEL" >/dev/null
  ROUND="rrd-$BACKEND-$(date +%s)-$$"
  ROUND_DIR="$RUNS/$ROUND"
  mkdir -p "$ROUND_DIR"
  for arm in a b; do
    mkdir -p "$ROUND_DIR/$arm"
    make_target "$ROUND_DIR/$arm"
  done
  python3 - "$ROUND_DIR/round-meta.json" "$ROUND" "$BACKEND" "$MODEL" <<'PY'
import json,os,sys
from pathlib import Path
path=Path(sys.argv[1])
path.write_text(json.dumps({"v":2,"round_id":sys.argv[2],"memory_backend":sys.argv[3],"provider":"native-codex","model":sys.argv[4]},separators=(",",":"))+"\n")
os.chmod(path,0o600)
PY
  printf '%s\n' "$ROUND" >"$ROUNDFILE"
  echo "new native Codex + ContextMesh + ReasonRenderCoding round: $ROUND ($BACKEND memory)"
  exit 0
fi

[ -s "$ROUNDFILE" ] || { echo "No round. Run: $PUBLIC prep" >&2; exit 1; }
ROUND="$(cat "$ROUNDFILE")"
ROUND_DIR="$RUNS/$ROUND"
[[ "$ROUND" == "rrd-$BACKEND-"* ]] || { echo "round backend mismatch; run prep" >&2; exit 1; }

export RRD_CODEX_BIN="$CODEX_BIN"
export RRD_CODEX_MODEL="$MODEL"
if [ "$(uname -s)" = Darwin ]; then
  RRD_EXTERNAL_SANDBOX=1
else
  RRD_EXTERNAL_SANDBOX=0
fi
export RRD_EXTERNAL_SANDBOX
export CODEX_HOME="$NATIVE_HOME"
export RRD_REPO_ROOT="$REPO"
export RRD_MEMORY_BACKEND="$BACKEND"
export RRD_SUMMARY_MODE=deterministic
export RRC_DEMO_UV_BIN="${RRC_DEMO_UV_BIN:-$(command -v uv)}"
if [ "$BACKEND" = everos ]; then export RRC_EVEROS_URL="http://127.0.0.1:8000"; else unset RRC_EVEROS_URL; fi

seed_arm() {
  local arm="$1" demo="$ROUND_DIR/$1"
  export RRD_TARGET_ROOT="$demo/target"
  export RRD_SEED_MANIFEST="$demo/seed-manifest.json"
  export RRD_HOOK_EVENTS="$demo/hook-events.jsonl"
  python3 "$ROOT/scripts/rrd_codex_hook.py" seed --round-id "$ROUND" --arm "$arm" \
    --target-root "$demo/target" --manifest "$demo/seed-manifest.json" \
    --memory-backend "$BACKEND"
}

if [ "$SIDE" = seed ]; then
  seed_arm a
  seed_arm b
  touch "$ROUND_DIR/.seeded"
  echo "seed complete — current shared-file digests are sealed for both arms"
  exit 0
fi

[ -f "$ROUND_DIR/.seeded" ] || { echo "round is not seeded; run prep" >&2; exit 1; }
HEADLESS=0
case "$SIDE" in
  run-a) ARM=a; HEADLESS=1 ;;
  run-b) ARM=b; HEADLESS=1 ;;
  *) ARM="$SIDE" ;;
esac
MODE=cold; [ "$ARM" = b ] && MODE=warm
DEMO="$ROUND_DIR/$ARM"
export RRD_TARGET_ROOT="$DEMO/target"
export RRD_SEED_MANIFEST="$DEMO/seed-manifest.json"
export RRD_HOOK_EVENTS="$DEMO/hook-events.jsonl"
export RRD_RAW_RESULTS="$DEMO/raw-results"
export RRC_PLANNER_CODEX_HOME="$NATIVE_HOME"
export RRC_DEMO_ROUND="$ROUND"
export RRC_DEMO_MODE="$MODE"
export RRC_DEMO_DATABASE="$DEMO/plan-spec.sqlite"
export RRC_DEMO_LOCK="$DEMO/plan-spec.lock"
export RRC_DEMO_EVENTS="$DEMO/rrc-events.jsonl"
export RRC_DEMO_MODEL_EVENTS="$DEMO/rrc-model-events.jsonl"
export RRC_STRONG_MODEL="$MODEL"
export RRC_PLANNER_TIMEOUT="${RRC_PLANNER_TIMEOUT:-90}"
export RRC_LOCK_TIMEOUT="${RRC_LOCK_TIMEOUT:-120}"
export RRC_VISIBILITY_TIMEOUT="${RRC_VISIBILITY_TIMEOUT:-10}"
export RRC_BRIDGE_TIMEOUT="${RRC_BRIDGE_TIMEOUT:-240}"

cp "$ROOT/RRD-demo-prompt.txt" "$ROOT/runs/RRD-demo-prompt.txt"
MODE_LABEL="$(printf '%s' "$MODE" | tr '[:lower:]' '[:upper:]')"
echo "=== native Codex combined demo — side $ARM ($MODE_LABEL) ==="
echo "client: $($CODEX_BIN --version) · model: $MODEL · memory: $BACKEND"
echo "workers: four native v1 Codex workers in parallel"
echo "ContextMesh: sealed shared digests + bounded wait-result compression"
echo "ReasonRenderCoding: $MODE packet pipeline"
echo "prompt: $ROOT/runs/RRD-demo-prompt.txt"
cd "$DEMO/target"
ENVIRONMENT=(
  "HOME=$HOME" "PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  "LANG=${LANG:-en_US.UTF-8}" "LC_ALL=${LC_ALL:-en_US.UTF-8}" "TERM=${TERM:-xterm-256color}"
  "CODEX_HOME=$CODEX_HOME" "RRD_CODEX_BIN=$RRD_CODEX_BIN" "RRD_CODEX_MODEL=$RRD_CODEX_MODEL"
  "RRD_EXTERNAL_SANDBOX=$RRD_EXTERNAL_SANDBOX"
  "RRD_REPO_ROOT=$RRD_REPO_ROOT" "RRD_MEMORY_BACKEND=$RRD_MEMORY_BACKEND"
  "RRD_SUMMARY_MODE=$RRD_SUMMARY_MODE" "RRD_TARGET_ROOT=$RRD_TARGET_ROOT"
  "RRD_SEED_MANIFEST=$RRD_SEED_MANIFEST" "RRD_HOOK_EVENTS=$RRD_HOOK_EVENTS"
  "RRD_RAW_RESULTS=$RRD_RAW_RESULTS" "RRC_PLANNER_CODEX_HOME=$RRC_PLANNER_CODEX_HOME"
  "RRC_DEMO_UV_BIN=$RRC_DEMO_UV_BIN" "RRC_DEMO_ROUND=$RRC_DEMO_ROUND"
  "RRC_DEMO_MODE=$RRC_DEMO_MODE" "RRC_DEMO_DATABASE=$RRC_DEMO_DATABASE"
  "RRC_DEMO_LOCK=$RRC_DEMO_LOCK" "RRC_DEMO_EVENTS=$RRC_DEMO_EVENTS"
  "RRC_DEMO_MODEL_EVENTS=$RRC_DEMO_MODEL_EVENTS" "RRC_STRONG_MODEL=$RRC_STRONG_MODEL"
  "RRC_PLANNER_TIMEOUT=$RRC_PLANNER_TIMEOUT" "RRC_LOCK_TIMEOUT=$RRC_LOCK_TIMEOUT"
  "RRC_VISIBILITY_TIMEOUT=$RRC_VISIBILITY_TIMEOUT" "RRC_BRIDGE_TIMEOUT=$RRC_BRIDGE_TIMEOUT"
  "RRC_EVEROS_URL=${RRC_EVEROS_URL:-}" "RRC_CONTROL=${RRC_CONTROL:-}"
)
if [ "$HEADLESS" = 1 ]; then
  /usr/bin/env -i "${ENVIRONMENT[@]}" \
    "${CODEX_COMMAND[@]}" --strict-config --dangerously-bypass-hook-trust \
    exec --json \
    --skip-git-repo-check "$(cat "$ROOT/RRD-demo-prompt.txt")" \
    | /usr/bin/tee "$DEMO/root-events.jsonl"
  exit "${PIPESTATUS[0]}"
fi
exec /usr/bin/env -i "${ENVIRONMENT[@]}" \
  "${CODEX_COMMAND[@]}" --strict-config --dangerously-bypass-hook-trust
