#!/usr/bin/env bash
# Start the ContextMesh measurement + memory stack:
#   1. Tollgate token proxy on :8788  (route ollama=openai@https://ollama.com)
#   2. EverOS memory server on :8000  (LLM slot pointed through Tollgate)
# Idempotent: skips a component whose port is already serving.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
RUNS="$ROOT/runs"
mkdir -p "$RUNS"

# `demo.sh a` and `demo.sh b` are commonly launched from separate terminals.
# Serialize their idempotent startup checks so both cannot spawn Tollgate at
# once and race for the JSONL sink's exclusive lease.
START_LOCK="$RUNS/.start-stack.lock"
release_start_lock() {
  if [ -f "$START_LOCK/pid" ] && [ "$(cat "$START_LOCK/pid")" = "$$" ]; then
    rm -f "$START_LOCK/pid"
    rmdir "$START_LOCK" 2>/dev/null || true
  fi
}
for _ in $(seq 1 600); do
  if mkdir "$START_LOCK" 2>/dev/null; then
    echo $$ > "$START_LOCK/pid"
    trap release_start_lock EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    break
  fi
  owner="$(cat "$START_LOCK/pid" 2>/dev/null || true)"
  if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
    rm -f "$START_LOCK/pid"
    rmdir "$START_LOCK" 2>/dev/null || true
    continue
  fi
  sleep 0.1
done
if [ ! -f "$START_LOCK/pid" ] || [ "$(cat "$START_LOCK/pid")" != "$$" ]; then
  echo "stack startup is still locked by another process; retry: $ROOT/demo.sh up" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$ROOT/.env.local"

# --- Tollgate ---
if curl -sf http://127.0.0.1:8788/healthz >/dev/null 2>&1; then
  echo "tollgate: already running on :8788"
else
  (
    cd "$REPO/TokenTracker"
    nohup uv run python token_tracker_proxy.py serve --port 8788 \
      --log "$RUNS/tokens.jsonl" \
      --route "ollama=openai@https://ollama.com" \
      >"$RUNS/tollgate.log" 2>&1 &
    echo $! > "$RUNS/tollgate.pid"
  )
  for _ in $(seq 1 30); do
    curl -sf http://127.0.0.1:8788/healthz >/dev/null 2>&1 && break
    sleep 0.5
  done
  if ! curl -sf http://127.0.0.1:8788/healthz >/dev/null 2>&1; then
    echo "tollgate: failed to start; recent log output:" >&2
    tail -20 "$RUNS/tollgate.log" >&2 || true
    echo "Run '$ROOT/demo.sh down' once, then retry '$ROOT/demo.sh up'." >&2
    exit 1
  fi
  echo "tollgate: started on :8788 (log: $RUNS/tollgate.log, sink: $RUNS/tokens.jsonl)"
fi

# --- EverOS ---
if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "everos: already running on :8000"
else
  EVEROS_REV="$(git -C "$REPO/EverOS" rev-parse HEAD)"
  # Keep the local demo store revision-scoped. EverOS intentionally fails
  # closed on index schema drift; a new revision gets a clean compatible store
  # and `demo.sh prep` immediately seeds it.
  EVROOT="$ROOT/everos-root/$EVEROS_REV"
  mkdir -p "$EVROOT"

  # LanceDB does not publish a wheel for Intel macOS. Prefer a small Linux
  # container whenever Docker is available; fall back to the host install on
  # platforms where the locked EverOS environment is supported.
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    IMAGE="contextmesh-everos:local"
    CONTAINER="contextmesh-everos"
    IMAGE_REV="$(docker image inspect "$IMAGE" --format '{{ index .Config.Labels "org.contextmesh.everos-revision" }}' 2>/dev/null || true)"
    if [ "$IMAGE_REV" != "$EVEROS_REV" ]; then
      echo "everos: building Linux image for revision ${EVEROS_REV:0:12}"
      docker build \
        --build-arg "EVEROS_REV=$EVEROS_REV" \
        -f "$ROOT/Dockerfile.everos" \
        -t "$IMAGE" \
        "$REPO/EverOS" >"$RUNS/everos-build.log"
    fi
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker run -d --rm \
      --name "$CONTAINER" \
      --add-host host.docker.internal:host-gateway \
      -p 127.0.0.1:8000:8000 \
      -v "$EVROOT:/data" \
      -e "EVEROS_LLM__API_KEY=$OLLAMA_API_KEY" \
      -e "EVEROS_LLM__BASE_URL=http://host.docker.internal:8788/ollama/everos-extraction/v1" \
      -e "EVEROS_LLM__MODEL=$CONTEXTMESH_MODEL" \
      "$IMAGE" \
      sh -c 'test -f /data/everos.toml || everos init --root /data; exec everos server start --root /data --host 0.0.0.0' \
      >"$RUNS/everos.container"
  else
    (
      cd "$REPO/EverOS"
      if [ ! -f "$EVROOT/everos.toml" ]; then
        uv run everos init --root "$EVROOT"
      fi
      # LLM slot is OpenAI-protocol; route it through Tollgate so EverOS's own
      # extraction cost is metered under the session "everos-extraction".
      EVEROS_ROOT="$EVROOT" \
      EVEROS_LLM__API_KEY="$OLLAMA_API_KEY" \
      EVEROS_LLM__BASE_URL="http://127.0.0.1:8788/ollama/everos-extraction/v1" \
      EVEROS_LLM__MODEL="$CONTEXTMESH_MODEL" \
      nohup uv run everos server start --root "$EVROOT" \
        >"$RUNS/everos.log" 2>&1 &
      echo $! > "$RUNS/everos.pid"
    )
  fi
  for _ in $(seq 1 60); do
    curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && break
    sleep 0.5
  done
  curl -sf http://127.0.0.1:8000/health >/dev/null
  echo "everos: started on :8000 (root: $EVROOT)"
fi

echo "stack ready"
