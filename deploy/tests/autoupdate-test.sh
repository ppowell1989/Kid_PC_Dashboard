#!/usr/bin/env bash
# End-to-end check of the hub self-updater with real Docker: a pretend TrueNAS (data folder, a
# checkout owned by a normal user, a GitHub stand-in), then merges that are good, broken, and good again.
# Run as root from the repo root:  sudo bash deploy/tests/autoupdate-test.sh
set -euo pipefail
SRC_REPO="$(cd "$(dirname "$0")/../.." && pwd)"
OWNER="${SUDO_USER:-$(stat -c %U "$SRC_REPO")}"
PORT=18088
W="$(mktemp -d)"; chmod 755 "$W"
DATA="$W/apps/kidspc-data"
FAKE_TOKEN="ghp_FAKEtoken0123456789abcdefghijklmnopqr"
cleanup() { docker rm -f kidspc-hub >/dev/null 2>&1 || true; }
trap cleanup EXIT
fail() { echo "FAIL: $*"; echo "--- updater.log"; cat "$DATA/updater/updater.log" 2>/dev/null || true; echo "--- state"; cat "$DATA/hub/hub-update.json" 2>/dev/null || true; exit 1; }
as_owner() { if [ "$OWNER" = root ]; then "$@"; else runuser -u "$OWNER" -- "$@"; fi; }
version() { curl -fs "http://127.0.0.1:$PORT/healthz" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])'; }
state() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$DATA/hub/hub-update.json" "$1"; }
run_updater() { bash "$DATA/updater/hub-autoupdate.sh" "$@"; }

echo "==> GitHub stand-in and the checkout (owned by $OWNER, like ~/kidspc-src on the TrueNAS)"
chown "$OWNER" "$W"
as_owner git clone -q --bare "$SRC_REPO" "$W/origin.git"
as_owner git -C "$W/origin.git" update-ref refs/heads/main "$(as_owner git -C "$SRC_REPO" rev-parse HEAD)"
as_owner git clone -q -b main "$W/origin.git" "$W/kidspc-src"
as_owner git clone -q -b main "$W/origin.git" "$W/dev"     # where the "merges" are made
as_owner git -C "$W/dev" config user.email ci@example.com
as_owner git -C "$W/dev" config user.name CI
merge() {   # merge <title> <python to change the hub>
  as_owner python3 -c "$2" "$W/dev/hub/app/main.py"
  as_owner git -C "$W/dev" commit -qam "$1"
  as_owner git -C "$W/dev" push -q origin main
  as_owner git -C "$W/dev" rev-parse HEAD
}

echo "==> Data folder and a running hub, the way truenas-test-setup.sh leaves it"
mkdir -p "$DATA/hub" "$DATA/updater"
cp "$SRC_REPO/hub/config.example.yaml" "$DATA/hub/config.yaml"
sed -i 's/enabled: true/enabled: false/' "$DATA/hub/config.yaml"
printf '\nupdates:\n  enabled: false\n  github_token: "%s"\n' "$FAKE_TOKEN" >> "$DATA/hub/config.yaml"
cp "$SRC_REPO/deploy/hub-autoupdate.sh" "$SRC_REPO/deploy/hub-run.sh" "$DATA/updater/"
printf 'REPO=%q\nPORT=%q\nBRANCH=main\n' "$W/kidspc-src" "$PORT" > "$DATA/updater/updater.env"
. "$SRC_REPO/deploy/hub-run.sh"
V0="$(hub_repo_version "$W/kidspc-src")"
hub_build "$SRC_REPO/hub" kidspc-hub:local "$V0"
hub_start kidspc-hub:local "$PORT" "$DATA"
hub_healthy "$PORT" "$V0" 60 || fail "first hub didn't start"
[ "$(version)" = "$V0" ] || fail "healthz should report $V0"

echo "==> Nothing merged: nothing changes"
run_updater
[ "$(state result)" = current ] || fail "expected result current"

echo "==> A good merge goes in by itself"
V1="$(merge "Good change one" 'import sys; p=sys.argv[1]; s=open(p).read(); open(p,"w").write(s+"\n# good change one\n")')"
run_updater
[ "$(version)" = "$V1" ] || fail "hub should be on $V1"
[ "$(state result)" = updated ] || fail "expected result updated"
[ "$(state title)" = "Good change one" ] || fail "title not recorded"
[ "$(as_owner git -C "$W/kidspc-src" rev-parse HEAD)" = "$V1" ] || fail "checkout should have moved along"
[ -f "$DATA/hub/hub.db" ] || fail "database missing"
ls "$DATA"/updater/backups/*/hub.db >/dev/null || fail "no database backup"

echo "==> A broken merge: health check fails, the hub goes back, the database is put back"
V2="$(merge "Broken change" 'import sys; p=sys.argv[1]; s=open(p).read(); open(p,"w").write(s+"\nraise SystemExit(\"broken on purpose\")\n")')"
run_updater
[ "$(version)" = "$V1" ] || fail "hub should have rolled back to $V1"
[ "$(state result)" = rolled_back ] || fail "expected result rolled_back"
[ "$(state failed_version)" = "$V2" ] || fail "failed version not recorded"
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' kidspc-hub | grep -q unless-stopped || fail "rolled-back hub lost its restart policy"

echo "==> The same broken version is not tried again every minute"
started="$(docker inspect -f '{{.State.StartedAt}}' kidspc-hub)"
run_updater
[ "$(docker inspect -f '{{.State.StartedAt}}' kidspc-hub)" = "$started" ] || fail "hub was restarted for a version that already failed"

echo "==> A broken build (not just a broken start) leaves the running hub alone"
as_owner bash -c "echo 'this is not a package' > '$W/dev/hub/requirements.txt'"
V3="$(merge "Broken build" 'import sys')"
run_updater
[ "$(version)" = "$V1" ] || fail "hub should still be on $V1"
[ "$(state result)" = build_failed ] || fail "expected result build_failed"
as_owner git -C "$W/dev" checkout -q "$V1" -- hub/requirements.txt

echo "==> The fix after it goes in"
V4="$(merge "Fix" 'import sys; p=sys.argv[1]; s=open(p).read(); open(p,"w").write(s.replace("raise SystemExit(\"broken on purpose\")\n", ""))')"
run_updater
[ "$(version)" = "$V4" ] || fail "hub should be on $V4"
[ "$(state result)" = updated ] || fail "expected result updated after the fix"
[ -z "$(state failed_version)" ] || fail "failed version should be cleared"

echo "==> A run while another holds the lock does nothing"
( exec 9>"$DATA/updater/.hub-update.lock"; flock 9; sleep 8 ) &
sleep 1
V5="$(merge "Change during a manual run" 'import sys; p=sys.argv[1]; s=open(p).read(); open(p,"w").write(s+"\n# five\n")')"
run_updater
[ "$(version)" = "$V4" ] || fail "updater should have skipped while locked"
wait
run_updater
[ "$(version)" = "$V5" ] || fail "hub should be on $V5 once the lock is free"

echo "==> No GitHub token in anything the updater wrote"
! grep -rq "$FAKE_TOKEN" "$DATA/updater/updater.log" "$DATA/hub/hub-update.json" || fail "token found in the log"
docker images --filter label=app=kidspc-hub --format '{{.Repository}}:{{.Tag}}'
echo "PASS"
