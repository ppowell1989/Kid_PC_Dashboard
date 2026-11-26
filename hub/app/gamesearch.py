"""One game search across stores, for the parent dashboard.

A parent types a name and gets one row per game, with cover art, the ESRB rating, and the
platforms it is on. Each platform carries the launcher and launch reference the shell needs,
so nobody has to know a Steam app id or a Battle.net product code.

Sources, none of which needs an account or an API key:
  Steam      the public store search (in main.py), with appdetails for the rating
  Battle.net a short curated list below: Blizzard's PC catalogue is small and has no public search
  Epic       a short curated list of Epic's own games, plus the games on each kid's Epic account, which the
             kid's PC reads from the Epic launcher's library cache and sends with its heartbeat. Epic's store
             search sits behind bot protection, so the hub doesn't call it.
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
    # Battle.net shows World of Warcraft's versions as "Game Version" choices under one game. Here each version is
    # its own game, so parents approve exactly the one they mean. Each has its own install folder (GameLibrary.cs).
    # GUESS: only "WoW" (retail) is a known Battle.net code; the others are best guesses until the test PC's
    # Battle.net reports its own list (GameDeployer reports it to the activity feed as battlenet_products).
    {"code": "WoW",   "title": "World of Warcraft (Midnight, current)",            "esrb": "T", "aliases": ["wow", "warcraft", "retail", "midnight"]},
    {"code": "WoWF",  "title": "World of Warcraft: Forever",                       "esrb": "T", "aliases": ["wow forever", "warcraft forever", "wow"]},
    {"code": "WoWC",  "title": "World of Warcraft: Mists of Pandaria Classic",     "esrb": "T", "aliases": ["wow classic", "mists classic", "mop classic", "wow"]},
    {"code": "WoWCE", "title": "World of Warcraft Classic Era (Hardcore, Season of Discovery)", "esrb": "T",
     "aliases": ["wow classic", "classic era", "vanilla", "hardcore", "season of discovery", "sod", "wow"]},
    {"code": "WoWA",  "title": "World of Warcraft: Burning Crusade Classic Anniversary", "esrb": "T",
     "aliases": ["wow classic", "tbc", "burning crusade", "anniversary", "wow"]},
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

# Epic games by their launcher "app name", as in com.epicgames.launcher://apps/<app>?action=launch. These are Epic's
# own games, which an under-13 (cabined) Epic account may download even before a parent has given consent.
# Fortnite and Sugar (Rocket League) are long-known app names. GUESS: Fall Guys' id, until a PC reports its library.
EPIC = [
    {"app": "Fortnite", "title": "Fortnite", "esrb": "T",
     "aliases": ["lego fortnite", "fortnite festival", "rocket racing", "fortnite battle royale"]},
    # Rocket League and Fall Guys left the Steam store for new players, so Epic is the default store for them.
    {"app": "Sugar", "title": "Rocket League", "esrb": "E", "steam": "252950", "prefer": True, "aliases": ["rocket league", "rl"]},
    {"app": "0a2d9f6403244d12969e11da6713137b", "title": "Fall Guys", "esrb": "E", "steam": "1097150", "prefer": True, "aliases": ["fall guys"]},
]
EPIC_BY_APP = {g["app"].lower(): g for g in EPIC}
EPIC_APP_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

STEAM_ART = "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{}/header.jpg"

# Cover art for curated games that aren't on Steam, wide like a Steam header (tiles crop to fill).
# Each was looked at by a person and is checked by CI (hub/tests/art_audit.py). Key: "launcher:ref", lower case.
# Blizzard's own pages (blz-contentstack) and the Microsoft Store catalogue. Every World of Warcraft version shows
# WoW's key art: Blizzard has no separate wide art for the Classic versions, and the tile's title says which one.
# Blizzard Arcade Collection has no art anywhere we can link to: it gets the plain tile.
ART: dict[str, str] = {
    "battlenet:wow": "https://blz-contentstack-images.akamaized.net/v3/assets/blt9c12f249ac15c7ec/bltb5a24e5ab1e2cfb0/6a88e3589b942efdb6f74110/wow-thumbnail-homepage.jpg",
    "battlenet:wowf": "https://blz-contentstack-images.akamaized.net/v3/assets/blt9c12f249ac15c7ec/bltb5a24e5ab1e2cfb0/6a88e3589b942efdb6f74110/wow-thumbnail-homepage.jpg",
    "battlenet:wowc": "https://blz-contentstack-images.akamaized.net/v3/assets/blt9c12f249ac15c7ec/bltb5a24e5ab1e2cfb0/6a88e3589b942efdb6f74110/wow-thumbnail-homepage.jpg",
    "battlenet:wowce": "https://blz-contentstack-images.akamaized.net/v3/assets/blt9c12f249ac15c7ec/bltb5a24e5ab1e2cfb0/6a88e3589b942efdb6f74110/wow-thumbnail-homepage.jpg",
    "battlenet:wowa": "https://blz-contentstack-images.akamaized.net/v3/assets/blt9c12f249ac15c7ec/bltb5a24e5ab1e2cfb0/6a88e3589b942efdb6f74110/wow-thumbnail-homepage.jpg",
    "battlenet:wtcg": "https://d39zum0jwvcigt.cloudfront.net/_next/static/images/default-475d770302527dbab7708dca2af05afd.jpg",
    "battlenet:hero": "https://store-images.s-microsoft.com/image/apps.38378.14113515594390646.9e638c04-e7d6-468d-8441-ec0f6cc4f7b7.674600de-b6eb-4b18-8c64-ad133a32434e?w=460",
    "battlenet:s2": "https://blz-contentstack-images.akamaized.net/v3/assets/blt9c12f249ac15c7ec/bltbe2068a317e02d9f/6966bac0fb2fd910dbda4a26/og_image.webp?format=jpg",
    "battlenet:s1": "https://blz-contentstack-images.akamaized.net/v3/assets/blt9c12f249ac15c7ec/blt9cbae43d0642e6ba/6a9b42028102310ecb81e0e3/OG_Image.png",
    "battlenet:w3": "https://blz-contentstack-images.akamaized.net/v3/assets/blt9c12f249ac15c7ec/blt806d59ff0b4e0508/6aa88158ad9209ba7f5629ef/rebirth_og.png",
    "battlenet:d3": "https://store-images.s-microsoft.com/image/apps.46364.64140138686919815.d9e5eb6a-300a-42c9-ad36-95f5a7005152.86c286a6-7528-42fe-bf96-c6d007f180f4?w=460",
    "battlenet:osi": "https://store-images.s-microsoft.com/image/apps.36735.13762945046819896.a6fd9d59-7e4a-4d04-9ffd-42dcbbe9a791.7ecac3c2-f946-4316-90f8-c3fba6772ca2?w=460",
    "battlenet:anbs": "https://blz-contentstack-images.akamaized.net/v3/assets/blt9c12f249ac15c7ec/blt47deaa9e2be4b752/6a1f4beaeb54a6907d694fb8/DI_Warlock_OG-Image@2x_enUS.jpg",
    "epic:fortnite": "https://store-images.s-microsoft.com/image/apps.21491.70702278257994163.f7265b8a-f10e-49c4-8115-73c4d800af63.a3060383-ea77-4eac-8882-c5c52237b18b?w=460",
}


def art_for(launcher: str, ref: str) -> str:
    """The cover art the hub shows for a game when nobody has set one: Steam's store header for Steam games
    and for Battle.net/Epic games that are also on Steam, else the curated picture above, else ""."""
    if launcher == "steam":
        return STEAM_ART.format(ref) if ref.isdigit() else ""
    art = ART.get(f"{launcher}:{ref}".lower())
    if art:
        return art
    twin = (BATTLENET_BY_CODE if launcher == "battlenet" else EPIC_BY_APP if launcher == "epic" else {}).get(ref.lower(), {})
    return STEAM_ART.format(twin["steam"]) if twin.get("steam") else ""


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


def epic_search(q: str, library: list[dict] = ()) -> list[dict]:
    """The curated Epic games, then games on the kids' Epic accounts (library rows: app, title, image_url)."""
    out = [dict(g) for g in EPIC if matches(q, g["title"], g.get("aliases", []))]
    seen = {g["app"].lower() for g in out}
    for g in library:
        app = (g.get("app") or "")
        if not EPIC_APP_RE.match(app) or app.lower() in seen or not matches(q, g.get("title") or ""):
            continue
        known = EPIC_BY_APP.get(app.lower(), {})
        seen.add(app.lower())
        out.append({"app": app, "title": g["title"], "esrb": known.get("esrb", ""), "image_url": g.get("image_url", ""),
                    "steam": known.get("steam"), "prefer": known.get("prefer", False), "owned": True})
    return out[:8]


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

