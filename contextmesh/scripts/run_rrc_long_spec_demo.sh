#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_ID="${RRC_LONG_SPEC_RUN_ID:-$(date -u +%Y%m%d-%H%M%S)}"
if [ -z "${CONTEXTMESH_OPENCODE_BIN:-}" ] && [ -x "$HOME/.opencode/bin/opencode" ]; then
  export CONTEXTMESH_OPENCODE_BIN="$HOME/.opencode/bin/opencode"
fi

bash "$SCRIPT_DIR/start_stack.sh"
python3 "$ROOT/bench/rrc_long_spec_demo.py" \
  --out "$ROOT/runs/$RUN_ID" \
  --live \
  --runid "$RUN_ID" \
  "$@"
