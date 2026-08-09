#!/bin/bash
# Start only the memory service required by the selected native Codex demo.
set -euo pipefail
umask 077

BACKEND="${RRD_MEMORY_BACKEND:-}"
case "$BACKEND" in everos|sqlite) ;; *) echo "invalid RRD_MEMORY_BACKEND" >&2; exit 2 ;; esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
RUNS="$ROOT/runs"
STATE="$RUNS/rrd-everos-native.json"
LOCK="$RUNS/.rrd-everos-native.lock"
LOCK_ATTEMPTS="${RRD_LIFECYCLE_LOCK_ATTEMPTS:-300}"
case "$LOCK_ATTEMPTS" in ''|*[!0-9]*|0) echo "invalid lifecycle lock attempts" >&2; exit 2 ;; esac
GROUP_ATTEMPTS="${RRD_GROUP_TERM_ATTEMPTS:-40}"
case "$GROUP_ATTEMPTS" in ''|*[!0-9]*|0) echo "invalid group termination attempts" >&2; exit 2 ;; esac
mkdir -p "$RUNS"

if [ "$BACKEND" = sqlite ]; then
  echo "native Codex local memory: no service required"
  exit 0
fi

health() { /usr/bin/curl -sf -m 2 http://127.0.0.1:8000/health >/dev/null 2>&1; }

LOCK_HELD=0
release_lock() {
  [ "$LOCK_HELD" = 1 ] || return 0
  rm -f "$LOCK/owner"
  rmdir "$LOCK" 2>/dev/null || true
  LOCK_HELD=0
}
acquire_lock() {
  local status
  for _ in $(seq 1 "$LOCK_ATTEMPTS"); do
    if mkdir -m 700 "$LOCK" 2>/dev/null; then
      printf '%s\n' "$$" >"$LOCK/owner"
      LOCK_HELD=1
      return 0
    fi
    status="$(python3 - "$LOCK" <<'PY'
import os,stat,sys
lock=sys.argv[1]
try:
    meta=os.lstat(lock)
    owner=os.path.join(lock,"owner")
    ometa=os.lstat(owner)
    if not stat.S_ISDIR(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o700:
        raise ValueError
    if not stat.S_ISREG(ometa.st_mode) or stat.S_IMODE(ometa.st_mode) != 0o600 or ometa.st_size > 32:
        raise ValueError
    pid=int(open(owner,encoding="ascii").read().strip())
    os.kill(pid,0)
except ProcessLookupError: print("dead")
except (OSError,ValueError): print("unsafe")
else: print("live")
PY
)"
    case "$status" in
      dead) rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null || true ;;
      live) sleep 0.1 ;;
      *) echo "unsafe EverOS lifecycle lock; refusing startup" >&2; exit 1 ;;
    esac
  done
  echo "timed out waiting for EverOS lifecycle lock" >&2
  exit 1
}

acquire_lock
trap release_lock EXIT

read_state() {
  python3 - "$STATE" <<'PY'
import json,os,stat,sys
path=sys.argv[1]
try:
    meta=os.lstat(path)
    if not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size > 4096:
        raise ValueError("unsafe state")
    with open(path,encoding="utf-8") as stream: value=json.load(stream)
except (OSError,UnicodeError,ValueError):
    raise SystemExit(1)
if value.get("v") != 1 or value.get("owner") != "rrd-native": raise SystemExit(1)
fields=("kind","revision","container_id","pid","data_root","start_time","pgid","executable","cwd","nonce")
print(*(value.get(name,"") for name in fields),sep="|")
PY
}

docker_identity_ok() {
  local container="$1" revision="$2" data_root="$3"
  [ -n "$container" ] || return 1
  [ "$(docker inspect -f '{{.Id}}' "$container" 2>/dev/null || true)" = "$container" ] || return 1
  [ "$(docker inspect -f '{{ index .Config.Labels "org.contextmesh.owner" }}' "$container" 2>/dev/null || true)" = rrd-native ] || return 1
  [ "$(docker inspect -f '{{ index .Config.Labels "org.contextmesh.revision" }}' "$container" 2>/dev/null || true)" = "$revision" ] || return 1
  [ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$container" 2>/dev/null || true)" = "$data_root" ] || return 1
  [ "$(docker inspect -f '{{(index (index .NetworkSettings.Ports "8000/tcp") 0).HostIp}}:{{(index (index .NetworkSettings.Ports "8000/tcp") 0).HostPort}}' "$container" 2>/dev/null || true)" = "127.0.0.1:8000" ] || return 1
}

