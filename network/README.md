# Network filter: AdGuard Home

AdGuard Home runs in the compose stack (`deploy/docker-compose.yml`). It answers DNS for the whole
house, but the kid PCs get their own rules and their own query log, which the hub imports.

GUESS: AdGuard Home over Pi-hole, because it has per-client rules, built-in SafeSearch enforcement and
"blocked services" toggles (TikTok, Roblox, etc.) without extra tooling.

## One-time setup

1. **Fixed addresses.** On the router, give each kid PC a DHCP reservation (e.g. Adam 192.168.1.50,
   Barrett 192.168.1.51). AdGuard identifies clients by IP.
2. **DNS for the house.** Point the router's DHCP DNS server at the TrueNAS IP. If you only want to
   filter the kid PCs, instead set DNS on each kid PC's network adapter to the TrueNAS IP (the kid's
   standard account can't change it).
3. **Block DNS escape.** On the router, block outbound port 53 and 853 from the kid PCs to anything
   except the TrueNAS, if the router supports it. Chrome's own DNS-over-HTTPS is already off by policy.
4. **Clients.** In AdGuard → Settings → Client settings, add `adam-pc` and `barrett-pc` with their IPs.
   The names must match `adguard_client` in `hub/config.yaml`. For each:
   - Use own settings: on
   - Parental control: on. Safe search: on (all engines).
   - Blocked services: pick what you don't want (see `adguard-clients.example.yaml`).
   - Blocklists: AdGuard DNS filter, plus a family list such as "OISD NSFW".
5. **Split DNS for voice chat.** Settings → DNS rewrites: `voice.example.com → <TrueNAS LAN IP>`.
   That lets the kids' PCs reach the voice server inside the house without going out through the router.
6. **Hub access.** Create an AdGuard user/password and put it in `hub/config.yaml` under `adguard:`.

`adguard-clients.example.yaml` shows the `clients:` section as it appears in `AdGuardHome.yaml`, if
you'd rather edit the file than click through the UI (stop the container first).
