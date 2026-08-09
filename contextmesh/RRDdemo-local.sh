#!/bin/bash
# EverOS-free local SQLite ContextMesh + ReasonRenderCoding demo.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export RRD_MEMORY_BACKEND=sqlite
exec "$ROOT/scripts/rrd_demo.sh" "$@"