host_process_identity_ok() {
  local pid="$1" pgid="$2" data_root="$3" start_time="$4" executable="$5" cwd="$6" nonce="$7"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  [ "$pgid" = "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  case "$(ps -p "$pid" -o command= 2>/dev/null || true)" in
    *"rrd_everos_host.py --nonce $nonce --cwd $cwd --uv "*" --root $data_root"*) ;;
    *) return 1 ;;
  esac
  [ "$(ps -p "$pid" -o pgid= 2>/dev/null | tr -d ' ' || true)" = "$pgid" ] || return 1
  [ "$(ps -p "$pid" -o lstart= 2>/dev/null | sed 's/^ *//;s/ *$//' || true)" = "$start_time" ] || return 1
  [ "$(/usr/sbin/lsof -a -p "$pid" -d txt -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)" = "$executable" ] || return 1
  [ "$(/usr/sbin/lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)" = "$cwd" ] || return 1
}

host_identity_ok() {
  host_process_identity_ok "$@" || return 1
  local pgid="$2"
  if [ -x /usr/sbin/lsof ]; then
    while IFS= read -r listener; do
      [ "$(ps -p "$listener" -o pgid= 2>/dev/null | tr -d ' ' || true)" = "$pgid" ] || return 1
    done < <(/usr/sbin/lsof -nP -t -iTCP:8000 -sTCP:LISTEN 2>/dev/null)
    [ -n "$(/usr/sbin/lsof -nP -t -iTCP:8000 -sTCP:LISTEN 2>/dev/null)" ] || return 1
  fi
}

# EverOS 1.2.3 eagerly constructs its LLM client even though this demo only uses
# assistant-only writes and keyword reads.  Point that unused client at a closed
# loopback port with a public sentinel credential: no model request can leave the
# process, and no external model credential is required or inherited.
EVEROS_STORAGE_MODEL="disabled-storage-only"
EVEROS_STORAGE_KEY="disabled-storage-only"
EVEROS_STORAGE_URL="http://127.0.0.1:9/v1"

if health; then
  if [ ! -s "$STATE" ]; then
    echo "EverOS :8000 is healthy but is not owned by this demo; refusing adoption." >&2
    exit 1
  fi
  IFS='|' read -r kind revision container pid data_root start_time pgid executable cwd nonce < <(read_state) || {
    echo "EverOS ownership record is invalid; refusing adoption." >&2
    exit 1
  }
  current_revision="$(/usr/bin/git -C "$REPO/EverOS" rev-parse HEAD)"
  expected_data="$ROOT/everos-native-root/$current_revision"
  [ "$revision" = "$current_revision" ] && [ "$data_root" = "$expected_data" ] || {
    echo "EverOS ownership record revision/data root mismatch; refusing adoption." >&2
    exit 1
  }
  case "$kind" in
    docker) docker_identity_ok "$container" "$revision" "$data_root" ;;
    host) host_identity_ok "$pid" "$pgid" "$data_root" "$start_time" "$executable" "$cwd" "$nonce" ;;
    *) false ;;
  esac || {
    echo "EverOS live identity does not match its ownership record; refusing adoption." >&2
    exit 1
  }
  echo "native Codex EverOS memory: already running on :8000"
  release_lock
  exit 0
fi

REV="$(/usr/bin/git -C "$REPO/EverOS" rev-parse HEAD)"
DATA="$ROOT/everos-native-root/$REV"

