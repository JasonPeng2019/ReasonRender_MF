#!/bin/bash
# Launch one native Codex side of the ContextMesh + ReasonRenderCoding demo.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"

if [ "${1:-}" = smoke ]; then
  [ "$#" -eq 7 ] && [ "$2" = --fixture ] && [ "$4" = --round-id ] && \
    [ "$6" = --timeout-ms ] || {
      echo "usage: rrd_demo_tui.sh smoke --fixture PATH --round-id ID --timeout-ms MS" >&2
      exit 2
    }
  PYTHON="$REPO/.venv/bin/python3"
  [ -x "$PYTHON" ] || { echo "reviewed project Python is unavailable" >&2; exit 1; }
  exec "$PYTHON" "$ROOT/scripts/rrcv2_product_guard.py" \
    --fixture "$3" --round-id "$5" --timeout-ms "$7"
fi

SIDE="${1:-}"
[[ "$SIDE" =~ ^(a|b|run-a|run-b|seed|reset)$ ]] || {
  echo "usage: rrd_demo_tui.sh <reset|seed|a|b|run-a|run-b>"
  exit 1
}
BACKEND="${RRD_MEMORY_BACKEND:-}"
case "$BACKEND" in everos|sqlite) ;; *) echo "invalid RRD_MEMORY_BACKEND" >&2; exit 2 ;; esac

RUNS="$ROOT/runs/rrd-demo"
ROUNDFILE="$RUNS/round-$BACKEND"
PUBLIC="$ROOT/RRDdemo-$([ "$BACKEND" = everos ] && printf everos || printf local).sh"
mkdir -p "$RUNS"

owner_scope() {
  python3 - "$1" <<'PY'
import hashlib,sys
print("cm-"+hashlib.sha256(sys.argv[1].encode()).hexdigest()[:32])
PY
}

CODEX_CANDIDATE="${RRD_CODEX_BIN:-$(command -v codex || true)}"
[ -n "$CODEX_CANDIDATE" ] || { echo "Codex is not installed" >&2; exit 1; }
CODEX_BIN="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CODEX_CANDIDATE")"
MODEL="${RRD_CODEX_MODEL:-gpt-5.5}"
ROOT_REASONING="${RRD_CODEX_REASONING:-medium}"
WORKER_MODEL="${RRD_WORKER_MODEL:-gpt-5.6-luna}"
WORKER_REASONING="${RRD_WORKER_REASONING:-low}"
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
    --codex-bin "$CODEX_BIN" --model "$MODEL" --worker-model "$WORKER_MODEL" \
    --worker-reasoning "$WORKER_REASONING" >/dev/null
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
  if [ "$BACKEND" = everos ]; then
    OWNER_SCOPE="$(owner_scope "$ROUND")"
    REVISION="$(/usr/bin/git -C "$REPO/EverOS" rev-parse HEAD)"
    DATA_ROOT="$ROOT/everos-native-root/$REVISION"
    python3 - "$ROUND_DIR/everos-target.json" "$ROUND" "$OWNER_SCOPE" "$DATA_ROOT" <<'PY'
