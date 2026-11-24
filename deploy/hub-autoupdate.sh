#!/usr/bin/env bash
# Keeps the hub on the newest version of main, by itself. truenas-test-setup.sh installs it as a
# TrueNAS cron job that runs every minute as root:
#   bash /mnt/<pool>/apps/kidspc-data/updater/hub-autoupdate.sh
# Each run: fetch main; if it moved, build the new hub image, back up the database, swap the
# container, and check it. If the new hub doesn't come up healthy within a minute, put the database
# and the old container back, and don't try that version again (the next merge is tried as usual).
#   --now      try again even if this version failed before
#   --status   print the last result and exit
# Writes hub/hub-update.json (the dashboard's PCs page shows it) and updater/updater.log.
# Never prints the GitHub token: git output goes through redact().
# The whole script is one block, so a run reads all of it before it starts and replacing this file
# while it runs (it updates itself after a good update) can't change the run halfway.
{
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="$(dirname "$HERE")"                 # /mnt/<pool>/apps/kidspc-data
STATE="$DATA/hub/hub-update.json"         # inside /data in the container, for the dashboard
LOG="$HERE/updater.log"
# shellcheck source=/dev/null
. "$HERE/updater.env"                     # REPO, PORT, BRANCH (written by truenas-test-setup.sh)
# shellcheck source=hub-run.sh
. "$HERE/hub-run.sh"
BRANCH="${BRANCH:-main}"
FORCE=0
case "${1:-}" in
  --now) FORCE=1 ;;
  --status) cat "$STATE" 2>/dev/null || echo "no update has run yet"; exit 0 ;;
esac

