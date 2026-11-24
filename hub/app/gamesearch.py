"""One game search across stores, for the parent dashboard.

A parent types a name and gets one row per game, with cover art, the ESRB rating, and the
platforms it is on. Each platform carries the launcher and launch reference the shell needs,
so nobody has to know a Steam app id or a Battle.net product code.

Sources, none of which needs an account or an API key:
  Steam      the public store search (in main.py), with appdetails for the rating
  Battle.net a short curated list below: Blizzard's PC catalogue is small and has no public search
  Xbox       the PC Game Pass catalogue: Microsoft's public "sigl" list of product ids, plus the
             public display catalogue for titles, art and ratings. Cached for a week.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata

import httpx

from .db import DB

# Battle.net product codes, as used by Battle.net.exe --exec="launch <code>".
# GUESS on some codes and folders: extend here and in windows/src/KidsPc.Helper/GameLibrary.cs together.
# steam: the same game's Steam app id, where it has one, so results merge and get Steam's art.
BATTLENET = [
    {"code": "WoW",  "title": "World of Warcraft",          "esrb": "T", "aliases": ["wow", "warcraft", "wow retail"]},
    # Battle.net shows these as "Game Version" choices under World of Warcraft; here each one is its own game, so
    # parents approve exactly the version they mean. GUESS: "WoWF" is a fan-reported code; Blizzard hasn't published it.
    {"code": "WoWF", "title": "World of Warcraft: Forever",  "esrb": "T", "aliases": ["wow forever", "warcraft forever", "wow"]},
    {"code": "Pro",  "title": "Overwatch 2",                 "esrb": "T", "steam": "2357570", "aliases": ["overwatch"]},
    {"code": "WTCG", "title": "Hearthstone",                 "esrb": "T"},
    {"code": "Hero", "title": "Heroes of the Storm",         "esrb": "T"},
    {"code": "S2",   "title": "StarCraft II",                "esrb": "T", "aliases": ["starcraft 2", "sc2"]},
    {"code": "S1",   "title": "StarCraft: Remastered",       "esrb": "T", "aliases": ["starcraft"]},
    {"code": "W3",   "title": "Warcraft III: Reforged",      "esrb": "T", "aliases": ["warcraft 3"]},
    {"code": "D3",   "title": "Diablo III",                  "esrb": "M", "aliases": ["diablo 3"]},
    {"code": "OSI",  "title": "Diablo II: Resurrected",      "esrb": "M", "aliases": ["diablo 2"]},
    {"code": "Fen",  "title": "Diablo IV",                   "esrb": "M", "steam": "2344520", "aliases": ["diablo 4"]},
    {"code": "ANBS", "title": "Diablo Immortal",             "esrb": "M"},
    {"code": "WLBY", "title": "Crash Bandicoot 4: It's About Time", "esrb": "E10+", "steam": "1378990", "aliases": ["crash bandicoot"]},
    {"code": "RTRO", "title": "Blizzard Arcade Collection",  "esrb": "E10+"},
]
BATTLENET_BY_CODE = {g["code"].lower(): g for g in BATTLENET}

GAMEPASS_PC_LIST = "fdd9e2a7-0fee-49f6-ad69-4354098401ff"   # Microsoft's public id for the PC Game Pass list
XBOX_CACHE_SECONDS = 7 * 24 * 3600
XBOX_KEY = "xbox:gamepass_pc"
_xbox_lock = asyncio.Lock()


def norm(title: str) -> str:
    """Comparable title: no accents, ™®©, punctuation or case. 'Diablo® IV' == 'diablo iv'."""
    t = re.sub(r"['\u2019]", "", title)   # "It's" and "It’s" both become "its"
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def matches(q: str, title: str, aliases: list[str] = ()) -> bool:
    words = norm(q).split()
    if not words:
        return False
    for t in (title, *aliases):
        n = f" {norm(t)} "
        if all(f" {w}" in n for w in words):   # every typed word starts a word of the title
            return True
    return False


def battlenet_search(q: str) -> list[dict]:
    return [g for g in BATTLENET if matches(q, g["title"], g.get("aliases", []))]


# Xbox / PC Game Pass ---------------------------------------------------------

def _xbox_image(props: dict) -> str:
    imgs = {i.get("ImagePurpose"): i.get("Uri", "") for i in props.get("Images") or []}
    for purpose in ("TitledHeroArt", "SuperHeroArt", "BoxArt", "Poster", "Tile"):
        uri = imgs.get(purpose)
        if uri:
            return ("https:" + uri if uri.startswith("//") else uri) + "?w=460"
    return ""


def xbox_product(p: dict) -> dict | None:
    """The fields we use from one display-catalog product."""
    loc = (p.get("LocalizedProperties") or [{}])[0]
    title = loc.get("ProductTitle") or ""
    if not title:
        return None
    esrb = ""
    for mp in p.get("MarketProperties") or []:
        for r in mp.get("ContentRatings") or []:
            if r.get("RatingSystem") == "ESRB":
                esrb = (r.get("RatingId") or "").split(":")[-1].upper()   # "ESRB:E10" -> "E10"
    esrb = {"E10": "E10+", "EC": "EC", "RP": ""}.get(esrb, esrb)
    return {"id": p.get("ProductId", ""), "title": title, "image_url": _xbox_image(loc), "esrb": esrb}


async def _fetch_gamepass(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get("https://catalog.gamepass.com/sigls/v2", params={"id": GAMEPASS_PC_LIST, "language": "en-us", "market": "US"})
    ids = [i["id"] for i in r.json() if isinstance(i, dict) and i.get("id")]
    sem = asyncio.Semaphore(4)

    async def batch(chunk: list[str]) -> list[dict]:
        async with sem:
            r = await client.get("https://displaycatalog.mp.microsoft.com/v7.0/products",
                                 params={"bigIds": ",".join(chunk), "market": "US", "languages": "en-us"})
            return [x for x in map(xbox_product, r.json().get("Products") or []) if x]

    chunks = [ids[i:i + 20] for i in range(0, len(ids), 20)]
    return [g for part in await asyncio.gather(*(batch(c) for c in chunks)) for g in part]


async def gamepass_catalogue(db: DB) -> list[dict] | None:
    """PC Game Pass games, cached for a week. None if Microsoft can't be reached and nothing is cached."""
    row = db.one("SELECT value FROM kv WHERE key=?", (XBOX_KEY,))
    cached = json.loads(row["value"]) if row else None
    if cached and time.time() - cached["_at"] < XBOX_CACHE_SECONDS:
        return cached["games"]
    async with _xbox_lock:
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                games = await asyncio.wait_for(_fetch_gamepass(c), timeout=20)
        except Exception:
            games = []
        if games:
            db.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (XBOX_KEY, json.dumps({"_at": time.time(), "games": games})))
            return games
    return cached["games"] if cached else None   # stale is better than nothing


