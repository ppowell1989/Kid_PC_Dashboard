"""Pulls the AdGuard Home query log into the visits table, per kid client.

Uses AdGuard Home's /control/querylog API. GUESS: field names match AdGuard Home
v0.107; check against your installed version.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .config import Settings
from .db import DB

log = logging.getLogger("hub.adguard")

# Domains that are noise rather than "a site he visited".
NOISE_SUFFIXES = (
    "steamserver.net", "steamcontent.com", "steampowered.com", "blizzard.com", "battle.net",
    "microsoft.com", "windowsupdate.com", "msftconnecttest.com", "nvidia.com", "gstatic.com",
    "googleapis.com", "doubleclick.net", "local", "arpa",
)


def is_noise(domain: str) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in NOISE_SUFFIXES)


def import_entries(db: DB, settings: Settings, entries: list[dict]) -> int:
    by_client = {k.adguard_client: k.id for k in settings.kids if k.adguard_client}
    last = (db.one("SELECT value FROM kv WHERE key='adguard_last'") or {}).get("value", "")
    newest = last
    n = 0
    for e in entries:
        at = e.get("time", "")
        if at <= last:
            continue
        newest = max(newest, at)
        client = (e.get("client_info") or {}).get("name") or e.get("client", "")
        kid_id = by_client.get(client)
        domain = ((e.get("question") or {}).get("name") or "").rstrip(".").lower()
        if not kid_id or not domain or is_noise(domain):
            continue
        blocked = 1 if str(e.get("reason", "")).startswith("Filtered") else 0
        db.x("INSERT INTO visits(kid_id,source,domain,blocked,at) VALUES (?,?,?,?,?)",
             (kid_id, "dns", domain, blocked, at))
        n += 1
    if newest != last:
        db.x("INSERT INTO kv(key,value) VALUES('adguard_last',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (newest,))
    return n


async def poll_forever(db: DB, settings: Settings) -> None:
    cfg = settings.adguard
    async with httpx.AsyncClient(base_url=cfg.url, auth=(cfg.username, cfg.password), timeout=20) as client:
        while True:
            try:
                r = await client.get("/control/querylog", params={"limit": 500})
                r.raise_for_status()
                n = import_entries(db, settings, r.json().get("data", []))
                if n:
                    log.info("imported %d dns visits", n)
            except Exception as exc:  # keep polling through AdGuard restarts
                log.warning("adguard poll failed: %s", exc)
            await asyncio.sleep(cfg.poll_seconds)