redact() { sed -E 's#://[^/@ ]+@#://***@#g; s#(gh[pousr]_|github_pat_)[A-Za-z0-9_]+#***#g'; }
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | redact >>"$LOG"; }
json_get() { python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception: print("")' "$STATE" "$1"; }
# write_state key=value ...: merges into hub-update.json, always stamps checked_at
write_state() {
  python3 - "$STATE" "$@" <<'PY'
import json, sys, os
from datetime import datetime
path = sys.argv[1]
try: s = json.load(open(path))
except Exception: s = {}
for kv in sys.argv[2:]:
    k, _, v = kv.partition("=")
    s[k] = v
s["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
tmp = path + ".tmp"
with open(tmp, "w") as f: json.dump(s, f)
os.replace(tmp, path)
PY
}

[ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 1000000 ] && mv -f "$LOG" "$LOG.1"
hub_lock "$HERE" 5 || exit 0             # someone (a manual setup run) is busy: try next minute

# git runs as the owner of the checkout, so it uses that person's own GitHub sign-in. If that
# can't fetch (no saved sign-in), it uses the hub's read-only token from config.yaml, passed in the
# environment so it never shows up in a command line or a log.
OWNER="$(stat -c %U "$REPO")"
git_owner() {
  if [ "$OWNER" = root ]; then GIT_TERMINAL_PROMPT=0 git -C "$REPO" "$@"
  else GIT_TERMINAL_PROMPT=0 runuser -u "$OWNER" -- git -C "$REPO" "$@"; fi
}
fetch() {
  local out
  if out="$(git_owner fetch -q origin "$BRANCH" 2>&1)"; then return 0; fi
  local tok
  tok="$(sed -nE 's/^[[:space:]]*github_token:[[:space:]]*"?([A-Za-z0-9_]+)"?.*/\1/p' "$DATA/hub/config.yaml" | head -1)"
  if [ -n "$tok" ]; then
    export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"
    GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$tok" | base64 -w0)"
    export GIT_CONFIG_VALUE_0
    if out="$(git_owner fetch -q origin "$BRANCH" 2>&1)"; then unset GIT_CONFIG_VALUE_0; return 0; fi
    unset GIT_CONFIG_VALUE_0
  fi
  log "could not fetch $BRANCH: $(printf %s "$out" | tail -2 | tr '\n' ' ')"
  return 1
}

fetch || { write_state result=fetch_failed message="Could not reach GitHub to look for a new version."; exit 0; }
TARGET="$(git_owner rev-parse FETCH_HEAD)"
SHORT="${TARGET:0:8}"
RUNNING="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' kidspc-hub 2>/dev/null | sed -n 's/^HUB_VERSION=//p' || true)"
if [ "$TARGET" = "$RUNNING" ] && docker ps -q -f name='^kidspc-hub$' -f status=running | grep -q .; then
  write_state result=current version="$TARGET"
  exit 0
fi
if [ "$FORCE" = 0 ] && [ "$(json_get failed_version)" = "$TARGET" ]; then
  exit 0                                   # this exact version already failed; wait for the next merge
fi
TITLE="$(git_owner log -1 --format=%s "$TARGET" | redact)"
log "new version $SHORT ($TITLE), running ${RUNNING:0:8}"

# Build from a clean copy of the commit, not the working tree, so local edits in the checkout can't
# leak into an automatic update. Then bring the checkout along too (only if that's a fast-forward).
SRC="$(mktemp -d)"; trap 'rm -rf "$SRC"' EXIT
git_owner archive "$TARGET" hub deploy | tar -x -C "$SRC"
if ! hub_build "$SRC/hub" "kidspc-hub:$SHORT" "$TARGET" >>"$LOG" 2>&1; then
  log "build of $SHORT failed; the running hub is unchanged"
  write_state result=build_failed failed_version="$TARGET" failed_title="$TITLE" \
    message="The new version didn't build, so the hub stayed on the old one."
  exit 0
fi
git_owner merge -q --ff-only "$TARGET" >/dev/null 2>&1 || log "note: $REPO has local changes; left it as it is"

# Stop the old hub cleanly, back up its database, start the new one.
PREV_IMAGE="$(docker inspect -f '{{.Image}}' kidspc-hub 2>/dev/null || true)"
[ -n "$PREV_IMAGE" ] && docker tag "$PREV_IMAGE" kidspc-hub:previous
BK="$HERE/backups/$(date +%Y%m%d-%H%M%S)-${RUNNING:0:8}"
mkdir -p "$BK"
docker stop -t 20 kidspc-hub >/dev/null 2>&1 || true
cp -a "$DATA/hub/hub.db"* "$BK/" 2>/dev/null || true
ls -1d "$HERE"/backups/*/ 2>/dev/null | head -n -5 | xargs -r rm -rf   # keep the last 5
hub_start "kidspc-hub:$SHORT" "$PORT" "$DATA"

# Healthy = answers on the new version within a minute, and still does 15 seconds later.
if hub_healthy "$PORT" "$TARGET" 60 && sleep 15 && hub_healthy "$PORT" "$TARGET" 5; then
  docker tag "kidspc-hub:$SHORT" kidspc-hub:local
  log "hub is on $SHORT"
  write_state result=updated version="$TARGET" title="$TITLE" updated_at="$(date -Iseconds)" \
    failed_version= failed_title= message=
  # The updater updates itself only after a version proved good.
  cp -f "$SRC/deploy/hub-autoupdate.sh" "$SRC/deploy/hub-run.sh" "$HERE/" 2>/dev/null || true
  # Old images of ours, except the one running and the one before it.
  docker images --filter label=app=kidspc-hub --format '{{.Repository}}:{{.Tag}}' \
    | grep -vE ":(local|previous|$SHORT)$" | grep -v '<none>' | xargs -r docker rmi >/dev/null 2>&1 || true
  docker image prune -f --filter label=app=kidspc-hub >/dev/null 2>&1 || true
  exit 0
fi

log "new hub $SHORT failed its health check; last lines of its log:"
docker logs --tail 30 kidspc-hub 2>&1 | redact >>"$LOG" || true
docker rm -f kidspc-hub >/dev/null 2>&1 || true
docker rmi "kidspc-hub:$SHORT" >/dev/null 2>&1 || true
if [ -f "$BK/hub.db" ]; then                                 # the old version gets the database it knew
  rm -f "$DATA/hub/hub.db" "$DATA/hub/hub.db-wal" "$DATA/hub/hub.db-shm"
  cp -a "$BK"/hub.db* "$DATA/hub/"
fi
if [ -n "$PREV_IMAGE" ]; then
  hub_start kidspc-hub:previous "$PORT" "$DATA"
  if hub_healthy "$PORT" "" 60; then
    log "rolled back to ${RUNNING:0:8}"
    write_state result=rolled_back version="$RUNNING" failed_version="$TARGET" failed_title="$TITLE" \
      message="The new version didn't start properly, so the hub went back to the one before. It tries again at the next merge."
    exit 0
  fi
fi
log "rollback did not come up either; the hub is down"
write_state result=down failed_version="$TARGET" failed_title="$TITLE" \
  message="The hub could not start. Run the setup line on the TrueNAS."
exit 1
}