# Health is not the ownership authority.  If an unhealthy prior record exists,
# recover only a proven-dead identity.  A still-live exact identity is left for
# `down` to terminate; changed/foreign identity is always fail-closed.
if [ -e "$STATE" ]; then
  IFS='|' read -r stale_kind stale_revision stale_container stale_pid stale_data stale_start stale_pgid stale_executable stale_cwd stale_nonce < <(read_state) || {
    echo "EverOS ownership record is invalid; refusing recovery." >&2
    exit 1
  }
  [ "$stale_revision" = "$REV" ] && [ "$stale_data" = "$DATA" ] || {
    echo "EverOS stale ownership record revision/data root mismatch; refusing recovery." >&2
    exit 1
  }
  case "$stale_kind" in
    docker)
      if [ -z "$(docker inspect -f '{{.Id}}' "$stale_container" 2>/dev/null || true)" ]; then
        rm -f "$STATE"
      elif docker_identity_ok "$stale_container" "$stale_revision" "$stale_data"; then
        echo "owned EverOS container is live but unhealthy; run the EverOS down command first" >&2
        exit 1
      else
        echo "EverOS container identity changed; refusing recovery." >&2
        exit 1
      fi
      ;;
    host)
      if ! kill -0 "$stale_pid" 2>/dev/null && ! kill -0 -- "-$stale_pgid" 2>/dev/null; then
        rm -f "$STATE"
      elif host_process_identity_ok "$stale_pid" "$stale_pgid" "$stale_data" "$stale_start" "$stale_executable" "$stale_cwd" "$stale_nonce"; then
        echo "owned EverOS process group is live but unhealthy; run the EverOS down command first" >&2
        exit 1
      elif ! kill -0 "$stale_pid" 2>/dev/null && kill -0 -- "-$stale_pgid" 2>/dev/null; then
        echo "owned EverOS process group remains live without its leader; refusing recovery." >&2
        exit 1
      else
        echo "EverOS host identity changed; refusing recovery." >&2
        exit 1
      fi
      ;;
    *) echo "invalid EverOS ownership kind; refusing recovery." >&2; exit 1 ;;
  esac
fi

mkdir -p "$DATA"
TMP="$STATE.tmp.$$"

reap_started_leader() {
  local state
  [ -n "${STARTED_PID:-}" ] || return 0
  state="$(ps -p "$STARTED_PID" -o stat= 2>/dev/null | tr -d ' ' || true)"
  case "$state" in ""|Z*) wait "$STARTED_PID" 2>/dev/null || true ;; esac
}

terminate_started_group() {
  local pgid="${STARTED_PGID:-${STARTED_PID:-}}"
  [ -n "$pgid" ] || return 0
  kill -TERM -- "-$pgid" 2>/dev/null || true
  for _ in $(seq 1 "$GROUP_ATTEMPTS"); do
    reap_started_leader
    kill -0 -- "-$pgid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 -- "-$pgid" 2>/dev/null; then
    kill -KILL -- "-$pgid" 2>/dev/null || true
  fi
  for _ in $(seq 1 "$GROUP_ATTEMPTS"); do
    reap_started_leader
    kill -0 -- "-$pgid" 2>/dev/null || break
    sleep 0.1
  done
  reap_started_leader
  if kill -0 -- "-$pgid" 2>/dev/null; then
    echo "partially started EverOS process group remained live" >&2
    return 1
  fi
  if [ -x /usr/sbin/lsof ] && [ -n "$(/usr/sbin/lsof -nP -t -iTCP:8000 -sTCP:LISTEN 2>/dev/null || true)" ]; then
    echo "port 8000 remained live after partial-start cleanup" >&2
    return 1
  fi
}

begin_record_window() {
  SIGNAL_PENDING=0
  trap 'SIGNAL_PENDING=130' INT
  trap 'SIGNAL_PENDING=143' TERM
  trap 'SIGNAL_PENDING=129' HUP
}

end_record_window() {
  local pending="$SIGNAL_PENDING"
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  [ "$pending" = 0 ] || exit "$pending"
}

