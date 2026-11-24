#!/usr/bin/env bash
# Friend voice chat for the TrueNAS test install: a LiveKit voice server plus Caddy for HTTPS.
# Run from the repo root, after deploy/truenas-test-setup.sh:
#   sudo bash deploy/truenas-voice-setup.sh <pool> <domain> [hub_port] [https_port]
#
# What becomes reachable from the internet (and nothing else):
#   https://<domain>/join/...  the friend's invite page (and the one API call it makes)
#   https://<domain>/rtc       LiveKit signalling
#   7882/udp, 7881/tcp         LiveKit audio
# The parent dashboard is NOT routed through Caddy; it stays home-network only.
#
# You still have to do two things by hand (the script prints them at the end):
#   1. Point <domain> at your home's public IP (an A record, or dynamic DNS).
#   2. Forward three ports on the router to this machine.
# Safe to re-run.
set -euo pipefail
POOL="${1:?usage: sudo bash deploy/truenas-voice-setup.sh <pool> <domain> [hub_port] [https_port]}"
DOMAIN="${2:?give the domain friends will use, e.g. voice.example.com}"
PORT="${3:-8088}"
HTTPS_PORT="${4:-8443}"            # TrueNAS's own web UI already uses 443, so Caddy listens here
LIVEKIT_IMAGE="${LIVEKIT_IMAGE:-livekit/livekit-server:v1.8.0}"   # GUESS: a newer v1.x tag is fine too
CADDY_IMAGE="${CADDY_IMAGE:-caddy:2}"
DATA="/mnt/$POOL/apps/kidspc-data"
CFG="$DATA/hub/config.yaml"

[[ "$DOMAIN" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}$ ]] || { echo "\"$DOMAIN\" doesn't look like a domain name"; exit 1; }
[ -f "$CFG" ] || { echo "No $CFG yet. Run deploy/truenas-test-setup.sh first."; exit 1; }
docker image inspect kidspc-hub:local >/dev/null 2>&1 || { echo "Hub image missing. Run deploy/truenas-test-setup.sh first."; exit 1; }

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

cat > "$DATA/voice/Caddyfile" <<CADDY
# Public face: only the friend invite pages and LiveKit signalling. Everything else is a 404.
${DOMAIN} {
	@livekit path /rtc*
	handle @livekit {
		reverse_proxy host.docker.internal:7880
	}
	@friend path /join/* /api/voice/join/* /static/app.css
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

echo "==> Pointing the hub at wss://$DOMAIN"
sed -i "/^livekit:/,/^[^ #]/ {
  s|^  url: .*|  url: wss://${DOMAIN}|
  s|^  api_url: .*|  api_url: http://host.docker.internal:7880|
  s|^  public_join_base: .*|  public_join_base: https://${DOMAIN}|
}" "$CFG"

echo "==> Starting the voice server and HTTPS"
docker rm -f kidspc-livekit kidspc-caddy >/dev/null 2>&1 || true
docker run -d --name kidspc-livekit --restart unless-stopped --network host \
  -v "$DATA/voice/livekit.yaml:/etc/livekit.yaml:ro" "$LIVEKIT_IMAGE" --config /etc/livekit.yaml >/dev/null
docker run -d --name kidspc-caddy --restart unless-stopped -p "$HTTPS_PORT:443" \
  --add-host host.docker.internal:host-gateway \
  -v "$DATA/voice/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$DATA/caddy:/data" "$CADDY_IMAGE" >/dev/null

echo "==> Restarting the hub"
docker rm -f kidspc-hub >/dev/null 2>&1 || true
docker run -d --name kidspc-hub --restart unless-stopped -p "$PORT:8080" \
  --add-host host.docker.internal:host-gateway -v "$DATA/hub:/data" kidspc-hub:local >/dev/null
for _ in $(seq 30); do curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 1; done
curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null || { docker logs kidspc-hub | tail -30; exit 1; }
sleep 3
docker ps --format '{{.Names}}' | grep -q '^kidspc-livekit$' || { echo "The voice server did not stay up:"; docker logs kidspc-livekit | tail -20; exit 1; }

IP="$(ip -4 -br addr show | awk '$1 !~ /^(lo|docker|br-|veth)/ && $2=="UP" {split($3,a,"/"); print a[1]; exit}')"
PUBLIC="$(curl -fs --max-time 5 https://api.ipify.org || echo '<your public IP>')"
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
