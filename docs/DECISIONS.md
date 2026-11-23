# Decisions

## Still open (switches in `hub/config.yaml` → `decisions:`)

| Question | Switch | What each setting does | Default |
|---|---|---|---|
| Chrome: allow-list or filtered web? | `web_mode` | `allowlist`: helper sets Chrome to block everything except the hub's allowed sites; kids can ask for a site. `filtered`: open web, AdGuard blocklists + forced SafeSearch/YouTube Restricted. | `filtered` |
| Game Pass? | `game_pass_enabled` | `false`: Game Pass titles are hidden everywhere and never launched. `true`: shown and launched via the Xbox app, which needs a Microsoft account on the PC. | `false` |
| Verify friends' parents? | `friend_verification` | `none`: a parent makes the link and sends it. `discord_oauth`: link refuses to work until the friend's parent signs in with Discord. **The Discord sign-in itself is not built yet.** | `none` |
| Hub away from home? | `remote_access` | `lan_only`: dashboard refuses non-home IPs. `tailscale`: also allows Tailscale (install the Tailscale app on TrueNAS and your phones; nothing exposed to the internet). `public`: allow anything (only behind HTTPS + password; not recommended). | `lan_only` |

Friend `/join` links work from anywhere regardless of `remote_access`; that's the point of them.

## Choices made while scaffolding (guesses, easy to change)

| Area | Choice | Why |
|---|---|---|
| Hub | Python 3.12, FastAPI, SQLite, plain JS dashboard | Small, one container, one file to back up, no build step. |
| Shell | .NET 8 WPF, per-user `Winlogon\Shell` | Works on Windows Pro (Shell Launcher needs Enterprise/Education). |
| Helper | .NET 8 Worker Service as LocalSystem | The only place with admin rights and the hub token. |
| Shell ↔ helper | Named pipe, JSON lines | Local only; helper re-checks every request against hub rules. |
| Time counting | A minute counts when a game or Chrome is running in the kid's session | Sitting on the home screen doesn't use up time. |
| Offline | Fail closed: count down locally, lock at zero | A pulled cable shouldn't mean unlimited time. |
| Weekends | Sat/Sun get weekend minutes; Fri/Sat nights get the weekend bedtime | Edit `hub/app/rules.py`. |
| Network filter | AdGuard Home | Per-client rules and query log API. |
| Voice | LiveKit + Caddy, one room per kid, tokens only from the hub | Revocable links, nothing to install for friends. |
| Ports | 443/TCP + 7882/UDP (+7881/TCP optional) | WebRTC audio needs its own port; the plan's "one port" was too optimistic. |
| Extension | Unlisted Chrome Web Store item | Chrome on Windows won't force-install off-store extensions on a non-managed PC. |
