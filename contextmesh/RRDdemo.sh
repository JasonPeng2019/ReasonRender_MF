#!/usr/bin/env bash
# Combined ContextMesh + ReasonRenderCoding multi-agent demo.
#
#   ./RRDdemo.sh up       start Tollgate + EverOS
#   ./RRDdemo.sh prep     new round + seed shared digests + preflight
#   ./RRDdemo.sh a        open the COLD opencode TUI (+ ContextMesh, terminal 1)
#   ./RRDdemo.sh b        open the WARM opencode TUI (+ ContextMesh, terminal 2)
#   ./RRDdemo.sh meter    open combined live token/reuse meter   (terminal 3)
#   ./RRDdemo.sh down     stop the stack
#
# Paste the same four-handler audit prompt into both TUIs. Each orchestrator
# launches four real OpenCode worker subagents. ContextMesh optimizes their
# overlapping reads/results; RRC plans every COLD worker but plans once and
# reuses the validated packet for WARM siblings.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNS="$ROOT/runs/rrd-demo"
ROUND_FILE="$RUNS/round"
cmd="${1:-help}"

load_round() {
  if [ ! -s "$ROUND_FILE" ]; then
    echo "No combined demo round exists. Run: $ROOT/RRDdemo.sh prep" >&2
    exit 1
  fi
  ROUND="$(cat "$ROUND_FILE")"
  ROUND_DIR="$RUNS/$ROUND"
}

case "$cmd" in
  up)    exec "$ROOT/scripts/start_stack.sh" ;;
  down)  exec "$ROOT/scripts/stop_stack.sh" ;;
  prep)
    "$ROOT/scripts/start_stack.sh"
    # Fail with the actionable auth/connectivity report before starting an
    # OpenCode seed session (which would otherwise enter its opaque retry loop).
    "$ROOT/scripts/rrd_demo_preflight.sh"
    "$ROOT/scripts/rrd_demo_tui.sh" reset
    "$ROOT/scripts/rrd_demo_tui.sh" seed
    load_round
    mkdir -p "$ROUND_DIR"
    cp "$ROOT/RRD-demo-prompt.txt" "$ROOT/runs/RRD-demo-prompt.txt"
    command -v pbcopy >/dev/null && pbcopy < "$ROOT/RRD-demo-prompt.txt" && echo "audit prompt copied to clipboard"
    ;;
  a|b)
    "$ROOT/scripts/start_stack.sh"
    exec "$ROOT/scripts/rrd_demo_tui.sh" "$cmd"
    ;;
  seed)    exec "$ROOT/scripts/rrd_demo_tui.sh" seed ;;
  reset)   exec "$ROOT/scripts/rrd_demo_tui.sh" reset ;;
  meter)
    load_round
    exec python3 "$ROOT/scripts/rrd_combined_meter.py" --round "$ROUND" --watch
    ;;
  prompt)  cat "$ROOT/RRD-demo-prompt.txt" ;;
  *) grep '^#' "$ROOT/RRDdemo.sh" | grep -v '^#!' | sed 's/^# \{0,1\}//' ;;
esac
