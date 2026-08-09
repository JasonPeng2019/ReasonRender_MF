#!/bin/bash
# Stop only an EverOS service proven to be owned by the native Codex demo.
set -euo pipefail
umask 077

BACKEND="${RRD_MEMORY_BACKEND:-}"
case "$BACKEND" in everos|sqlite) ;; *) echo "invalid RRD_MEMORY_BACKEND" >&2; exit 2 ;; esac
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/runs/rrd-everos-native.json"
LOCK="$ROOT/runs/.rrd-everos-native.lock"
LOCK_ATTEMPTS="${RRD_LIFECYCLE_LOCK_ATTEMPTS:-300}"
case "$LOCK_ATTEMPTS" in ''|*[!0-9]*|0) echo "invalid lifecycle lock attempts" >&2; exit 2 ;; esac
GROUP_ATTEMPTS="${RRD_GROUP_TERM_ATTEMPTS:-40}"
case "$GROUP_ATTEMPTS" in ''|*[!0-9]*|0) echo "invalid group termination attempts" >&2; exit 2 ;; esac
mkdir -p "$ROOT/runs"

if [ "$BACKEND" = sqlite ]; then
  echo "native Codex local memory: nothing to stop"
  exit 0
fi
LOCK_HELD=0
release_lock() {
  [ "$LOCK_HELD" = 1 ] || return 0
  rm -f "$LOCK/owner"
  rmdir "$LOCK" 2>/dev/null || true
  LOCK_HELD=0
}
for _ in $(seq 1 "$LOCK_ATTEMPTS"); do
  if mkdir -m 700 "$LOCK" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCK/owner"
    LOCK_HELD=1
    break
  fi
  status="$(python3 - "$LOCK" <<'PY'
import os,stat,sys
try:
    lock=sys.argv[1]; meta=os.lstat(lock); owner=os.path.join(lock,"owner"); ometa=os.lstat(owner)
    if not stat.S_ISDIR(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o700: raise ValueError
    if not stat.S_ISREG(ometa.st_mode) or stat.S_IMODE(ometa.st_mode) != 0o600 or ometa.st_size > 32: raise ValueError
    pid=int(open(owner,encoding="ascii").read().strip()); os.kill(pid,0)
except ProcessLookupError: print("dead")
except (OSError,ValueError): print("unsafe")
else: print("live")
PY
)"
  case "$status" in
    dead) rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null || true ;;
    live) sleep 0.1 ;;
    *) echo "unsafe EverOS lifecycle lock; refusing teardown" >&2; exit 1 ;;
  esac
done
[ "$LOCK_HELD" = 1 ] || { echo "timed out waiting for EverOS lifecycle lock" >&2; exit 1; }
trap release_lock EXIT
[ -s "$STATE" ] || { echo "no demo-owned EverOS service is recorded"; exit 0; }

