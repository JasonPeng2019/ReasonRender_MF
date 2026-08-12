#!/bin/bash
# Historical/non-RRCv2 four-handler audit experiment.
#
# This command intentionally runs the frozen generic audit matrix. It does not
# exercise ReasonRenderCoding's SPEC/PRIME/IMPLEMENT/VERIFY state machine and
# its results must never be reported as RRCv2 evidence.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
case "${1:-help}" in
  plan) exec uv run --locked --project "$REPO/pyproject.toml" python "$ROOT/bench/run_bench.py" --plan ;;
  run) shift; exec uv run --locked --project "$REPO/pyproject.toml" python "$ROOT/bench/run_bench.py" --run-ablation "$@" ;;
  resume) shift; exec uv run --locked --project "$REPO/pyproject.toml" python "$ROOT/bench/run_bench.py" --run-ablation --resume "$@" ;;
  prompt) cat "$ROOT/RRD-audit-prompt.txt" ;;
  *) sed -n '2,7s/^# \{0,1\}//p' "$0" ;;
esac
