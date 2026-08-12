#!/bin/bash
# Shared dispatcher for the ContextMesh + ReasonRenderCoding Codex demos.
#
# Public entry points:
#   RRDdemo-everos.sh prep|a|b|meter|down  (EverOS memory)
#   RRDdemo-local.sh  prep|a|b|meter|down  (local SQLite/files only)
#
# `a`: open the COLD Codex TUI; `b`: open the WARM Codex TUI; `meter` belongs
# in the third terminal. Each side generates and submits one canonical coding
# assignment; each root launches one source-blind native worker subagent.
set -euo pipefail

BACKEND="${RRD_MEMORY_BACKEND:-}"
case "$BACKEND" in
  everos|sqlite) ;;
  *)
    echo "RRD_MEMORY_BACKEND must be 'everos' or 'sqlite' (public launchers set it)." >&2
    exit 2
    ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNS="$ROOT/runs/rrd-demo"
ROUND_FILE="$RUNS/round-$BACKEND"
PUBLIC="$ROOT/RRDdemo-$([ "$BACKEND" = everos ] && printf everos || printf local).sh"
cmd="${1:-help}"

load_round() {
  if [ ! -s "$ROUND_FILE" ]; then
    echo "No $BACKEND demo round exists. Run: $PUBLIC prep" >&2
    exit 1
  fi
  ROUND="$(cat "$ROUND_FILE")"
  ROUND_DIR="$RUNS/$ROUND"
}

case "$cmd" in
  up)    exec "$ROOT/scripts/rrd_start_stack.sh" ;;
  down)  exec "$ROOT/scripts/rrd_stop_stack.sh" ;;
  prep)
    "$ROOT/scripts/rrd_start_stack.sh"
    "$ROOT/scripts/rrd_demo_preflight.sh"
    "$ROOT/scripts/rrd_demo_tui.sh" reset
    "$ROOT/scripts/rrd_demo_tui.sh" seed
    load_round
    mkdir -p "$ROUND_DIR"
    echo "round prepared; each a/b launcher generates and submits its bound RRCv2 prompt"
    ;;
  login)
    candidate="${RRD_CODEX_BIN:-$(command -v codex || true)}"
    [ -n "$candidate" ] || { echo "Codex is not installed" >&2; exit 1; }
    candidate="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$candidate")"
    exec python3 "$ROOT/scripts/rrd_native_config.py" login-command --root "$ROOT" \
      --codex-bin "$candidate"
    ;;
  a|b|run-a|run-b)
    "$ROOT/scripts/rrd_start_stack.sh"
    exec "$ROOT/scripts/rrd_demo_tui.sh" "$cmd"
    ;;
  canary)
    "$ROOT/scripts/rrd_start_stack.sh"
    exec "$ROOT/scripts/rrd_demo_preflight.sh" --canary
    ;;
  seed)    exec "$ROOT/scripts/rrd_demo_tui.sh" seed ;;
  reset)   exec "$ROOT/scripts/rrd_demo_tui.sh" reset ;;
  meter)
    load_round
    exec python3 "$ROOT/scripts/rrd_combined_meter.py" \
      --round "$ROUND" --memory-backend "$BACKEND" --watch
    ;;
  prompt)
    load_round
    latest="$ROUND_DIR/a/RRCv2-demo-prompt.txt"
    [ -s "$latest" ] || { echo "Start side a or b to generate its bound prompt." >&2; exit 1; }
    cat "$latest"
    ;;
  *) sed -n '2,10s/^# \{0,1\}//p' "$ROOT/scripts/rrd_demo.sh" ;;
esac
