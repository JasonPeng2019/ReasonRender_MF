#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNS="$ROOT/runs"
if [ -s "$RUNS/rrd-response-proxy.pid" ]; then
  kill "$(cat "$RUNS/rrd-response-proxy.pid")" 2>/dev/null || true
  rm -f "$RUNS/rrd-response-proxy.pid" "$RUNS/rrd-response-proxy.version"
  echo "rrd response proxy: stopped"
fi
if [ -s "$RUNS/rrd-tollgate.pid" ]; then
  pid="$(cat "$RUNS/rrd-tollgate.pid")"
  kill "$pid" 2>/dev/null || true
  rm -f "$RUNS/rrd-tollgate.pid" "$RUNS/rrd-tollgate.route"
  echo "rrd tollgate: stopped"
fi
exec "$ROOT/scripts/stop_stack.sh"
