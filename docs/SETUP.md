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

## 2. Each PC (about 20 minutes each, mostly downloads)

1. Install Windows 11 and finish its setup with a **parent admin account**. Your Microsoft account
   is fine for this parent account; the kid accounts are local and are made by step 3.
   Let Windows Update finish (it brings the NVIDIA driver), and plug in Ethernet if you can.
2. On your phone, dashboard: the kid's card, **Set up a new PC**. It shows one line, good for 12 hours.
3. On the new PC, right-click Start, **Terminal (Admin)**, type or paste that line, press Enter:

   ```powershell
   irm http://192.168.4.54:8088/setup/ABCD2345 | iex
   ```

   It asks for a password for the kid's account (he only needs it if he signs out), then by itself:
   downloads the newest build from the hub (x64 or ARM, picked automatically), makes the kid's
   standard account and profile, installs the shell and helper, applies the Chrome policies and
   lockdown, turns on automatic sign-in, waits for the helper to install Steam and Chrome, checks
   everything, and restarts into the kid's screen. Re-running the same line is safe.
4. The dashboard shows the PC online with its build within a minute.
5. Sign in to Steam and Battle.net once as the kid, with "remember me". Steam: a child account
   in your **Steam Family**. Battle.net: a child account under Blizzard parental controls.

The old manual way still works: `windows\setup\Install-KidPc.ps1 -KidUser adam -KidName Adam -HubUrl ...
-DeviceToken ... -ExtensionToken ... -AutoLogon` from the unzipped CI bundle. CI runs the one-line path
on a clean Windows machine on every push (`windows/tests/FreshPc.ps1`).

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
