#!/bin/bash
# Compatibility entry point: the default demo now uses native Codex with local memory.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "demo.sh now forwards to the native Codex local-memory product." >&2
exec "$ROOT/RRDdemo-local.sh" "$@"
