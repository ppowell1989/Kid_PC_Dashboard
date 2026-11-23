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
