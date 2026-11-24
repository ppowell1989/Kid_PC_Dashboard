#!/usr/bin/env bash
# Friend voice chat for the TrueNAS test install: a LiveKit voice server, reachable over HTTPS.
# Run from the repo root, after deploy/truenas-test-setup.sh:
#   sudo bash deploy/truenas-voice-setup.sh <pool> <domain> [hub_port] [https_port | tunnel]
#
# Two ways to get HTTPS in front of it (the 4th argument):
#   tunnel       You already run a Cloudflare Tunnel. Nothing extra is started here; you add two
#                routes for <domain> to the tunnel. No new 443 forward, nothing existing changes.
#   <a port>     (default 8443) This script starts its own Caddy on that port with a Let's Encrypt
#                certificate, and the router forwards public 443 to it.
#
# What becomes reachable from the internet (and nothing else):
#   https://<domain>/join/...  the friend's invite page (and the one API call it makes)
#   https://<domain>/rtc       LiveKit signalling
#   7882/udp, 7881/tcp         LiveKit audio (always straight to this machine, never through a tunnel)
# The parent dashboard is NOT routed; it stays home-network only.
#
# The script prints what is left to do by hand at the end. Safe to re-run.
set -euo pipefail
POOL="${1:?usage: sudo bash deploy/truenas-voice-setup.sh <pool> <domain> [hub_port] [https_port | tunnel]}"
DOMAIN="${2:?give the domain friends will use, e.g. voice.example.com}"
PORT="${3:-8088}"
HTTPS_PORT="${4:-8443}"            # a port for our own Caddy, or the word "tunnel"
[[ "$HTTPS_PORT" == "tunnel" || "$HTTPS_PORT" =~ ^[0-9]+$ ]] || { echo "4th argument must be a port number or the word tunnel"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LIVEKIT_IMAGE="${LIVEKIT_IMAGE:-livekit/livekit-server:v1.8.0}"   # GUESS: a newer v1.x tag is fine too
CADDY_IMAGE="${CADDY_IMAGE:-caddy:2}"
DATA="/mnt/$POOL/apps/kidspc-data"
CFG="$DATA/hub/config.yaml"

[[ "$DOMAIN" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}$ ]] || { echo "\"$DOMAIN\" doesn't look like a domain name"; exit 1; }
[ -f "$CFG" ] || { echo "No $CFG yet. Run deploy/truenas-test-setup.sh first."; exit 1; }
docker image inspect kidspc-hub:local >/dev/null 2>&1 || { echo "Hub image missing. Run deploy/truenas-test-setup.sh first."; exit 1; }

# shellcheck source=hub-run.sh
. "$REPO/deploy/hub-run.sh"
mkdir -p "$DATA/updater"
hub_lock "$DATA/updater"   # waits if an automatic hub update is running right now
echo "==> Rebuilding the hub from this copy of the code"
hub_build "$REPO/hub" kidspc-hub:local "$(hub_repo_version "$REPO")"

echo "==> Reading the voice keys from the hub config"
read -r KEY SECRET < <(docker run --rm -v "$DATA/hub:/data:ro" kidspc-hub:local \
  python -c "from app.config import load_settings; s = load_settings('/data/config.yaml'); print(s.livekit.api_key, s.livekit.api_secret)")
[ "${#SECRET}" -ge 32 ] || { echo "livekit.api_secret in $CFG must be at least 32 characters"; exit 1; }

mkdir -p "$DATA/voice" "$DATA/caddy"
cat > "$DATA/voice/livekit.yaml" <<YAML
port: 7880
rtc:
  tcp_port: 7881
  udp_port: 7882            # single UDP port, so the router needs one UDP forward
  use_external_ip: true     # finds the home's public IP so friends outside can connect
keys:
  ${KEY}: ${SECRET}
room:
  empty_timeout: 300
  max_participants: 6
logging:
  level: info
YAML
chmod 600 "$DATA/voice/livekit.yaml"

