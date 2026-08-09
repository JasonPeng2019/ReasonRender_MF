#!/bin/bash
# Explicit EverOS-backed ContextMesh + ReasonRenderCoding demo.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export RRD_MEMORY_BACKEND=everos
exec "$ROOT/scripts/rrd_demo.sh" "$@"
