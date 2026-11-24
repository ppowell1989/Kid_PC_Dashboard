# Kids PC

Parental controls for Adam's and Barrett's gaming PCs (Christmas 2026). A custom Windows shell on each
PC, a small hub on the TrueNAS, and a phone dashboard for Patrick and Barbara.

```
 Kid PC (Windows 11)                                  TrueNAS (Docker)
 ┌───────────────────────────────┐                    ┌──────────────────────────────┐
 │ KidsPcShell  (kid's shell)    │   named pipe       │ hub      FastAPI + SQLite    │◄── parents' phones
 │   tiles, time, lock, requests ├──────┐             │          dashboard :8080     │    (home Wi-Fi)
 │ KidsPcHelper (SYSTEM service) │◄─────┘  HTTP       │ adguard  DNS filter + log    │
 │   rules, enforcement, installs├───────────────────►│ livekit  voice rooms         │◄── friends' browsers
 │ Chrome (policy-locked)        │                    │ caddy    HTTPS for /join     │    (via 443 + 7882/udp)
 │   + reporter extension        ├───────────────────►│                              │
 └───────────────────────────────┘                    └──────────────────────────────┘
```

| Folder | What |
|---|---|
| `hub/` | Python FastAPI service: rules, time accounting, requests, web history, voice invites, family (brothers see each other, join the same game, family voice room), parent dashboard. Tested (`pytest`). |
| `windows/` | .NET 8 kid shell (WPF) and admin helper (Windows service), plus PowerShell setup. |
| `chrome/` | Chrome lockdown policies and the page-reporting extension. |
| `network/` | AdGuard Home per-kid setup. |
| `voice/` | LiveKit config and notes on what must face the internet. |
| `deploy/` | `docker-compose.yml` and `Caddyfile` for TrueNAS. |
| `docs/` | `SETUP.md` (step by step) and `DECISIONS.md` (open decisions and stack guesses). |

## The four open decisions are switches

All in `hub/config.yaml` under `decisions:`. Nothing is settled in code.

| Switch | Values | Default |
|---|---|---|
| `web_mode` | `allowlist` / `filtered` | `filtered` |
| `game_pass_enabled` | `true` / `false` | `false` |
| `friend_verification` | `none` / `discord_oauth` | `none` |
| `remote_access` | `lan_only` / `tailscale` / `public` | `lan_only` |

## Status

- Hub: working, 13 tests pass, dashboard checked in a phone-sized browser.
- Windows shell and helper: written, **not yet compiled** (no .NET SDK where it was written).
- Chrome extension: written; needs to be published as an unlisted Chrome Web Store item to force-install.
- Discord verification: switch exists, sign-in flow is a TODO.

Run the hub locally:

```bash
cd hub && python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
pytest
cp config.example.yaml config.yaml   # edit; hash passwords with: python -m app.hashpw 'pw'
HUB_CONFIG=config.yaml uvicorn app.main:app --port 8080
```
