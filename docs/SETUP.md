# Setup, start to finish

Target: both PCs working by about 20 December, leaving a few days of slack before Christmas.

## 1. TrueNAS hub (an evening)

1. Create datasets: `apps/kidspc/hub`, `apps/kidspc/adguard/{work,conf}`, `apps/kidspc/caddy`.
2. Copy this repo to the TrueNAS (or clone it once there is a GitHub repo).
3. `cp hub/config.example.yaml /mnt/<pool>/apps/kidspc/hub/config.yaml` and fill in:
   - parent passwords: `python -m app.hashpw 'password'` (or `docker compose run --rm hub python -m app.hashpw 'password'`)
   - a random `device_token` and `extension_token` per kid, and `session_secret`
   - LiveKit key and secret (same values in `voice/livekit.yaml`)
4. In `deploy/docker-compose.yml`, change `tank` to your pool name, then `docker compose up -d --build`
   from `deploy/` (or paste it into TrueNAS SCALE → Apps → Custom App → Install via YAML).
5. Open `http://<truenas>:8080` on your phone, sign in, add games on the Games tab.
6. AdGuard: `http://<truenas>:3000`, then follow `network/README.md`.

## 2. Each PC (about an hour each, after the hardware is built)

1. Install Windows 11 with a **local parent admin account**. Setup pushes hard for a Microsoft account
   and the workarounds change between releases; check the current method when you install (on Pro,
   look for a "domain join instead" or offline-account option). GUESS: Windows 11 Pro, for the policy
   and app control options.
2. Install GPU drivers, Chrome, Steam and Battle.net as the admin.
3. Steam: create a Steam account for the kid inside your **Steam Family**, with store and library
   restrictions on. Battle.net: create a child account under your Blizzard parental controls.
4. Build the Windows code on any Windows PC with the .NET 8 SDK: `windows\build.ps1`.
5. Run `windows\setup\Install-KidPc.ps1` (see `windows/README.md`).
6. Sign in as the kid once, sign out, run the script again with `-ShellOnly`.
7. Run Sysinternals **Autologon** for the kid's account. Reboot. The shell should appear.
8. Sign in to Steam and Battle.net once as the kid, with "remember me", so the launchers start signed in.

## 3. Voice chat from outside the house (needs your go-ahead: outward-facing)

1. Pick a domain (or a free dynamic-DNS name) for voice, e.g. `voice.<yourname>.net`.
2. Router: forward 443/TCP, 80/TCP and 7882/UDP (optionally 7881/TCP) to the TrueNAS.
3. Put the domain in `deploy/Caddyfile` and `hub/config.yaml`, restart the stack.
4. AdGuard DNS rewrite for the domain → TrueNAS LAN IP (so it works from inside the house too).
5. Create an invite on the Voice tab and test it from a phone on mobile data.

## 4. Chrome reporting extension (needs your go-ahead: publishes to Google)

See `chrome/README.md`. Upload as an unlisted Chrome Web Store item, then re-run
`Set-ChromePolicy.ps1 -ExtensionId <id>`. Allow a week for review.

## 5. Before wrapping the boxes

- [ ] Time runs down while a game is open and stops on the home screen
- [ ] 10-minute warning shows over a full-screen game
- [ ] Lock screen appears at zero; the game is closed within 30 seconds
- [ ] "Ask for more time" shows up on your phone; approving it unlocks the PC
- [ ] An unapproved Steam game is closed when started
- [ ] Ctrl+Alt+Del offers no Task Manager; there is no way to reach a desktop
- [ ] Chrome: no incognito, SafeSearch forced, visits show on the Web tab
- [ ] Pull the network cable: PC keeps counting down and locks at zero
- [ ] Voice: kid's window and a friend's phone can hear each other; revoking kicks the friend
