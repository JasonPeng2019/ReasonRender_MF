#!/usr/bin/env bash
# Preflight for the combined ContextMesh + ReasonRenderCoding worker demo.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.env.local"
fail=0

check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "PASS  $name"; else echo "FAIL  $name"; fail=1; fi
}

check "tollgate :8788 healthz" curl -sf -m 5 http://127.0.0.1:8788/healthz
check "everos :8000 health" curl -sf -m 5 http://127.0.0.1:8000/health
check "Ollama API key is configured" test -n "${OLLAMA_API_KEY:-}"
check "Codex CLI is installed" command -v codex
check "Codex CLI is authenticated" codex login status
check "uv is installed" command -v "${RRC_DEMO_UV_BIN:-uv}"
check "combined audit prompt is canonical" cmp -s "$ROOT/RRD-demo-prompt.txt" "$ROOT/demo-prompt.txt"
check "ContextMesh plugin is present" test -f "$ROOT/plugin/contextmesh.ts"
check "RRC task-augmentation plugin is present" test -f "$ROOT/plugin/reasonrendercoding.ts"
check "Ollama Cloud auth + OpenCode model" \
  curl -sf -m 60 https://ollama.com/v1/chat/completions \
    -H "Authorization: Bearer $OLLAMA_API_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$CONTEXTMESH_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}"
OC="${CONTEXTMESH_OPENCODE_BIN:-opencode}"
OCVER="$("$OC" --version 2>/dev/null || true)"
case "$OCVER" in
  1.1[89].*|1.2[0-9].*|[2-9].*) echo "PASS  opencode binary is $OCVER (renders TUI, loads both plugins)" ;;
  *) echo "FAIL  opencode binary is '$OCVER' — need 1.18.15+"; fail=1 ;;
esac

if [ "$fail" = 0 ]; then
  echo "ALL GREEN — services/auth/config ready for the four-worker combined demo."
else
  echo "NOT READY — fix FAILs above, then rerun $ROOT/RRDdemo.sh prep."
fi
exit "$fail"
