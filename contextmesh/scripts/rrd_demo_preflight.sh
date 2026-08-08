#!/usr/bin/env bash
# Local preflight for the Codex + Ollama ContextMesh/RRC demo.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.env.local"
fail=0
CODEX_BIN="${RRD_CODEX_BIN:-codex}"
PREFLIGHT_CODEX_HOME="$(mktemp -d "${TMPDIR:-/tmp}/rrd-codex-preflight.XXXXXX")" || {
  echo "FAIL  could not create isolated Codex preflight home" >&2
  exit 1
}
trap 'rm -rf "$PREFLIGHT_CODEX_HOME"' EXIT

check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "PASS  $name"; else echo "FAIL  $name"; fail=1; fi
}

# Invoked indirectly through check().
# shellcheck disable=SC2329
codex_version_ok() {
  test "$("$CODEX_BIN" --version)" = "codex-cli 0.147.0"
}

# shellcheck disable=SC2329
codex_feature_enabled() {
  local features
  features="$(CODEX_HOME="$PREFLIGHT_CODEX_HOME" "$CODEX_BIN" features list)" || return
  grep -Eq "^$1[[:space:]]+stable[[:space:]]+true" <<<"$features"
}

# shellcheck disable=SC2329
prompt_is_codex_native() {
  ! grep -Eq "task tool|subagent_type" "$ROOT/RRD-demo-prompt.txt"
}

check "RRD Tollgate :8789 healthz" curl -sf -m 5 http://127.0.0.1:8789/healthz
check "RRD response proxy :8790 healthz" curl -sf -m 5 http://127.0.0.1:8790/healthz
check "EverOS :8000 health" curl -sf -m 5 http://127.0.0.1:8000/health
check "OLLAMA_API_KEY is configured" test -n "${OLLAMA_API_KEY:-}"
check "CONTEXTMESH_MODEL is configured" test -n "${CONTEXTMESH_MODEL:-}"
check "Codex CLI is installed" command -v "$CODEX_BIN"
check "Codex CLI matches tested 0.147.0 wire contract" codex_version_ok
check "uv is installed" command -v "${RRC_DEMO_UV_BIN:-uv}"
check "Codex hooks feature is available" codex_feature_enabled hooks
check "Codex multi-agent feature is available" codex_feature_enabled multi_agent
check "Codex hook adapter is present" test -f "$ROOT/scripts/rrd_codex_hook.py"
check "Codex-native audit prompt has no OpenCode task syntax" prompt_is_codex_native

if [ "${1:-}" = "--canary" ]; then
  echo "Running one optional live Ollama Responses canary (this consumes a few tokens)…"
  if OLLAMA_API_KEY="$OLLAMA_API_KEY" CONTEXTMESH_MODEL="$CONTEXTMESH_MODEL" python3 - <<'PY'
import json, os, urllib.request
request=urllib.request.Request(
    "http://127.0.0.1:8789/ollama/setup-canary-root/v1/responses",
    data=json.dumps({"model":os.environ["CONTEXTMESH_MODEL"],"input":"Reply with OK.","stream":False,"max_output_tokens":8}).encode(),
    headers={"Authorization":"Bearer "+os.environ["OLLAMA_API_KEY"],"Content-Type":"application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=60) as response:
    body=json.loads(response.read())
if not isinstance(body, dict) or not body.get("id"):
    raise SystemExit("response lacked an id")
print("PASS  Ollama Responses canary")
PY
  then :; else
    echo "FAIL  Ollama Responses canary"; fail=1
  fi
fi

if [ "$fail" = 0 ]; then
  echo "ALL GREEN — Codex uses the custom Ollama provider; codex login is not required."
else
  echo "NOT READY — fix FAILs above, then rerun $ROOT/RRDdemo.sh prep."
fi
exit "$fail"