import hashlib,json,os,sys
from pathlib import Path
path=Path(sys.argv[1]); round_id=sys.argv[2]; owner=sys.argv[3]; data_root=sys.argv[4]
seed=json.dumps({"owner_scope":owner,"round_id":round_id,"v":1},sort_keys=True,separators=(",",":")).encode()
digest=hashlib.sha256(seed).hexdigest()
value={
  "app_id":"default",
  "base_url":"http://127.0.0.1:8000/api/v2/memory",
  "health_url":"http://127.0.0.1:8000/health",
  "isolation":{
    "data_root_sha256":hashlib.sha256(os.fsencode(os.path.realpath(data_root))).hexdigest(),
    "instance_id":"cm-"+digest[:32],
    "mode":"dedicated_empty_instance_v1",
    "owner_scope":owner,
  },
  "min_score":{"denominator":20,"numerator":7},
  "project_id":"default",
  "protocol":"external_ref_passthrough_v1",
  "readiness":{"consecutive_zeroes":2,"max_wait_ms":30000,"mode":"cascade_pending_two_zero_v1","poll_interval_ms":250},
  "search_method":"hybrid",
  "top_k":3,
  "track":"episodes",
  "user_id":"rrc",
  "v":1,
  "workload_session_id":"rrcv2-cm-"+digest[:32],
}
path.write_text(json.dumps(value,sort_keys=True,separators=(",",":")),encoding="utf-8")
os.chmod(path,0o600)
PY
  fi
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
export RRD_CODEX_REASONING="$ROOT_REASONING"
export RRD_WORKER_MODEL="$WORKER_MODEL"
export RRD_WORKER_REASONING="$WORKER_REASONING"
export RRD_ENABLE_RRC="${RRD_ENABLE_RRC:-1}"
export RRD_ENABLE_CONTEXTMESH="${RRD_ENABLE_CONTEXTMESH:-1}"
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
export RRC_DEMO_DATABASE="$ROUND_DIR/rrcv2.sqlite3"
export RRC_DEMO_LOCK="$DEMO/plan-spec.lock"
export RRC_DEMO_EVENTS="$DEMO/rrc-events.jsonl"
export RRC_DEMO_MODEL_EVENTS="$DEMO/rrc-model-events.jsonl"
export RRC_STRONG_MODEL="$MODEL"
export RRC_REQUIRE_EFFECTIVE_MODEL=1
export RRC_PLANNER_TIMEOUT="${RRC_PLANNER_TIMEOUT:-90}"
export RRC_LOCK_TIMEOUT="${RRC_LOCK_TIMEOUT:-120}"
export RRC_VISIBILITY_TIMEOUT="${RRC_VISIBILITY_TIMEOUT:-10}"
export RRC_BRIDGE_TIMEOUT="${RRC_BRIDGE_TIMEOUT:-240}"
export RRCV2_ATTEMPTS_ROOT="$ROUND_DIR/rrcv2-attempts"
RRCV2_OWNER_SCOPE="$(owner_scope "$ROUND")"
export RRCV2_OWNER_SCOPE
export RRCV2_ROUTE_ID=rrcv2-coding-v1
export RRCV2_CELL_ID="rrcv2-$ROUND-$ARM"
export RRCV2_CELL_AUTHORITY_ROOT="$ROUND_DIR/rrcv2-cell-authority"
RRCV2_ROOT_SENTINEL="rrcv2-root-$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
RRCV2_PARENT_HISTORY_SENTINEL="rrcv2-parent-$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
export RRCV2_ROOT_SENTINEL RRCV2_PARENT_HISTORY_SENTINEL
if [ "$BACKEND" = everos ]; then
  export RRCV2_EVEROS_TARGET="$ROUND_DIR/everos-target.json"
else
  unset RRCV2_EVEROS_TARGET
fi

PROMPT="$DEMO/RRCv2-demo-prompt.txt"
RRCV2_PRODUCT_TASK_ENVELOPE="$DEMO/task-envelope.v1.json"
export RRCV2_PRODUCT_TASK_ENVELOPE
"$RRC_DEMO_UV_BIN" run --project "$REPO/pyproject.toml" python \
  "$ROOT/scripts/rrcv2_demo_prompt.py" --repository "$REPO" --target "$DEMO/target" \
  --mode "$MODE" --root-sentinel "$RRCV2_ROOT_SENTINEL" \
  --parent-history-sentinel "$RRCV2_PARENT_HISTORY_SENTINEL" \
  --uv-bin "$RRC_DEMO_UV_BIN" --output "$PROMPT" \
  --task-envelope-output "$RRCV2_PRODUCT_TASK_ENVELOPE"
RRCV2_ROOT_PROMPT_SHA256="$(/usr/bin/shasum -a 256 "$PROMPT" | /usr/bin/awk '{print $1}')"
export RRCV2_ROOT_PROMPT_SHA256