if [ "$HTTPS_PORT" != "tunnel" ]; then
cat > "$DATA/voice/Caddyfile" <<CADDY
# Public face: only the friend invite pages and LiveKit signalling. Everything else is a 404.
${DOMAIN} {
	@livekit path /rtc*
	handle @livekit {
		reverse_proxy host.docker.internal:7880
	}
	@friend path /join/* /api/voice/join/* /static/app.css /static/fonts/*
	handle @friend {
		reverse_proxy host.docker.internal:${PORT} {
			header_up X-Real-IP {remote_host}
		}
	}
	handle {
		respond "Not found" 404
	}
}
CADDY
fi

echo "==> Pointing the hub at wss://$DOMAIN"
sed -i "/^livekit:/,/^[^ #]/ {
  s|^  url: .*|  url: wss://${DOMAIN}|
  s|^  api_url: .*|  api_url: http://host.docker.internal:7880|
  s|^  public_join_base: .*|  public_join_base: https://${DOMAIN}|
}" "$CFG"

echo "==> Starting the voice server"
docker rm -f kidspc-livekit kidspc-caddy >/dev/null 2>&1 || true
docker run -d --name kidspc-livekit --restart unless-stopped --network host \
  -v "$DATA/voice/livekit.yaml:/etc/livekit.yaml:ro" "$LIVEKIT_IMAGE" --config /etc/livekit.yaml >/dev/null
if [ "$HTTPS_PORT" != "tunnel" ]; then
  echo "==> Starting HTTPS (Caddy on port $HTTPS_PORT)"
  docker run -d --name kidspc-caddy --restart unless-stopped -p "$HTTPS_PORT:443" \
    --add-host host.docker.internal:host-gateway \
    -v "$DATA/voice/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$DATA/caddy:/data" "$CADDY_IMAGE" >/dev/null
fi

echo "==> Restarting the hub"
hub_start kidspc-hub:local "$PORT" "$DATA"
hub_healthy "$PORT" "" 30 || { docker logs kidspc-hub | tail -30; exit 1; }
sleep 3
docker ps --format '{{.Names}}' | grep -q '^kidspc-livekit$' || { echo "The voice server did not stay up:"; docker logs kidspc-livekit | tail -20; exit 1; }

IP="$(ip -4 -br addr show | awk '$1 !~ /^(lo|docker|br-|veth)/ && $2=="UP" {split($3,a,"/"); print a[1]; exit}')"
PUBLIC="$(curl -fs --max-time 5 https://api.ipify.org || echo '<your public IP>')"
if [ "$HTTPS_PORT" = "tunnel" ]; then
cat <<NEXT

Voice server is running and the hub now hands out wss://${DOMAIN}. Two things left:

1. Cloudflare Tunnel: add two routes for  ${DOMAIN}  (order matters only among these two)
     ${DOMAIN}   path ^/rtc                                           ->  http://${IP:-truenas}:7880
     ${DOMAIN}   path ^/(join/|api/voice/join/|static/app\\.css\$|static/fonts/)   ->  http://${IP:-truenas}:${PORT}
   Do NOT add a route without a path: that would put the whole hub on the internet.

2. Router: forward these to ${IP:-this TrueNAS}  (audio never goes through the tunnel)
     UDP 7882  ->  ${IP:-truenas}:7882    (voice audio)
     TCP 7881  ->  ${IP:-truenas}:7881    (voice audio fallback, optional)

Friends connect their audio to your public IP, which right now is ${PUBLIC}.
Then create an invite on the dashboard's Voice tab and open the link on a phone that is NOT on your Wi-Fi.

To undo all of it:  sudo docker rm -f kidspc-livekit   and remove the tunnel routes and router forwards.
NEXT
exit 0
fi
cat <<NEXT

Voice server and HTTPS are running. Two things left, both yours to do:

1. DNS: point  ${DOMAIN}  at your public IP  ${PUBLIC}
   (an A record at your registrar, or dynamic DNS if your IP changes)

2. Router: forward these to ${IP:-this TrueNAS}
     TCP 443   ->  ${IP:-truenas}:${HTTPS_PORT}    (HTTPS: invite pages and voice signalling)
     UDP 7882  ->  ${IP:-truenas}:7882    (voice audio)
     TCP 7881  ->  ${IP:-truenas}:7881    (voice audio fallback, optional)

Once both are in place Caddy fetches its certificate by itself (about a minute). Check with:
   sudo docker logs --tail 20 kidspc-caddy
Then create an invite on the dashboard's Voice tab and open the link on a phone that is NOT on your Wi-Fi.

To undo all of it:  sudo docker rm -f kidspc-livekit kidspc-caddy   and remove the router forwards.
NEXT
