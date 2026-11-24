#!/usr/bin/env bash
# One-shot test install of the hub on TrueNAS SCALE (Docker). Run from the repo root:
#   sudo bash deploy/truenas-test-setup.sh <pool> [port]
# Creates /mnt/<pool>/apps/kidspc-data, writes config.yaml with fresh tokens, builds and starts
# the hub container, and installs the self-updater (a TrueNAS cron job, see deploy/hub-autoupdate.sh)
# so every later change merged to main reaches the hub by itself. Safe to re-run: keeps an existing
# config.yaml and database. NO_AUTOUPDATE=1 skips the self-updater.
set -euo pipefail
POOL="${1:?usage: sudo bash deploy/truenas-test-setup.sh <pool> [port]}"
PORT="${2:-8088}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="/mnt/$POOL/apps/kidspc-data"
CFG="$DATA/hub/config.yaml"
UPD="$DATA/updater"
# shellcheck source=hub-run.sh
. "$REPO/deploy/hub-run.sh"
VERSION="$(hub_repo_version "$REPO")"

mkdir -p "$DATA/hub" "$UPD"
hub_lock "$UPD"   # waits if an automatic update is running right now

echo "==> Building hub image"
hub_build "$REPO/hub" kidspc-hub:local "$VERSION"
if [ ! -f "$CFG" ]; then
  echo "==> Creating $CFG"
  read -rsp "Choose a dashboard password for Patrick and Barbara: " PW; echo
  [ -n "$PW" ] || { echo "empty password"; exit 1; }
  HASH="$(docker run --rm -e PW="$PW" kidspc-hub:local python -c "import os; from app.security import hash_password; print(hash_password(os.environ['PW']))")"
  tok() { openssl rand -hex 24; }
  ADAM_DEV=$(tok); ADAM_EXT=$(tok); BARR_DEV=$(tok); BARR_EXT=$(tok)
  cat > "$CFG" <<YAML
timezone: America/New_York
parents:
  - { name: Patrick, password_hash: "${HASH}" }
  - { name: Barbara, password_hash: "${HASH}" }
kids:
  # TEST VALUES: Adam gets 10 minutes a day so the warning and lock show up quickly.
  - id: adam
    name: Adam
    device_token: "${ADAM_DEV}"
    extension_token: "${ADAM_EXT}"
    adguard_client: adam-pc
    daily_minutes: { weekday: 10, weekend: 10 }
    bedtime: { weekday: "23:30", weekend: "23:30" }
  - id: barrett
    name: Barrett
    device_token: "${BARR_DEV}"
    extension_token: "${BARR_EXT}"
    adguard_client: barrett-pc
    daily_minutes: { weekday: 90, weekend: 180 }
    bedtime: { weekday: "20:30", weekend: "21:30" }
decisions:
  web_mode: filtered
  game_pass_enabled: false
  friend_verification: none
  remote_access: lan_only
lan_cidrs: ["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "127.0.0.0/8"]
allowed_sites: [google.com, wikipedia.org, khanacademy.org]
session_secret: "$(openssl rand -hex 32)"
adguard: { enabled: false }
livekit:
  url: ws://127.0.0.1:7880
  api_url: http://127.0.0.1:7880
  api_key: kidspc
  api_secret: "$(openssl rand -hex 32)"
  public_join_base: http://127.0.0.1:${PORT}
YAML
  chmod 600 "$CFG"
  {
    echo "Adam's PC:     -DeviceToken ${ADAM_DEV} -ExtensionToken ${ADAM_EXT}"
    echo "Barrett's PC:  -DeviceToken ${BARR_DEV} -ExtensionToken ${BARR_EXT}"
  } > "$DATA/hub/pc-tokens.txt"
  chmod 600 "$DATA/hub/pc-tokens.txt"
else
  echo "==> Keeping existing $CFG"
fi

# Auto-update (asked once): the hub fetches new Windows builds from the private GitHub release
# and the PCs pull them from the hub. Needs a fine-grained token: this repo only, Contents read-only.
if ! grep -q '^updates:' "$CFG"; then
  echo "==> Auto-update for the PCs (optional, press Enter to skip)"
  read -rsp "GitHub token with read-only Contents access to Kid_PC_Dashboard: " GH; echo
  GH=$(printf %s "$GH" | tr -cd 'A-Za-z0-9_')   # a paste with Ctrl+V in the web shell adds invisible control characters
  if [ -n "$GH" ]; then
    printf '\nupdates:\n  enabled: true\n  github_repo: ppowell1989/Kid_PC_Dashboard\n  release_tag: test-build\n  github_token: "%s"\n' "$GH" >> "$CFG"
    echo "    auto-update on"
  else
    printf '\nupdates: { enabled: false }\n' >> "$CFG"
    echo "    skipped (edit updates: in $CFG later)"
  fi
fi

echo "==> Starting hub on port $PORT"
hub_start kidspc-hub:local "$PORT" "$DATA"
hub_healthy "$PORT" "$VERSION" 30 || { docker logs kidspc-hub | tail -30; exit 1; }

if [ "${NO_AUTOUPDATE:-0}" != 1 ]; then
  echo "==> Self-updater: the hub installs each change merged to main by itself"
  cp -f "$REPO/deploy/hub-autoupdate.sh" "$REPO/deploy/hub-run.sh" "$UPD/"
  printf 'REPO=%q\nPORT=%q\nBRANCH=main\n' "$REPO" "$PORT" > "$UPD/updater.env"
  rm -f "$DATA/hub/hub-update.json"   # a manual run starts with a clean slate (also clears a failed version)
  CMD="bash $UPD/hub-autoupdate.sh"
  if command -v midclt >/dev/null 2>&1; then
    # A TrueNAS cron job (System, Advanced, Cron Jobs) survives TrueNAS updates; /etc/cron.d does not.
    ID="$(midclt call cronjob.query | python3 -c 'import json,sys; print(next((str(j["id"]) for j in json.load(sys.stdin) if "hub-autoupdate.sh" in j["command"]), ""))')"
    JOB="$(python3 -c 'import json,sys; print(json.dumps({"enabled": True, "user": "root", "command": sys.argv[1],
      "description": "Kids PC hub: install new versions from main", "stdout": True, "stderr": True,
      "schedule": {"minute": "*", "hour": "*", "dom": "*", "month": "*", "dow": "*"}}))' "$CMD")"
    if [ -n "$ID" ]; then midclt call cronjob.update "$ID" "$JOB" >/dev/null; else midclt call cronjob.create "$JOB" >/dev/null; fi
    echo "    TrueNAS cron job set: every minute"
  else
    printf 'SHELL=/bin/bash\n* * * * * root %s >/dev/null 2>&1\n' "$CMD" > /etc/cron.d/kidspc-hub-autoupdate
    echo "    /etc/cron.d/kidspc-hub-autoupdate set: every minute"
  fi
  echo "    Log: $UPD/updater.log   Last result: sudo bash $UPD/hub-autoupdate.sh --status"
fi

IP="$(ip -4 -br addr show | awk '$1 !~ /^(lo|docker|br-|veth)/ && $2=="UP" {split($3,a,"/"); print a[1]; exit}')"
echo
echo "Hub is running:  http://${IP:-<truenas-ip>}:$PORT   (sign in as Patrick or Barbara)"
echo "Tokens for the PC install script:"
cat "$DATA/hub/pc-tokens.txt"
