#!/usr/bin/env bash
# One-command preflight for the live demo. Run this BEFORE going on stage.
# Verifies: stack up, EverOS KV round-trip, Ollama Cloud reachable with the key,
# demo workspace seeded. Prints PASS/FAIL per check; exits non-zero on any FAIL.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.env.local"
fail=0

check() { # name, command...
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "PASS  $name"; else echo "FAIL  $name"; fail=1; fi
}

check "tollgate :8788 healthz"  curl -sf -m 5 http://127.0.0.1:8788/healthz
check "everos :8000 health"     curl -sf -m 5 http://127.0.0.1:8000/health
check "everos digest KV round-trip" python3 "$ROOT/scripts/smoke_everos.py"
check "ollama cloud auth + model (also warms the model)" \
  curl -sf -m 60 https://ollama.com/v1/chat/completions \
    -H "Authorization: Bearer $OLLAMA_API_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$CONTEXTMESH_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}"
check "demo workspace seeded (side B warm)" test -f "$ROOT/runs/demo-tui/.seeded"
OC="${CONTEXTMESH_OPENCODE_BIN:-opencode}"
OCVER="$("$OC" --version 2>/dev/null)"
case "$OCVER" in
  1.1[89].*|1.2[0-9].*|[2-9].*) echo "PASS  opencode binary is $OCVER (renders TUI, loads plugin)";;
  *) echo "FAIL  opencode binary is '$OCVER' — need 1.18.15+ for TUI+plugin parity"; fail=1;;
esac

if [ "$fail" = 0 ]; then
  echo "ALL GREEN — round $(cat "$ROOT/runs/demo-tui/round" 2>/dev/null || echo '(run demo_tui.sh reset)'); prompt in $ROOT/runs/demo-prompt.txt (pbcopy it)."
else
  echo "NOT READY — fix FAILs above (start_stack.sh / demo_tui.sh seed)."
fi
exit "$fail"
