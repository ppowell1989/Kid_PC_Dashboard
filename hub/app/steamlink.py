"""Which Steam account each kid's PC uses, and which games that account owns.

The kid's PC tells the hub which Steam account is signed in there (from Steam's own files), so nobody has to type
it. With a Steam Web API key (free, from steamcommunity.com/dev/apikey, kept only on the hub) the hub reads that
account's game list from Steam and can say, before anything is tried, "Adam's Steam account doesn't own this".
A parent can also type an account in (a profile link, a SteamID or a custom URL name) for a PC that isn't set up.

Owned games are a hint, never a gate: a game shared through Steam Family isn't in the kid's own list but still
installs, so the PC still tries; the hub only words things more clearly and, for a free game that isn't on the
account yet, has the PC open Steam's own Install box so one press adds it.

Everything stays on the hub: the key, the account ids and the cached game lists live in its kv table.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

from .db import DB

API = "https://api.steampowered.com"
STEAMID64_BASE = 76561197960265728
OWNED_TTL = 20 * 60          # seconds before a game list is read again
LINK_KEY = "steam_link"      # {"api_key": "...", "ids": {kid_id: steamid64}}


def _kv(db: DB, key: str) -> dict:
    row = db.one("SELECT value FROM kv WHERE key=?", (key,))
    try:
        return json.loads(row["value"]) if row else {}
    except ValueError:
        return {}


def _put(db: DB, key: str, value: dict | None) -> None:
    if value is None:
        db.x("DELETE FROM kv WHERE key=?", (key,))
    else:
        db.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def settings(db: DB) -> dict:
    return _kv(db, LINK_KEY)


def has_key(db: DB) -> bool:
    return bool(settings(db).get("api_key"))


def set_key(db: DB, key: str | None) -> None:
    s = settings(db)
    if key:
        s["api_key"] = key
    else:
        s.pop("api_key", None)
    _put(db, LINK_KEY, s)


def set_manual(db: DB, kid_id: str, steamid: str | None) -> None:
    s = settings(db)
    ids = s.setdefault("ids", {})
    if steamid:
        ids[kid_id] = steamid
    else:
        ids.pop(kid_id, None)
    _put(db, LINK_KEY, s)
    _put(db, f"steam_owned:{kid_id}", None)


def seen_on_pc(db: DB, kid_id: str, acct: dict) -> None:
    """The PC said which Steam account is signed in there. Stored only when it changes."""
    if _kv(db, f"steam_seen:{kid_id}") != acct:
        _put(db, f"steam_seen:{kid_id}", acct)


def account(db: DB, kid_id: str) -> dict | None:
    """The Steam account the hub goes by for this kid: one a parent typed in wins over the one seen on the PC."""
    manual = settings(db).get("ids", {}).get(kid_id)
    seen = _kv(db, f"steam_seen:{kid_id}")
    if manual:
        name = seen.get("name") if seen.get("id") == manual else ""
        return {"id": manual, "name": name or "", "login": seen.get("login", "") if seen.get("id") == manual else "", "source": "parent"}
    if seen.get("id"):
        return {**seen, "source": "pc"}
    return None


def owned_info(db: DB, kid_id: str) -> dict:
    """What the hub last read about this kid's games: {appids, count, at, private, error} or {} if never read."""
    acct = account(db, kid_id)
    info = _kv(db, f"steam_owned:{kid_id}")
    if not acct or info.get("steamid") != acct["id"]:
        return {}
    return info


def owned(db: DB, kid_id: str) -> set[str] | None:
    """App ids the kid's Steam account owns, or None when the hub can't tell (no key, no account, private, not read)."""
    info = owned_info(db, kid_id)
    if not info or info.get("private") or "appids" not in info:
        return None
    return set(info["appids"])


# --------------------------------------------------------------------------- talking to Steam

_PROFILE = re.compile(r"steamcommunity\.com/(profiles|id)/([^/?#\s]+)", re.I)


async def resolve(db: DB, text: str) -> str:
    """A profile link, a SteamID64 or a custom URL name -> SteamID64. Raises ValueError in plain words."""
    text = text.strip()
    m = _PROFILE.search(text)
    kind, val = (m.group(1).lower(), m.group(2)) if m else ("", text)
    if val.isdigit() and len(val) == 17 and kind in ("", "profiles"):
        return val
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", val):
        raise ValueError("That doesn't look like a Steam profile link or account id.")
    key = settings(db).get("api_key")
    if not key:
        raise ValueError("Add a Steam Web API key first, or paste the profile link that ends in a long number.")
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{API}/ISteamUser/ResolveVanityURL/v1/", params={"key": key, "vanityurl": val})
        resp = r.json().get("response", {})
    except Exception as e:
        raise ValueError(f"Couldn't reach Steam ({type(e).__name__}).") from e
    if resp.get("success") != 1:
        raise ValueError(f"Steam doesn't know a profile called {val}.")
    return resp["steamid"]


async def fetch_owned(key: str, steamid: str) -> dict:
    """Ask Steam for an account's games. Free games he has played or added count too."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{API}/IPlayerService/GetOwnedGames/v1/", params={
                "key": key, "steamid": steamid, "include_played_free_games": 1, "include_free_sub": 1, "skip_unvetted_apps": 0})
    except Exception as e:
        return {"error": f"Couldn't reach Steam ({type(e).__name__})."}
    if r.status_code in (401, 403):
        return {"error": "Steam turned the API key down. Check it on steamcommunity.com/dev/apikey and paste it again."}
    if r.status_code != 200:
        return {"error": f"Steam answered {r.status_code}."}
    try:
        resp = r.json().get("response", {})
    except ValueError:
        return {"error": "Steam sent something the hub couldn't read."}
    if "games" not in resp:
        # Steam answers an empty list for an account whose game details aren't public.
        return {"private": True}
    return {"appids": sorted({str(g["appid"]) for g in resp["games"]}), "count": resp.get("game_count", len(resp["games"]))}


async def refresh(db: DB, kid_id: str, force: bool = False) -> dict:
    key = settings(db).get("api_key")
    acct = account(db, kid_id)
    if not key or not acct:
        return {}
    info = _kv(db, f"steam_owned:{kid_id}")
    if not force and info.get("steamid") == acct["id"] and time.time() - info.get("at", 0) < OWNED_TTL:
        return info
    got = await fetch_owned(key, acct["id"])
    got.update({"steamid": acct["id"], "at": time.time()})
    if got.get("error") and info.get("steamid") == acct["id"] and "appids" in info:
        got["appids"], got["count"] = info["appids"], info.get("count", 0)   # keep the last good list, say what went wrong
        got["stale"] = True
    _put(db, f"steam_owned:{kid_id}", got)
    return got


async def keep_fresh(db: DB, kid_ids, every: int = 300) -> None:
    """Background: re-read each linked kid's games when the cached list is older than OWNED_TTL."""
    while True:
        for kid_id in list(kid_ids()):
            try:
                await refresh(db, kid_id)
            except Exception:
                pass
        await asyncio.sleep(every)


def steamid_from_account_id(account_id: int) -> str:
    return str(STEAMID64_BASE + account_id)
