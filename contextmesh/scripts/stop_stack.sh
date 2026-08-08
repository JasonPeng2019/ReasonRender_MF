#!/usr/bin/env bash
# Stop the ContextMesh stack (Tollgate + EverOS) started by start_stack.sh.
# Kills the recorded pid AND the real server processes — `uv run` wrappers
# spawn children that would otherwise survive and hold the JSONL flock lease.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for name in tollgate everos; do
  pidfile="$ROOT/runs/$name.pid"
  if [ -f "$pidfile" ]; then
    kill "$(cat "$pidfile")" 2>/dev/null && echo "$name: stopped wrapper $(cat "$pidfile")"
    rm -f "$pidfile"
  fi
done
pkill -f "token_tracker_proxy.py serve" 2>/dev/null && echo "tollgate: server process stopped"
pkill -f "everos server start" 2>/dev/null && echo "everos: server process stopped"
if command -v docker >/dev/null 2>&1; then
  docker rm -f contextmesh-everos >/dev/null 2>&1 && echo "everos: container stopped"
fi
rm -f "$ROOT/runs/everos.container"
exit 0