"$RRC_DEMO_UV_BIN" run --locked --project "$REPO/pyproject.toml" python \
  "$ROOT/scripts/rrcv2_product_cell.py" --repository "$REPO" \
  --database "$RRC_DEMO_DATABASE" --authority-root "$RRCV2_CELL_AUTHORITY_ROOT" \
  --task-envelope "$RRCV2_PRODUCT_TASK_ENVELOPE" --cell-id "$RRCV2_CELL_ID" \
  --run-id "$ROUND" --arm "rrc_$MODE" --session-id "root-launch-$ROUND-$ARM" \
  >"$DEMO/root-permit.json"

cp "$PROMPT" "$ROOT/runs/RRD-demo-prompt.txt"
MODE_LABEL="$(printf '%s' "$MODE" | tr '[:lower:]' '[:upper:]')"
echo "=== native Codex combined demo — side $ARM ($MODE_LABEL) ==="
echo "client: $($CODEX_BIN --version) · root/planner: $MODEL · memory: $BACKEND"
echo "workers: one source-blind native v1 Codex worker · $WORKER_MODEL/$WORKER_REASONING"
echo "ContextMesh: sealed source-to-SPEC once; source-blind native workers; receipt-only wait results"
echo "ReasonRenderCoding: canonical $MODE SPEC/IMPLEMENT/VERIFY/repair/fallback pipeline"
echo "prompt: $PROMPT"
cd "$DEMO/target"
ENVIRONMENT=(
  "HOME=$HOME" "PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  "LANG=${LANG:-en_US.UTF-8}" "LC_ALL=${LC_ALL:-en_US.UTF-8}" "TERM=${TERM:-xterm-256color}"
  "CODEX_HOME=$CODEX_HOME" "RRD_CODEX_BIN=$RRD_CODEX_BIN" "RRD_CODEX_MODEL=$RRD_CODEX_MODEL"
  "RRD_CODEX_REASONING=$RRD_CODEX_REASONING"
  "RRD_WORKER_MODEL=$RRD_WORKER_MODEL" "RRD_WORKER_REASONING=$RRD_WORKER_REASONING"
  "RRD_ENABLE_RRC=$RRD_ENABLE_RRC" "RRD_ENABLE_CONTEXTMESH=$RRD_ENABLE_CONTEXTMESH"
  "RRD_EXTERNAL_SANDBOX=$RRD_EXTERNAL_SANDBOX"
  "RRD_REPO_ROOT=$RRD_REPO_ROOT" "RRD_MEMORY_BACKEND=$RRD_MEMORY_BACKEND"
  "RRD_SUMMARY_MODE=$RRD_SUMMARY_MODE" "RRD_TARGET_ROOT=$RRD_TARGET_ROOT"
  "RRD_SEED_MANIFEST=$RRD_SEED_MANIFEST" "RRD_HOOK_EVENTS=$RRD_HOOK_EVENTS"
  "RRD_RAW_RESULTS=$RRD_RAW_RESULTS" "RRC_PLANNER_CODEX_HOME=$RRC_PLANNER_CODEX_HOME"
  "RRC_DEMO_UV_BIN=$RRC_DEMO_UV_BIN" "RRC_DEMO_ROUND=$RRC_DEMO_ROUND"
  "RRC_DEMO_MODE=$RRC_DEMO_MODE" "RRC_DEMO_DATABASE=$RRC_DEMO_DATABASE"
  "RRC_DEMO_LOCK=$RRC_DEMO_LOCK" "RRC_DEMO_EVENTS=$RRC_DEMO_EVENTS"
  "RRC_DEMO_MODEL_EVENTS=$RRC_DEMO_MODEL_EVENTS" "RRC_STRONG_MODEL=$RRC_STRONG_MODEL"
  "RRC_REQUIRE_EFFECTIVE_MODEL=$RRC_REQUIRE_EFFECTIVE_MODEL"
  "RRC_PLANNER_TIMEOUT=$RRC_PLANNER_TIMEOUT" "RRC_LOCK_TIMEOUT=$RRC_LOCK_TIMEOUT"
  "RRC_VISIBILITY_TIMEOUT=$RRC_VISIBILITY_TIMEOUT" "RRC_BRIDGE_TIMEOUT=$RRC_BRIDGE_TIMEOUT"
  "RRC_EVEROS_URL=${RRC_EVEROS_URL:-}" "RRC_CONTROL=${RRC_CONTROL:-}"
  "RRCV2_ATTEMPTS_ROOT=$RRCV2_ATTEMPTS_ROOT" "RRCV2_OWNER_SCOPE=$RRCV2_OWNER_SCOPE"
  "RRCV2_ROUTE_ID=$RRCV2_ROUTE_ID" "RRCV2_CELL_ID=$RRCV2_CELL_ID"
  "RRCV2_CELL_AUTHORITY_ROOT=$RRCV2_CELL_AUTHORITY_ROOT"
  "RRCV2_PRODUCT_TASK_ENVELOPE=$RRCV2_PRODUCT_TASK_ENVELOPE"
  "RRCV2_ROOT_PROMPT_SHA256=$RRCV2_ROOT_PROMPT_SHA256"
  "RRCV2_ROOT_SENTINEL=$RRCV2_ROOT_SENTINEL"
  "RRCV2_PARENT_HISTORY_SENTINEL=$RRCV2_PARENT_HISTORY_SENTINEL"
  "RRCV2_EVEROS_TARGET=${RRCV2_EVEROS_TARGET:-}"
)
FINISHER_READY="$DEMO/finisher.ready"
rm -f "$FINISHER_READY"
cd "$REPO"
FINISHER_ARGS=(run python "$ROOT/scripts/rrc_finisher.py" \
  --database "$RRC_DEMO_DATABASE" --owner-scope "$RRCV2_OWNER_SCOPE" \
  --codex-bin "$RRD_CODEX_BIN" \
  --strong-model "$RRC_STRONG_MODEL" --small-model gpt-5.6-luna \
  --memory-backend "$RRD_MEMORY_BACKEND" \
  --ready-file "$FINISHER_READY")
