#!/bin/bash
# Write-deny wrapper for every RRCv2 convergence verification command.
set -u
set -o pipefail

fail() {
  printf 'rrcv2 verify guard: FAIL: %s\n' "$*" >&2
  exit 1
}

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
REPO=$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd -P) || exit 1
EVIDENCE_DIR="$REPO/.generated/state/rrcv2-convergence/guard"
if [[ ${1-} == "--evidence-dir" ]]; then
  [[ $# -ge 3 ]] || fail "--evidence-dir requires a value"
  EVIDENCE_DIR=$2
  shift 2
fi
[[ ${1-} == "--" ]] || fail "usage: rrd_verify_guard.sh [--evidence-dir DIR] -- command [args...]"
shift
[[ $# -gt 0 ]] || fail "missing command"

case "$EVIDENCE_DIR" in
  /*) ;;
  *) EVIDENCE_DIR="$PWD/$EVIDENCE_DIR" ;;
esac
mkdir -p -- "$EVIDENCE_DIR" || fail "cannot create evidence directory"
chmod 700 "$EVIDENCE_DIR" || fail "cannot protect evidence directory"

ORIGINAL_HOME=${HOME-}
ORIGINAL_CODEX_HOME=${CODEX_HOME-}
MODEL_BEARING=${RRD_VERIFY_MODEL_BEARING-0}
[[ "$MODEL_BEARING" == 0 || "$MODEL_BEARING" == 1 ]] || fail "invalid RRD_VERIFY_MODEL_BEARING"

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/rrcv2-verify-guard.XXXXXX") || fail "cannot create temporary root"
PROFILE="$TMP_ROOT/write-deny.sb"
CONTROL="$TMP_ROOT/control"
mkdir -p "$CONTROL" "$TMP_ROOT/home" "$TMP_ROOT/codex" "$TMP_ROOT/tmp" || fail "cannot prepare temporary roots"
chmod 700 "$TMP_ROOT" "$CONTROL" "$TMP_ROOT/home" "$TMP_ROOT/codex" "$TMP_ROOT/tmp" || fail "cannot protect temporary roots"

# shellcheck disable=SC2329  # Invoked indirectly by the traps below.
cleanup() {
  rm -rf -- "$TMP_ROOT"
}
trap cleanup EXIT INT TERM HUP

if [[ $(uname -s) != Darwin || ! -x /usr/bin/sandbox-exec ]]; then
  fail "no capability-proven sandbox-exec backend on this host"
fi

sb_quote() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '%s' "$value"
}

declare -a DENY_ROOTS DENY_LITERALS PROBES
DENY_ROOTS=(
  "$REPO/.codex"
  "$REPO/.claude"
  "$REPO/contextmesh/everos-root"
  "$REPO/contextmesh/everos-native-root"
  "$REPO/contextmesh/snowflake"
)
if [[ -n "$ORIGINAL_HOME" && "$ORIGINAL_HOME" == /* ]]; then
  DENY_ROOTS+=(
    "$ORIGINAL_HOME/.codex"
    "$ORIGINAL_HOME/.claude"
    "$ORIGINAL_HOME/.ssh"
    "$ORIGINAL_HOME/.aws"
    "$ORIGINAL_HOME/.config/gcloud"
  )
fi
DENY_LITERALS=(
  "$REPO/.env.rrcv2-guard-sentinel"
  "$REPO/CLAUDE.local.rrcv2-guard-sentinel"
)
while IFS= read -r -d '' path; do DENY_LITERALS+=("$path"); done < <(
  find "$REPO" -maxdepth 1 \( -name '.env*' -o -name 'CLAUDE.local*' \) -print0
)

{
  printf '(version 1)\n(allow default)\n'
  for path in "${DENY_ROOTS[@]}"; do
    printf '(deny file-write* (subpath "%s"))\n' "$(sb_quote "$path")"
  done
  for path in "${DENY_LITERALS[@]}"; do
    printf '(deny file-write* (literal "%s"))\n' "$(sb_quote "$path")"
  done
} >"$PROFILE" || fail "cannot write sandbox profile"
chmod 600 "$PROFILE" || fail "cannot protect sandbox profile"

for path in "${DENY_ROOTS[@]}"; do
  [[ -d "$path" ]] && PROBES+=("$path/.rrcv2-guard-write-probe")
done
PROBES+=("${DENY_LITERALS[@]}")

if [[ ${RRD_VERIFY_GUARD_FORCE_PROBE_FAILURE-0} == 1 ]]; then
  fail "forced probe failure before inner command"
fi

probe_index=0
for target in "${PROBES[@]}"; do
  probe_index=$((probe_index + 1))
  allowed="$CONTROL/$probe_index"
  rm -f -- "$allowed" "$target"
  # shellcheck disable=SC2016  # Positional parameters expand in the inner shell.
  /usr/bin/sandbox-exec -f "$PROFILE" /bin/sh -c '
    printf allowed >"$1" || exit 20
    if printf forbidden >"$2" 2>/dev/null; then exit 41; fi
    exit 0
  ' guard-probe "$allowed" "$target" >/dev/null 2>&1
  probe_status=$?
  if [[ $probe_status -ne 0 || ! -f "$allowed" ]]; then
    rm -f -- "$target"
    fail "write-deny probe did not prove target $target"
  fi
  [[ ! -e "$target" && ! -L "$target" ]] || {
    rm -f -- "$target"
    fail "write-deny probe changed target $target"
  }
done

if [[ "$MODEL_BEARING" == 0 ]]; then
  export HOME="$TMP_ROOT/home"
  export CODEX_HOME="$TMP_ROOT/codex"
  export XDG_CONFIG_HOME="$TMP_ROOT/home/.config"
  export XDG_CACHE_HOME="$TMP_ROOT/home/.cache"
  export XDG_DATA_HOME="$TMP_ROOT/home/.local/share"
else
  EXPECTED_CODEX_HOME="$REPO/contextmesh/.codex-rrd-native"
  [[ "$ORIGINAL_HOME" == /* ]] || fail "model-bearing command requires absolute real HOME"
  [[ "$ORIGINAL_CODEX_HOME" == "$EXPECTED_CODEX_HOME" ]] || fail "model-bearing command requires stable CODEX_HOME"
  [[ ! -e "$EXPECTED_CODEX_HOME/auth.json" && ! -L "$EXPECTED_CODEX_HOME/auth.json" ]] || fail "auth.json is forbidden"
  export HOME="$ORIGINAL_HOME"
  export CODEX_HOME="$ORIGINAL_CODEX_HOME"
fi
export TMPDIR="$TMP_ROOT/tmp"
export RRD_VERIFY_GUARD_ACTIVE=1
unset OPENAI_API_KEY ANTHROPIC_API_KEY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

STARTED="$TMP_ROOT/inner-started"
printf started >"$STARTED" || fail "cannot record inner start"
/usr/bin/sandbox-exec -f "$PROFILE" "$@"
status=$?

timestamp=$(/bin/date -u +%Y%m%dT%H%M%SZ)
evidence="$EVIDENCE_DIR/guard-$timestamp-$$.json"
EVIDENCE_PATH="$evidence" GUARD_BACKEND=sandbox-exec GUARD_MODEL_BEARING="$MODEL_BEARING" \
GUARD_PROBE_COUNT="${#PROBES[@]}" GUARD_STATUS="$status" GUARD_HOME="$HOME" GUARD_CODEX_HOME="${CODEX_HOME-}" \
/usr/bin/python3 - <<'PY'
import json, os, pathlib, tempfile
path = pathlib.Path(os.environ["EVIDENCE_PATH"])
row = {
    "backend": os.environ["GUARD_BACKEND"],
    "codex_home": os.environ["GUARD_CODEX_HOME"],
    "home": os.environ["GUARD_HOME"],
    "inner_started": True,
    "model_bearing": os.environ["GUARD_MODEL_BEARING"] == "1",
    "probe_count": int(os.environ["GUARD_PROBE_COUNT"]),
    "status": int(os.environ["GUARD_STATUS"]),
    "v": 1,
}
raw = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
fd, name = tempfile.mkstemp(prefix=".guard.", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.replace(name, path)
finally:
    try: os.unlink(name)
    except FileNotFoundError: pass
PY
evidence_status=$?
[[ $evidence_status -eq 0 ]] || fail "cannot write guard evidence"
exit "$status"
