#!/usr/bin/env bash
# ContextMesh demo — top-level convenience wrapper.
#
#   ./demo.sh up        start the stack (Tollgate + EverOS)
#   ./demo.sh prep      prepare the real Lane B packets for a three-arm round
#   ./demo.sh raw       open Raw (long-spec reconstruction) TUI
#   ./demo.sh contextmesh open Raw + ContextMesh (long-spec reconstruction) TUI
#   ./demo.sh full      open ContextMesh + RRCv2 cached-packet TUI
#   ./demo.sh meter3    open the live three-arm token meter
#   ./demo.sh down      stop the stack
#
# Each TUI starts its assigned workload automatically. The meter only shows
# the current round.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd="${1:-help}"
case "$cmd" in
  up)    exec "$ROOT/scripts/start_stack.sh" ;;
  down)  exec "$ROOT/scripts/stop_stack.sh" ;;
  prep)
    "$ROOT/scripts/demo_tui.sh" reset
    exec "$ROOT/scripts/demo_tui.sh" prep
    ;;
  raw|raw-recovery|raw-worker-recovery|raw-alt-worker|contextmesh|full|a|b) exec "$ROOT/scripts/demo_tui.sh" "$cmd" ;;
  seed)    exec "$ROOT/scripts/demo_tui.sh" prep ;;
  reset)   exec "$ROOT/scripts/demo_tui.sh" reset ;;
  meter)   exec python3 "$ROOT/scripts/live_meter.py" ;;
  meter3)  exec python3 "$ROOT/scripts/live_meter.py" --three-arm ;;
  *) grep '^#' "$ROOT/demo.sh" | sed 's/^# \{0,1\}//' ;;
esac