if [ -n "${RRCV2_EVEROS_TARGET:-}" ]; then
  FINISHER_ARGS+=(--everos-target "$RRCV2_EVEROS_TARGET")
fi
"$RRC_DEMO_UV_BIN" "${FINISHER_ARGS[@]}" >"$DEMO/finisher.log" 2>&1 &
FINISHER_PID=$!
cleanup_finisher() {
  kill -TERM "$FINISHER_PID" 2>/dev/null || true
  for _ in $(seq 1 100); do
    kill -0 "$FINISHER_PID" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$FINISHER_PID" 2>/dev/null; then kill -KILL "$FINISHER_PID" 2>/dev/null || true; fi
  wait "$FINISHER_PID" 2>/dev/null || true
}
trap cleanup_finisher EXIT INT TERM HUP
for _ in $(seq 1 100); do
  [ -f "$FINISHER_READY" ] && break
  kill -0 "$FINISHER_PID" 2>/dev/null || { cat "$DEMO/finisher.log" >&2; exit 1; }
  sleep 0.1
done
[ -f "$FINISHER_READY" ] || { echo "RRCv2 finisher did not become ready" >&2; exit 1; }
cd "$DEMO/target"
if [ "$HEADLESS" = 1 ]; then
  /usr/bin/env -i "${ENVIRONMENT[@]}" \
    "${CODEX_COMMAND[@]}" --strict-config --dangerously-bypass-hook-trust \
    exec --json \
    --skip-git-repo-check "$(cat "$PROMPT")" \
    | /usr/bin/tee "$DEMO/root-events.jsonl"
  exit "${PIPESTATUS[0]}"
fi
/usr/bin/env -i "${ENVIRONMENT[@]}" \
  "${CODEX_COMMAND[@]}" --strict-config --dangerously-bypass-hook-trust "$(cat "$PROMPT")"