IFS='|' read -r kind revision container pid data_root start_time pgid executable cwd nonce < <(python3 - "$STATE" <<'PY'
import json,os,stat,sys
try:
    meta=os.lstat(sys.argv[1])
    if not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size > 4096:
        raise ValueError("unsafe state")
    with open(sys.argv[1],encoding="utf-8") as stream: value=json.load(stream)
except (OSError,UnicodeError,ValueError): raise SystemExit(1)
if value.get("v") != 1 or value.get("owner") != "rrd-native": raise SystemExit(1)
print(*(value.get(name,"") for name in ("kind","revision","container_id","pid","data_root","start_time","pgid","executable","cwd","nonce")),sep="|")
PY
) || { echo "invalid EverOS ownership record; refusing teardown" >&2; exit 1; }
case "$kind" in
  docker)
    [ "$(docker inspect -f '{{.Id}}' "$container" 2>/dev/null || true)" = "$container" ] &&
      [ "$(docker inspect -f '{{ index .Config.Labels "org.contextmesh.owner" }}' "$container" 2>/dev/null || true)" = rrd-native ] &&
      [ "$(docker inspect -f '{{ index .Config.Labels "org.contextmesh.revision" }}' "$container" 2>/dev/null || true)" = "$revision" ] &&
      [ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$container" 2>/dev/null || true)" = "$data_root" ] &&
      [ "$(docker inspect -f '{{(index (index .NetworkSettings.Ports "8000/tcp") 0).HostIp}}:{{(index (index .NetworkSettings.Ports "8000/tcp") 0).HostPort}}' "$container" 2>/dev/null || true)" = "127.0.0.1:8000" ] || {
        echo "EverOS container identity changed; refusing teardown" >&2
        exit 1
      }
    docker rm -f "$container" >/dev/null
    ;;
  host)
    case "$pid" in ''|*[!0-9]*) echo "invalid EverOS PID" >&2; exit 1 ;; esac
    [ "$pgid" = "$pid" ] || { echo "EverOS PGID identity changed; refusing teardown" >&2; exit 1; }
    command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    case "$command_line" in *"rrd_everos_host.py --nonce $nonce --cwd $cwd --uv "*" --root $data_root"*) ;; *) echo "EverOS PID identity changed; refusing teardown" >&2; exit 1 ;; esac
    [ "$(ps -p "$pid" -o pgid= 2>/dev/null | tr -d ' ' || true)" = "$pgid" ] || {
      echo "EverOS process-group identity changed; refusing teardown" >&2
      exit 1
    }
    [ "$(ps -p "$pid" -o lstart= 2>/dev/null | sed 's/^ *//;s/ *$//' || true)" = "$start_time" ] || {
      echo "EverOS PID start time changed; refusing teardown" >&2
      exit 1
    }
    [ "$(/usr/sbin/lsof -a -p "$pid" -d txt -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)" = "$executable" ] || {
      echo "EverOS executable identity changed; refusing teardown" >&2
      exit 1
    }
    [ "$(/usr/sbin/lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)" = "$cwd" ] || {
      echo "EverOS cwd identity changed; refusing teardown" >&2
      exit 1
    }
    if [ -x /usr/sbin/lsof ]; then
      listeners="$(/usr/sbin/lsof -nP -t -iTCP:8000 -sTCP:LISTEN 2>/dev/null || true)"
      if [ -n "$listeners" ]; then
        while IFS= read -r listener; do
          [ "$(ps -p "$listener" -o pgid= 2>/dev/null | tr -d ' ' || true)" = "$pgid" ] || {
            echo "foreign listener shares port 8000; refusing teardown" >&2
            exit 1
          }
        done <<<"$listeners"
      fi
    fi
    kill -TERM -- "-$pgid"
    for _ in $(seq 1 "$GROUP_ATTEMPTS"); do
      kill -0 -- "-$pgid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 -- "-$pgid" 2>/dev/null; then
      # The leader may already have been reaped while a signal-ignoring descendant
      # still keeps this same process group alive.  A live group cannot reuse its
      # PGID, so it remains the exact group validated above and is safe to kill.
      kill -KILL -- "-$pgid" 2>/dev/null || true
    fi
    for _ in $(seq 1 "$GROUP_ATTEMPTS"); do
      kill -0 -- "-$pgid" 2>/dev/null || break
      sleep 0.1
    done
    kill -0 -- "-$pgid" 2>/dev/null && {
      echo "owned EverOS process group remained live after teardown" >&2
      exit 1
    }
    [ -z "$(/usr/sbin/lsof -nP -t -iTCP:8000 -sTCP:LISTEN 2>/dev/null || true)" ] || {
      echo "port 8000 remained live after owned group teardown" >&2
      exit 1
    }
    ;;
  *) echo "invalid EverOS ownership kind" >&2; exit 1 ;;
esac
rm -f "$STATE"
echo "native Codex EverOS memory stopped"