def merge(q: str, steam: list[dict], battlenet: list[dict], xbox: list[dict], game_pass_enabled: bool, in_catalogue: dict,
          epic: list[dict] = ()) -> list[dict]:
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
        if not r["image_url"]:
            r["image_url"] = art_for("battlenet", b["code"])
        add_platform(r, "battlenet", "battlenet", b["code"])
    for e in epic:
        r = steam_by_id.get(e.get("steam")) or row(e["title"])
        if not r["esrb"]:
            r["esrb"] = e.get("esrb", "")
        if not r["image_url"]:
            r["image_url"] = art_for("epic", e["app"]) or e.get("image_url", "")
        add_platform(r, "epic", "epic", e["app"], note="On his Epic account" if e.get("owned") else "")
        if e.get("prefer"):
            r["prefer"] = "epic"
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
        prefer = r.pop("prefer", None)
        r["platforms"].sort(key=lambda p: (not p["addable"], p["platform"] != prefer, ORDER.index(p["platform"])))
        r["default"] = 0 if r["platforms"] and r["platforms"][0]["addable"] else None
        out.append(r)
    # Closest title first: exact, then starts-with, then the rest in source order.
    nq = norm(q)
    out.sort(key=lambda r: (norm(r["title"]) != nq, not norm(r["title"]).startswith(nq)))
    return out[:12]


LABELS = {"steam": "Steam", "battlenet": "Battle.net", "epic": "Epic Games", "xbox": "PC Game Pass"}
ORDER = ["steam", "battlenet", "epic", "xbox"]   # preferred launcher when a game is on several