cleanup() {
  status="$?"
  cleanup_failed=0
  trap - EXIT INT TERM HUP
  if [ -n "${STARTED_CONTAINER:-}" ]; then
    docker rm -f "$STARTED_CONTAINER" >/dev/null 2>&1 || cleanup_failed=1
  fi
  if [ -n "${STARTED_PID:-}" ]; then
    terminate_started_group || cleanup_failed=1
  fi
  if [ "$cleanup_failed" = 1 ]; then
    if [ -f "$TMP" ]; then
      mv "$TMP" "$STATE" || echo "failed to preserve partial EverOS ownership record at $TMP" >&2
    elif [ ! -s "$STATE" ]; then
      echo "partial EverOS cleanup failed without a publishable ownership record" >&2
    fi
    echo "partial EverOS cleanup failed; exact ownership record preserved for down" >&2
  else
    rm -f "$TMP"
    if [ "${STATE_PUBLISHING:-0}" = 1 ]; then rm -f "$STATE"; fi
  fi
  release_lock
  if [ "$status" = 0 ] && [ "$cleanup_failed" = 1 ]; then status=1; fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  IMAGE="contextmesh-rrd-everos-native:$REV"
  NAME="contextmesh-rrd-everos-native"
  docker build --build-arg "EVEROS_REV=$REV" -f "$ROOT/Dockerfile.everos" -t "$IMAGE" \
    "$REPO/EverOS" >"$RUNS/rrd-everos-build.log"
  EXISTING="$(docker inspect -f '{{.Id}}' "$NAME" 2>/dev/null || true)"
  if [ -n "$EXISTING" ]; then
    echo "foreign container uses $NAME without an exact local ownership record; refusing removal" >&2
    exit 1
  fi
  begin_record_window
  STARTED_CONTAINER="$(docker run -d --rm --name "$NAME" \
    --label "org.contextmesh.owner=rrd-native" \
    --label "org.contextmesh.revision=$REV" \
    -e EVEROS_LLM__MODEL="$EVEROS_STORAGE_MODEL" \
    -e EVEROS_LLM__API_KEY="$EVEROS_STORAGE_KEY" \
    -e EVEROS_LLM__BASE_URL="$EVEROS_STORAGE_URL" \
    -p 127.0.0.1:8000:8000 -v "$DATA:/data" "$IMAGE" \
    sh -c 'test -f /data/everos.toml || everos init --root /data; exec everos server start --root /data --host 0.0.0.0')"
  python3 - "$TMP" "$REV" "$STARTED_CONTAINER" "$DATA" <<'PY'
import json,os,sys
from pathlib import Path
path=Path(sys.argv[1])
path.write_text(json.dumps({"v":1,"owner":"rrd-native","kind":"docker","revision":sys.argv[2],"container_id":sys.argv[3],"data_root":sys.argv[4]},separators=(",",":"))+"\n")
os.chmod(path,0o600)
PY
  end_record_window
else
  UV="${RRC_DEMO_UV_BIN:-$(command -v uv || true)}"
  [ -n "$UV" ] || { echo "uv is required to start host EverOS" >&2; exit 1; }
  if [ ! -f "$DATA/everos.toml" ]; then
    (cd "$REPO/EverOS" && env -i PATH="/usr/local/bin:/usr/bin:/bin" HOME="$DATA" \
      EVEROS_LLM__MODEL="$EVEROS_STORAGE_MODEL" \
      EVEROS_LLM__API_KEY="$EVEROS_STORAGE_KEY" \
      EVEROS_LLM__BASE_URL="$EVEROS_STORAGE_URL" \
      "$UV" run everos init --root "$DATA")
  fi
  UV="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$UV")"
  HOST_PY="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$(command -v python3)")"
  HOST_CWD="$REPO/EverOS"
  HOST_NONCE="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  begin_record_window
  "$HOST_PY" "$ROOT/scripts/rrd_everos_host.py" --nonce "$HOST_NONCE" \
    --cwd "$HOST_CWD" --uv "$UV" --root "$DATA" >"$RUNS/rrd-everos.log" 2>&1 &
  STARTED_PID=$!
  STARTED_PGID="$STARTED_PID"
  STARTED_AT="$(ps -p "$STARTED_PID" -o lstart= | sed 's/^ *//;s/ *$//')"
  HOST_EXECUTABLE="$HOST_PY"
  python3 - "$TMP" "$REV" "$STARTED_PID" "$STARTED_PGID" "$DATA" "$STARTED_AT" "$HOST_EXECUTABLE" "$HOST_CWD" "$HOST_NONCE" <<'PY'
import json,os,sys
from pathlib import Path
path=Path(sys.argv[1])
path.write_text(json.dumps({"v":1,"owner":"rrd-native","kind":"host","revision":sys.argv[2],"pid":int(sys.argv[3]),"pgid":int(sys.argv[4]),"data_root":sys.argv[5],"start_time":sys.argv[6],"executable":sys.argv[7],"cwd":sys.argv[8],"nonce":sys.argv[9]},separators=(",",":"))+"\n")
os.chmod(path,0o600)
PY
  end_record_window
fi

for _ in $(seq 1 60); do health && break; sleep 0.5; done
if ! health; then
  echo "EverOS failed to become healthy on :8000" >&2
  exit 1
fi
STATE_PUBLISHING=1
mv "$TMP" "$STATE"
trap - EXIT INT TERM HUP
STATE_PUBLISHING=0
STARTED_CONTAINER=""
STARTED_PID=""
STARTED_PGID=""
release_lock
echo "native Codex EverOS memory: started on :8000 (revision ${REV:0:12})"
