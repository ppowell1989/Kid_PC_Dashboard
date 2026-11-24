# Voice chat: LiveKit

LiveKit (open source, self-hosted) runs in the compose stack. The hub issues every access token, so
nobody can join without a link from you.

- **One room per kid** (`room-adam`, `room-barrett`). The kid's "Chat with a friend" tile opens a
  small always-on-top window with his room. Friends open their invite link in any browser and join it.
- **Invite links** are created and revoked on the dashboard's Voice tab. Revoking a link stops it
  working immediately and kicks that friend out of the room if they're in it.
- **Locked PC means no voice.** The helper refuses a voice token while time is up, and the shell
  closes the voice window when the lock screen appears.
- Voice only: tokens grant audio publish/subscribe; the pages never ask for a camera.

## Quick setup on the TrueNAS test install

```
sudo bash deploy/truenas-voice-setup.sh <pool> <domain> [hub_port] [https_port | tunnel]
# with an existing Cloudflare Tunnel (what the home install uses):
sudo bash deploy/truenas-voice-setup.sh NAS voice.example.com 8088 tunnel
```

It starts LiveKit (`kidspc-livekit`), points the hub at `wss://<domain>`, and prints what is left to do.

**With `tunnel`** nothing else is started. Add two routes for the domain to the tunnel (Cloudflare
dashboard, Networking > Tunnels > the tunnel > Routes):

| Path (a regular expression) | Service |
| --- | --- |
| `^/rtc` | `http://<truenas>:7880` |
| `^/(join/|api/voice/join/|static/app\.css$|static/fonts/)` | `http://<truenas>:<hub_port>` |

Never add a route for that domain without a path: it would put the whole hub on the internet. As a
second lock, the hub treats anything that arrives through Cloudflare as an outside visitor, so only the
join pages answer. The router then needs two forwards for the audio itself, which can't go through a
tunnel: **7882/UDP** and **7881/TCP**. Signalling goes through Cloudflare, so the kids' PCs reach it from
inside the house without any router loopback setting.

**With a port** (default 8443) the script also starts its own Caddy (`kidspc-caddy`) with a Let's Encrypt
certificate. You then need a DNS record for the domain pointing at the home's public IP, and the router
maps public 443 to that port, plus the two audio forwards. Don't use this when another reverse proxy
already owns the router's 443 forward; add the domain to that proxy or use the tunnel.

Nothing is reachable from outside until the routes (or DNS record) and the forwards exist, and
`docker rm -f kidspc-livekit kidspc-caddy` plus removing them undoes it.

Known wrinkle, own-Caddy setup only: the kids' PCs are inside the house but connect to the public name.
Most routers handle that ("NAT loopback" or "hairpin NAT"). If the kid's side can't connect while friends
outside can, that is the cause.

## What has to be outward-facing

Browsers only allow the microphone on HTTPS pages, and friends connect from outside the house, so:

1. A domain name pointed at your home's public IP (dynamic DNS is fine). Put it in
   `deploy/Caddyfile`, `hub/config.yaml` (`livekit.url`, `public_join_base`).
2. Router port forwards to the TrueNAS: **443/TCP** (Caddy: HTTPS, invite pages and LiveKit signalling),
   **7882/UDP** (LiveKit audio), and optionally **7881/TCP** (audio fallback for strict networks).
   This is two to three ports, not the one in the original plan, because WebRTC audio runs on its own port.
   80/TCP is needed briefly for Let's Encrypt.
3. Caddy only exposes `/join/*`, the join API and LiveKit. The parent dashboard is never routed there.

`friend_verification: discord_oauth` is wired as a switch: invites are refused until verified, but the
Discord sign-in flow itself is a TODO in `hub/app/main.py` (`voice_join`).
