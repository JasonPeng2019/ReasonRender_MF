#!/usr/bin/env bash
# ContextMesh demo — top-level convenience wrapper.
#
#   ./demo.sh up        start the stack (Tollgate + EverOS)
#   ./demo.sh prep      new round + seed digests + preflight  (run this before the demo)
#   ./demo.sh a         open the STOCK opencode TUI            (terminal 1)
#   ./demo.sh b         open the CONTEXTMESH opencode TUI      (terminal 2)
#   ./demo.sh meter     open the live token race               (terminal 3)
#   ./demo.sh down      stop the stack
#
# Then paste the same prompt (already on your clipboard after `prep`) into both
# TUIs and press Enter. The meter shows only the current round.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd="${1:-help}"
case "$cmd" in
  up)    exec "$ROOT/scripts/start_stack.sh" ;;
  down)  exec "$ROOT/scripts/stop_stack.sh" ;;
  prep)
    "$ROOT/scripts/start_stack.sh"
    "$ROOT/scripts/demo_tui.sh" reset
    "$ROOT/scripts/demo_tui.sh" seed
    command -v pbcopy >/dev/null && pbcopy < "$ROOT/demo-prompt.txt" && echo "prompt copied to clipboard"
    "$ROOT/scripts/demo_preflight.sh" || true
    ;;
  a|b)
    # A and B both depend on the local Tollgate proxy (and B on EverOS). Start
    # the idempotent stack here so launching a side directly cannot strand
    # OpenCode in its opaque "Cannot connect to API" retry loop.
    "$ROOT/scripts/start_stack.sh"
    exec "$ROOT/scripts/demo_tui.sh" "$cmd"
    ;;
  seed)    exec "$ROOT/scripts/demo_tui.sh" seed ;;
  reset)   exec "$ROOT/scripts/demo_tui.sh" reset ;;
  meter)   exec python3 "$ROOT/scripts/live_meter.py" ;;
  *) grep '^#' "$ROOT/demo.sh" | sed 's/^# \{0,1\}//' ;;
esac