def xbox_search(q: str, catalogue: list[dict]) -> list[dict]:
    return [g for g in catalogue if matches(q, g["title"])][:8]


# Merging ---------------------------------------------------------------------

def merge(q: str, steam: list[dict], battlenet: list[dict], xbox: list[dict], game_pass_enabled: bool, in_catalogue: dict) -> list[dict]:
    """One row per game, with every platform it's on. The first addable platform is the default."""
    rows: dict[str, dict] = {}

    def row(title: str) -> dict:
        k = norm(title)
        if k not in rows:
            rows[k] = {"title": title, "image_url": "", "esrb": "", "adult": False, "details": {}, "platforms": []}
        return rows[k]

    def add_platform(r: dict, platform: str, launcher: str, ref: str, addable: bool = True, note: str = "") -> None:
        if any(p["platform"] == platform for p in r["platforms"]):
            return
        r["platforms"].append({"platform": platform, "label": LABELS[platform], "launcher": launcher, "launch_ref": ref,
                               "addable": addable, "note": note, "game_id": in_catalogue.get((launcher, ref))})

    steam_by_id = {}
    for s in steam:
        if s.get("type") not in (None, "", "game"):
            continue   # DLC, soundtracks, demos
        r = row(s["name"])
        r.update(image_url=s.get("image_url", ""), esrb=s.get("esrb", ""), adult=bool(s.get("adult")))
        r["details"] = {k: s.get(k) for k in ("description", "esrb_descriptors", "content_notes", "free", "price", "appid")}
        add_platform(r, "steam", "steam", s["appid"])
        steam_by_id[s["appid"]] = r
    for b in battlenet:
        r = steam_by_id.get(b.get("steam")) or row(b["title"])
        if not r["esrb"]:
            r["esrb"] = b["esrb"]
        add_platform(r, "battlenet", "battlenet", b["code"])
    for x in xbox:
        r = row(x["title"])
        if not r["image_url"]:
            r["image_url"] = x["image_url"]
        if not r["esrb"]:
            r["esrb"] = x["esrb"]
        add_platform(r, "xbox", "gamepass", x["id"], addable=game_pass_enabled,
                     note="" if game_pass_enabled else "Game Pass is switched off on the hub, so this one can't be added yet")

    out = []
    for r in rows.values():
        r["platforms"].sort(key=lambda p: (not p["addable"], ORDER.index(p["platform"])))
        r["default"] = 0 if r["platforms"] and r["platforms"][0]["addable"] else None
        out.append(r)
    # Closest title first: exact, then starts-with, then the rest in source order.
    nq = norm(q)
    out.sort(key=lambda r: (norm(r["title"]) != nq, not norm(r["title"]).startswith(nq)))
    return out[:12]


LABELS = {"steam": "Steam", "battlenet": "Battle.net", "xbox": "PC Game Pass"}
ORDER = ["steam", "battlenet", "xbox"]   # preferred launcher when a game is on several
