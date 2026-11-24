#!/usr/bin/env bash
# One-shot test install of the hub on TrueNAS SCALE (Docker). Run from the repo root:
#   sudo bash deploy/truenas-test-setup.sh <pool> [port]
# Creates /mnt/<pool>/apps/kidspc-data, writes config.yaml with fresh tokens, builds and starts
# the hub container. Safe to re-run: keeps an existing config.yaml and database.
set -euo pipefail
POOL="${1:?usage: sudo bash deploy/truenas-test-setup.sh <pool> [port]}"
PORT="${2:-8088}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="/mnt/$POOL/apps/kidspc-data"
CFG="$DATA/hub/config.yaml"

echo "==> Building hub image"
docker build -q -t kidspc-hub:local "$REPO/hub" >/dev/null

mkdir -p "$DATA/hub"
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
  if [ -n "$GH" ]; then
    printf '\nupdates:\n  enabled: true\n  github_repo: ppowell1989/Kid_PC_Dashboard\n  release_tag: test-build\n  github_token: "%s"\n' "$GH" >> "$CFG"
    echo "    auto-update on"
  else
    printf '\nupdates: { enabled: false }\n' >> "$CFG"
    echo "    skipped (edit updates: in $CFG later)"
  fi
fi

echo "==> Starting hub on port $PORT"
docker rm -f kidspc-hub >/dev/null 2>&1 || true
docker run -d --name kidspc-hub --restart unless-stopped -p "$PORT:8080" -v "$DATA/hub:/data" kidspc-hub:local >/dev/null
for _ in $(seq 30); do curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 1; done
curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null || { docker logs kidspc-hub | tail -30; exit 1; }

IP="$(ip -4 -br addr show | awk '$1 !~ /^(lo|docker|br-|veth)/ && $2=="UP" {split($3,a,"/"); print a[1]; exit}')"
echo
echo "Hub is running:  http://${IP:-<truenas-ip>}:$PORT   (sign in as Patrick or Barbara)"
echo "Tokens for the PC install script:"
cat "$DATA/hub/pc-tokens.txt"
