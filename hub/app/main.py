"""Kids PC hub: rules, reporting, requests, and voice invites for the kids' PCs.

Three audiences:
  /api/device/*  the shell, admin helper and Chrome extension on each PC (Bearer device token)
  /api/parent/*  the parents' phone dashboard (session cookie)
  /join/*        friends joining voice and text chat from any browser (invite code)
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import adguard, chat, media, consoles, discover, gamesearch, installs, retention, rules, updates, xbox
from .config import Kid, Settings, get_settings
from .db import DB
from .security import (SESSION_COOKIE, SESSION_SECONDS, current_device, current_extension, current_parent, livekit_admin_token, livekit_token,
                       make_session, origin_allowed, verify_password)

STATIC = Path(__file__).parent / "static"
HEARTBEAT_COUNT_SECONDS = 50   # a heartbeat counts a minute only if the last counted one is this old


def get_db() -> DB:
    return app.state.db


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.db = DB(os.environ.get("HUB_DB", "hub.db"))
    app.state.updates_dir = updates.update_dir(os.environ.get("HUB_DB", "hub.db"))
    # Chat pictures sit next to the database, so a backup of the data folder has them too.
    app.state.chat_dir = retention.PICTURES_DIR = Path(os.environ.get("HUB_DB", "hub.db")).resolve().parent / "chat-pictures"
    app.state.media = media.LocalStore(app.state.chat_dir)   # photos and clips stay on this box; screens get signed links
    seed_playtimes(app.state.db, settings)
    tasks = []
    pollers_on = os.environ.get("HUB_DISABLE_POLLERS") != "1"
    tasks.append(asyncio.create_task(retention.prune_forever(app.state.db, lambda: rules.local_now(settings.timezone))))
    if settings.adguard.enabled and pollers_on:
        tasks.append(asyncio.create_task(adguard.poll_forever(app.state.db, settings)))
    if settings.updates.enabled and settings.updates.github_token and pollers_on:
        tasks.append(asyncio.create_task(updates.poll_forever(settings, app.state.updates_dir)))
    if settings.consoles.enabled and pollers_on:
        tasks.append(asyncio.create_task(consoles.sync_forever(
            app.state.db, settings, lambda kid: today_state(app.state.db, settings, kid), settings.consoles.poll_seconds)))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Kids PC hub", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(chat.router)


@app.middleware("http")
async def remote_access_gate(request: Request, call_next):
    # A request that came in through Cloudflare (the tunnel) is from outside the house, even though
    # its last hop is a container on this machine. Cloudflare always sets this header itself.
    if not origin_allowed(get_settings(), client_ip(request), request.url.path):
        return JSONResponse({"detail": "hub is home-network only"}, status_code=403)
    response = await call_next(request)
    # Pages and their scripts change with every hub update. Without this a browser keeps showing
    # its saved copy for a while; with it the browser asks each time and usually gets a quick "unchanged".
    # "private" is for Cloudflare, which the friend's page goes through: with "no-cache" alone it kept the
    # stylesheet and told browsers to hold it for 4 hours, so friends saw a new page with the old look.
    path = request.url.path
    if path == "/" or path.startswith(("/join/", "/static/")):
        response.headers.setdefault("Cache-Control", "no-cache, private")
    return response


# --------------------------------------------------------------------------- helpers

def rule_for(db: DB, kid: Kid, weekday: int) -> rules.DayRule:
    """The kid's playtime for a day of the week: the parents' saved rule, else config.yaml's defaults."""
    r = db.one("SELECT daily_minutes, bedtime, windows FROM playtimes WHERE kid_id=? AND weekday=?", (kid.id, weekday))
    if not r:
        return rules.default_rule(kid, weekday)
    return rules.DayRule(daily_minutes=r["daily_minutes"], bedtime=r["bedtime"], windows=[tuple(w) for w in json.loads(r["windows"])])


def seed_playtimes(db: DB, settings: Settings) -> None:
    """The DB owns every playtime rule. config.yaml's daily_minutes/bedtime only seed a kid's week the first
    time the hub sees that kid; after that, edits happen through the API (dashboard), never config.yaml."""
    for kid in settings.kids:
        for wd in range(7):
            r = rules.default_rule(kid, wd)
            db.x("""INSERT OR IGNORE INTO playtimes(kid_id,weekday,daily_minutes,bedtime,windows,updated_by)
                    VALUES(?,?,?,?,'[]','')""", (kid.id, wd, r.daily_minutes, r.bedtime))


def today_state(db: DB, settings: Settings, kid: Kid, now: datetime | None = None) -> rules.TimeState:
    now = now or rules.local_now(settings.timezone)
    day = rules.day_key(now)
    used = (db.one("SELECT minutes FROM usage WHERE kid_id=? AND day=?", (kid.id, day)) or {}).get("minutes", 0)
    used += consoles.console_minutes(db, kid.id, day)   # one daily budget: Switch play counts too
    g = db.one("SELECT COALESCE(SUM(minutes),0) AS m, MAX(until) AS u FROM grants WHERE kid_id=? AND day=?", (kid.id, day))
    # Extra time approved late at night may run past midnight, so last night's "play until" still counts.
    y = db.one("SELECT MAX(until) AS u FROM grants WHERE kid_id=? AND day=?", (kid.id, rules.day_key(now - timedelta(days=1))))
    untils = [u for u in (g["u"] if g else "", y["u"] if y else "") if u]
    late_until = datetime.fromisoformat(max(untils)) if untils else None
    p = db.one("SELECT until FROM pauses WHERE kid_id=?", (kid.id,))
    paused_until = datetime.fromisoformat(p["until"]) if p else None
    return rules.compute(rule_for(db, kid, now.weekday()), now, used, g["m"] if g else 0, late_until, paused_until,
                         rule_for=lambda wd: rule_for(db, kid, wd))


STEAM_ART = "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{}/header.jpg"


def with_art(g: dict) -> dict:
    """Steam games get their store header image unless the parent set one."""
    if not g.get("image_url") and g["launcher"] == "steam" and g["launch_ref"].isdigit():
        g["image_url"] = STEAM_ART.format(g["launch_ref"])
    return g


STEAM_CACHE_SECONDS = 24 * 3600
ADULT_DESCRIPTORS = {3, 4}   # Steam content descriptors: adult-only sexual content, frequent nudity/sexual content


async def steam_app(appid: str, db: DB | None = None) -> dict:
    """Store details for one Steam app, normalized: name, art, ESRB rating, age gate, adult flag.
    Cached in kv for a day. Best effort: {} if Steam is unreachable or the app doesn't exist."""
    key = f"steam:{appid}"
    if db:
        row = db.one("SELECT value FROM kv WHERE key=?", (key,))
        if row:
            cached = json.loads(row["value"])
            if time.time() - cached.get("_at", 0) < STEAM_CACHE_SECONDS:
                return cached["app"]
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get("https://store.steampowered.com/api/appdetails", params={"appids": appid, "l": "english", "cc": "US"})
            d = r.json().get(appid, {})
    except Exception:
        return {}
    if not d.get("success"):
        return {}
    data = d["data"]
    esrb = (data.get("ratings") or {}).get("esrb") or {}
    desc_ids = set((data.get("content_descriptors") or {}).get("ids") or [])
    raw_age = str(data.get("required_age") or "0").rstrip("+")
    age = int(raw_age) if raw_age.isdigit() else 0
    app_ = {
        "appid": appid, "name": data.get("name", ""), "type": data.get("type", ""),
        "image_url": data.get("header_image") or STEAM_ART.format(appid),
        "description": data.get("short_description", ""),
        "esrb": (esrb.get("rating") or "").upper(), "esrb_descriptors": esrb.get("descriptors", ""),
        "content_notes": (data.get("content_descriptors") or {}).get("notes") or "",
        "age": age, "adult": age >= 18 or bool(desc_ids & ADULT_DESCRIPTORS),
        "free": bool(data.get("is_free")),
        "price": ((data.get("price_overview") or {}).get("final_formatted") or ""),
    }
    if db:
        db.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
             (key, json.dumps({"_at": time.time(), "app": app_})))
    return app_


async def steam_lookup(appid: str) -> dict:
    """Name and header image (kept for add_game)."""
    a = await steam_app(appid)
    return {"name": a.get("name", ""), "header_image": a.get("image_url", "")} if a else {}


async def steam_store_search(q: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get("https://store.steampowered.com/api/storesearch/", params={"term": q[:100], "l": "english", "cc": "US"})
            return [i for i in r.json().get("items", []) if i.get("type") == "app"][:8]
    except Exception:
        return []


async def steam_search(q: str, db: DB, kid_safe: bool) -> list[dict]:
    items = await steam_store_search(q)
    details = await asyncio.gather(*(steam_app(str(i["id"]), db) for i in items))
    out = []
    for i, d in zip(items, details):
        d = d or {"appid": str(i["id"]), "name": i["name"], "image_url": STEAM_ART.format(i["id"]), "adult": False, "esrb": ""}
        if kid_safe and (d.get("adult") or d.get("type") not in ("game", None, "")):
            continue   # never show adult-only store pages, DLC or soundtracks on the kid's screen
        out.append(d)
    return out


NEW_GAME_DAYS = 14   # an approved game he hasn't played yet says "New" on his home screen for this long


def games_for(db: DB, settings: Settings, kid_id: str) -> list[dict]:
    """His approved games, with when he last played each (last_played, ISO or "") and whether it is new to him."""
    rows = db.q("""SELECT g.*, a.approved_at, p.last_at AS last_played, COALESCE(p.minutes, 0) AS minutes_played
                   FROM games g JOIN approvals a ON a.game_id = g.id
                   LEFT JOIN game_play p ON p.kid_id = a.kid_id AND p.game_id = g.id
                   WHERE a.kid_id=? ORDER BY g.title""", (kid_id,))
    if not settings.decisions.game_pass_enabled:
        rows = [r for r in rows if r["launcher"] != "gamepass"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NEW_GAME_DAYS)).isoformat()
    for r in rows:
        approved_at = r.pop("approved_at") or ""
        r["last_played"] = r["last_played"] or ""
        r["new"] = not r["last_played"] and approved_at >= cutoff
    return [with_art(r) for r in rows]


CLOUDFLARE_FAMILY = {"servers": ["1.1.1.3", "1.0.0.3", "2606:4700:4700::1113", "2606:4700:4700::1003"],
                     "doh": "https://family.cloudflare-dns.com/dns-query"}


# Other engines are blocked so searches go through the chosen one (Bing and DuckDuckGo have AI answers too).
_OTHER_ENGINES = ["bing.com/search", "bing.com/chat", "duckduckgo.com", "search.yahoo.com", "yandex.com", "search.brave.com"]
SEARCH = {
    # udm=14 is Google's "Web" results view: no AI Overview, no AI Mode. Plain google.com/search is blocked,
    # so a search typed on google.com itself shows the blocked page; the address bar uses the URL below.
    "google_web": {"name": "Google", "url": "https://www.google.com/search?q={searchTerms}&udm=14&safe=active",
                   "block": ["google.com/search", *_OTHER_ENGINES], "allow": ["google.com/search?udm=14"]},
    "kiddle": {"name": "Kiddle", "url": "https://www.kiddle.co/s.php?q={searchTerms}",
               "block": ["google.com/search", *_OTHER_ENGINES], "allow": ["kiddle.co"]},
    "google": {"name": "Google", "url": "https://www.google.com/search?q={searchTerms}&safe=active",
               "block": _OTHER_ENGINES, "allow": []},
}


def dns_for(settings: Settings) -> dict:
    """DNS the kid PC must use. servers=[] means leave the PC's DNS alone; doh="" means Chrome secure DNS off."""
    mode = settings.dns_filter
    if mode == "adguard" and settings.adguard.dns_ip:
        return {"filter": "adguard", "servers": [settings.adguard.dns_ip], "doh": ""}
    if mode == "none":
        return {"filter": "none", "servers": [], "doh": ""}
    return {"filter": "cloudflare_family", **CLOUDFLARE_FAMILY}   # also the fallback if adguard has no IP yet


def allowed_sites(db: DB, settings: Settings) -> list[str]:
    extra = [r["domain"] for r in db.q("SELECT domain FROM sites ORDER BY domain")]
    return sorted(set(settings.allowed_sites) | set(extra))


def kid_or_404(settings: Settings, kid_id: str) -> Kid:
    kid = settings.kid(kid_id)
    if not kid:
        raise HTTPException(404, "no such kid")
    return kid


# --------------------------------------------------------------------------- device API

class AppState(BaseModel):
    state: Literal["installed", "installing", "failed", "manual"]
    detail: str = Field(default="", max_length=300)


class GameInstallState(BaseModel):
    state: Literal[installs.REPORTABLE]  # type: ignore[valid-type]
    detail: str = Field(default="", max_length=300)
    done_bytes: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    retry: int = 0               # the "Try again" round this report belongs to; an older round's report is ignored
    stage: str = Field(default="", max_length=60)   # what the store is doing right now, e.g. "Waiting for Steam", "Checking files"


class Heartbeat(BaseModel):
    running: str = ""            # title of the foreground game/app, "" when on the home screen
    active: bool = False         # true when a game or Chrome is in use (counts against time)
    apps: dict[str, AppState] = Field(default_factory=dict)   # managed apps on this PC
    game_id: int | None = None   # catalogue id of the game in that title, when it is one
    voice: Literal["", "family", "friends"] = ""   # voice room the shell has open
    # Minutes played while the PC couldn't reach the hub (cable out, Wi-Fi off, hub down). The helper keeps
    # count and sends them with the first heartbeat that gets through, so going offline isn't free time.
    offline_minutes: int = Field(default=0, ge=0, le=24 * 60)
    games: dict[int, GameInstallState] = Field(default_factory=dict)   # approved games by catalogue id: where each stands on this PC
    disk_free_gb: float | None = Field(default=None, ge=0)              # free space on the drive games go to


class KidRequest(BaseModel):
    kind: Literal["game", "time", "site"]
    detail: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)
    ref: str = Field(default="", pattern=r"^((steam|battlenet|gamepass):[A-Za-z0-9]{1,20})?$")   # the exact game (and store) the kid picked from search


class Visit(BaseModel):
    url: str = Field(max_length=2000)
    title: str = Field(default="", max_length=300)
    at: str                      # ISO time from the extension


def auto_update(db: DB, kid_id: str) -> bool:
    """A parent chose "install updates as soon as they arrive" for this kid's PC. Off unless switched on."""
    return (db.one("SELECT value FROM kv WHERE key=?", (f"auto_update:{kid_id}",)) or {}).get("value") == "1"


@app.get("/api/device/rules")
def device_rules(kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    st = today_state(db, settings, kid)
    games = games_for(db, settings, kid.id)
    # A game is no use without the program it installs through, so approving a Battle.net game is
    # enough to get Battle.net put on the PC. Nobody has to remember to list it in managed_apps.
    managed = list(settings.managed_apps)
    for launcher in ("steam", "battlenet"):
        if launcher not in managed and any(g["launcher"] == launcher for g in games):
            managed.append(launcher)
    # The PC's install list: approved games the hub puts there by itself, and the "Try again" round of each.
    ids = {g["id"] for g in games}
    install = [{"game_id": r["game_id"], "retry": r["retry"] or 0} for r in db.q(
        """SELECT a.game_id, i.retry FROM approvals a LEFT JOIN game_installs i ON i.kid_id=a.kid_id AND i.game_id=a.game_id
           WHERE a.kid_id=? AND a.auto_install=1 ORDER BY a.game_id""", (kid.id,)) if r["game_id"] in ids]
    return {
        "kid": {"id": kid.id, "name": kid.name},
        "time": st.__dict__,
        "warning_minutes": rules.WARNING_MINUTES,
        "games": games,
        "web": {"mode": settings.decisions.web_mode, "allowed_sites": allowed_sites(db, settings), "dns": dns_for(settings),
                "search": SEARCH[settings.decisions.search]},
        "game_pass_enabled": settings.decisions.game_pass_enabled,
        "managed_apps": managed,
        "installs": install,
        "auto_update": auto_update(db, kid.id),
        "commands": db.q("SELECT id,kind,arg,created_by FROM commands WHERE kid_id=? AND state='pending' ORDER BY id", (kid.id,)),
        "pending_requests": db.q("SELECT id,kind,detail,state,created_at FROM requests WHERE kid_id=? AND state='pending'", (kid.id,)),
        # The friend's chat id, never the invite code: the PC has no use for the link, so a kid can't pass it on.
        "friends": [{"code": chat.friend_conv(r["code"]), "friend_name": r["friend_name"]}
                    for r in db.q("SELECT code,friend_name FROM invites WHERE kid_id=? AND revoked=0", (kid.id,))],
        "family": family_for(db, settings, kid),
        "family_voice": family_settings(db)["voice"],
        "timezone": settings.timezone,   # the PC's clock follows the hub, which decides "today" and bedtime
    }


@app.post("/api/device/heartbeat")
def device_heartbeat(hb: Heartbeat, kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    now_ts = time.time()
    now = rules.local_now(settings.timezone)
    prev = db.one("SELECT counted_at FROM status WHERE kid_id=?", (kid.id,))
    counted_at = prev["counted_at"] if prev else 0
    if hb.active and now_ts - counted_at >= HEARTBEAT_COUNT_SECONDS:
        db.x("""INSERT INTO usage(kid_id,day,minutes) VALUES(?,?,1)
                ON CONFLICT(kid_id,day) DO UPDATE SET minutes = minutes + 1""", (kid.id, rules.day_key(now)))
        counted_at = now_ts
    if hb.offline_minutes:
        db.x("""INSERT INTO usage(kid_id,day,minutes) VALUES(?,?,?)
                ON CONFLICT(kid_id,day) DO UPDATE SET minutes = minutes + excluded.minutes""", (kid.id, rules.day_key(now), hb.offline_minutes))
        db.x("INSERT INTO events(kid_id,kind,detail,at) VALUES(?,?,?,?)",
             (kid.id, "offline_play", f"{hb.offline_minutes} min played while the PC couldn't reach the hub", now.isoformat()))
    for app_name, a in list(hb.apps.items())[:10]:
        db.x("""INSERT INTO device_apps(kid_id,app,state,detail,at) VALUES(?,?,?,?,?)
                ON CONFLICT(kid_id,app) DO UPDATE SET state=excluded.state, detail=excluded.detail, at=excluded.at""",
             (kid.id, app_name[:30], a.state, a.detail, now.isoformat()))
    save_install_reports(db, kid, hb, now)
    if hb.disk_free_gb is not None:
        db.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
             (f"disk_free_gb:{kid.id}", f"{hb.disk_free_gb:.1f}"))
    st = today_state(db, settings, kid, now)
    game_id = hb.game_id if hb.game_id is not None and db.one("SELECT 1 FROM games WHERE id=?", (hb.game_id,)) else None
    if game_id is not None:
        # Per-game play: when he last played it (home screen order, "Played yesterday") and his minutes in it.
        counted = int(hb.active and counted_at == now_ts)
        db.x("""INSERT INTO game_play(kid_id,game_id,minutes,last_at) VALUES(?,?,?,?)
                ON CONFLICT(kid_id,game_id) DO UPDATE SET minutes = minutes + excluded.minutes, last_at = excluded.last_at""",
             (kid.id, game_id, counted, now.isoformat()))
    db.x("""INSERT INTO status(kid_id,seen_at,running,counted_at,locked,game_id,voice) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(kid_id) DO UPDATE SET seen_at=excluded.seen_at, running=excluded.running,
            counted_at=excluded.counted_at, locked=excluded.locked, game_id=excluded.game_id, voice=excluded.voice""",
         (kid.id, now.isoformat(), hb.running, counted_at, int(st.locked), game_id, hb.voice if not st.locked else ""))
    return {"time": st.__dict__}


def save_install_reports(db: DB, kid: Kid, hb: Heartbeat, now: datetime) -> None:
    """Store where each approved game stands on the PC, and put the moments that matter in the activity feed."""
    approved = {r["game_id"]: r for r in db.q(
        """SELECT a.game_id, g.title, i.state, i.retry, i.stage, i.detail, i.done_bytes, i.at, i.changed_at, i.rate
           FROM approvals a JOIN games g ON g.id=a.game_id
           LEFT JOIN game_installs i ON i.kid_id=a.kid_id AND i.game_id=a.game_id WHERE a.kid_id=?""", (kid.id,))}
    for gid, rep in list(hb.games.items())[:200]:
        cur = approved.get(gid)
        if cur is None:
            continue                       # not approved (any more): nothing to track
        if rep.retry < (cur["retry"] or 0):
            continue                       # from before the parent pressed Try again; the PC hasn't caught up
        moved = (rep.state, rep.stage, rep.detail, rep.done_bytes) != (cur["state"], cur["stage"] or "", cur["detail"] or "", cur["done_bytes"] or 0)
        changed_at = now.isoformat() if moved or not cur["changed_at"] else cur["changed_at"]
        rate = installs.speed(cur, rep.state, rep.done_bytes, now)
        db.x("""INSERT INTO game_installs(kid_id,game_id,state,detail,done_bytes,total_bytes,at,stage,changed_at,rate) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(kid_id,game_id) DO UPDATE SET state=excluded.state, detail=excluded.detail,
                done_bytes=excluded.done_bytes, total_bytes=excluded.total_bytes, at=excluded.at,
                stage=excluded.stage, changed_at=excluded.changed_at, rate=excluded.rate""",
             (kid.id, gid, rep.state, rep.detail, rep.done_bytes, rep.total_bytes, now.isoformat(), rep.stage, changed_at, rate))
        if rep.state != cur["state"] and cur["state"] is not None and rep.state in ("installed", "failed", "no_space", "not_owned", "needs_signin"):
            kind = "game_installed" if rep.state == "installed" else "game_install_" + ("failed" if rep.state == "failed" else rep.state)
            db.x("INSERT INTO events(kid_id,kind,detail,at) VALUES(?,?,?,?)",
                 (kid.id, kind, f"{cur['title']}{': ' + rep.detail if rep.detail and rep.state != 'installed' else ''}"[:300], now.isoformat()))


@app.post("/api/device/requests", status_code=201)
def device_request(req: KidRequest, kid: Kid = Depends(current_device), db: DB = Depends(get_db)):
    if req.kind == "time" and not (req.detail.isdigit() and 1 <= int(req.detail) <= 240):
        raise HTTPException(422, "time requests carry minutes (1 to 240) as detail")
    dup = db.one("SELECT id FROM requests WHERE kid_id=? AND kind=? AND detail=? AND state='pending'", (kid.id, req.kind, req.detail))
    if dup:
        return {"id": dup["id"], "duplicate": True}
    rid = db.x("INSERT INTO requests(kid_id,kind,detail,note,ref) VALUES(?,?,?,?,?)", (kid.id, req.kind, req.detail, req.note, req.ref))
    return {"id": rid}


@app.get("/api/device/games/search")
async def device_game_search(q: str, kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Game search for the kid's "Get more games" screen: Steam, Battle.net and (when it's on) PC Game Pass,
    one row per game, the same merge as the parent search. Each row carries the store it would come from
    ("battlenet:WoWC"), so the request names the exact game. Adult-only titles and non-games are left out."""
    q = q.strip()[:100]
    if len(q) < 2:
        return []
    gp = settings.decisions.game_pass_enabled
    steam, xbox_cat = await asyncio.gather(steam_search(q, db, kid_safe=True),
                                           gamesearch.gamepass_catalogue(db) if gp else asyncio.sleep(0, None))
    rows = gamesearch.merge(q, steam, gamesearch.battlenet_search(q), gamesearch.xbox_search(q, xbox_cat or []), gp, {})
    out = []
    for r in rows:
        if r["default"] is None or r["adult"]:
            continue
        p = r["platforms"][r["default"]]
        out.append({"ref": f"{p['launcher']}:{p['launch_ref']}", "name": r["title"], "image_url": r["image_url"],
                    "esrb": r["esrb"], "description": (r["details"] or {}).get("description") or "", "store": p["label"]})
    return out


@app.get("/api/device/steam/search")
async def device_steam_search(q: str, kid: Kid = Depends(current_device), db: DB = Depends(get_db)):
    """Steam-only kid search, kept for helpers older than /api/device/games/search."""
    if len(q.strip()) < 2:
        return []
    return await steam_search(q, db, kid_safe=True)


@app.post("/api/device/visits", status_code=204)
def device_visits(visits: list[Visit], kid: Kid = Depends(current_extension), db: DB = Depends(get_db)):
    for v in visits[:200]:
        host = (urlparse(v.url).hostname or "").lower()
        if not host:
            continue
        db.x("INSERT INTO visits(kid_id,source,domain,url,title,at) VALUES(?,?,?,?,?,?)",
             (kid.id, "chrome", host, v.url, v.title, v.at))
    return Response(status_code=204)


class HistoryVisit(BaseModel):
    url: str = Field(max_length=2000)
    title: str = Field(default="", max_length=300)
    at: str


class HistorySearch(BaseModel):
    term: str = Field(max_length=300)
    at: str


class History(BaseModel):
    visits: list[HistoryVisit] = Field(default_factory=list)
    searches: list[HistorySearch] = Field(default_factory=list)


def is_flagged(term: str, words: list[str]) -> bool:
    t = " " + " ".join(term.lower().split()) + " "
    return any(f" {w.lower()}" in t for w in words)   # word starts, so "sex" flags "sexy" but not "essex"


@app.post("/api/device/history", status_code=204)
def device_history(h: History, kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Chrome history read by the helper on the PC: pages visited and search terms."""
    for v in h.visits[:500]:
        host = (urlparse(v.url).hostname or "").lower()
        if host:
            db.x("INSERT INTO visits(kid_id,source,domain,url,title,at) VALUES(?,?,?,?,?,?)", (kid.id, "chrome", host, v.url, v.title, v.at))
    for q in h.searches[:500]:
        flagged = is_flagged(q.term, settings.flag_words)
        db.x("INSERT OR IGNORE INTO searches(kid_id,term,at,flagged) VALUES(?,?,?,?)", (kid.id, q.term.strip(), q.at, int(flagged)))
    return Response(status_code=204)


class CommandResult(BaseModel):
    ok: bool
    result: str = Field(default="", max_length=500)


@app.post("/api/device/commands/{cid}")
def device_command_done(cid: int, r: CommandResult, kid: Kid = Depends(current_device), db: DB = Depends(get_db)):
    c = db.one("SELECT * FROM commands WHERE id=? AND kid_id=?", (cid, kid.id))
    if not c:
        raise HTTPException(404)
    db.x("UPDATE commands SET state=?, result=?, done_at=datetime('now') WHERE id=?", ("done" if r.ok else "failed", r.result, cid))
    return {}


class Event(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    detail: str = Field(default="", max_length=300)
    at: str


@app.post("/api/device/events", status_code=204)
def device_events(events: list[Event], kid: Kid = Depends(current_device), db: DB = Depends(get_db)):
    for e in events[:100]:
        if e.kind in SAME_AS_LAST_SKIPPED:
            last = db.one("SELECT detail FROM events WHERE kid_id=? AND kind=? ORDER BY at DESC, id DESC LIMIT 1", (kid.id, e.kind))
            if last and last["detail"] == e.detail:
                continue   # the helper says this on every start; only a change is news
        db.x("INSERT INTO events(kid_id,kind,detail,at) VALUES(?,?,?,?)", (kid.id, e.kind, e.detail, e.at))
    return Response(status_code=204)


WEB_FILTER_WORDS = {"cloudflare_family": "family filter (blocks adult and harmful sites)", "adguard": "home network filter (AdGuard)",
                    "none": "no filter"}
# Events the PC repeats on every start (the web filter it set, the time zone it follows): kept once per change.
SAME_AS_LAST_SKIPPED = {"web_filter", "timezone_set"}
# Housekeeping no parent needs to read. Still stored (support and tests use them), never shown in Activity.
HIDDEN_EVENTS = {"battlenet_products", "timezone_set"}


@app.get("/api/device/update")
def device_update(arch: Literal["x64", "arm64"], kid: Kid = Depends(current_device)):
    """Latest PC build the hub has for this architecture; the helper compares the version with its own."""
    meta = updates.current(app.state.updates_dir, arch)
    if not meta:
        raise HTTPException(404, "no build on the hub yet")
    return meta


@app.get("/api/device/update/file")
def device_update_file(arch: Literal["x64", "arm64"], kid: Kid = Depends(current_device)):
    path = app.state.updates_dir / updates.ARCHES[arch]
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/zip")


@app.get("/api/device/voice-token")
def device_voice_token(kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """The kid's own token for his voice room. One room per kid: friends join his room."""
    if today_state(db, settings, kid).locked:   # the helper checks too; the hub is the one that can't be skipped
        raise HTTPException(403, "Voice chat is off while the PC is locked.")
    return {"url": settings.livekit.url, "room": f"room-{kid.id}",
            "token": livekit_token(settings, f"kid-{kid.id}", kid.name, f"room-{kid.id}")}


# Family: brothers see each other, join the same game, talk in one family voice room ----------

FAMILY_ROOM = "family-room"   # kid friend rooms are room-<kid id>, so this can't clash
FAMILY_DEFAULTS = {"see": True, "join": True, "voice": True}
ONLINE_WINDOW = timedelta(minutes=3)


def family_settings(db: DB) -> dict:
    row = db.one("SELECT value FROM kv WHERE key='family'")
    return {**FAMILY_DEFAULTS, **(json.loads(row["value"]) if row else {})}


def presence(db: DB, settings: Settings, kid: Kid) -> dict:
    """What one kid is up to right now, from his PC's last heartbeat."""
    s = db.one("SELECT * FROM status WHERE kid_id=?", (kid.id,))
    online = bool(s) and rules.local_now(settings.timezone) - datetime.fromisoformat(s["seen_at"]) < ONLINE_WINDOW
    return {"id": kid.id, "name": kid.name, "online": online,
            "running": s["running"] if online else "",
            "game_id": s["game_id"] if online else None,
            "voice": (s["voice"] or "") if online else "",
            "locked": bool(s["locked"]) if online else False}


def family_for(db: DB, settings: Settings, kid: Kid) -> list[dict]:
    """The other kids, as this kid's shell shows them. join_game is set only when the game his brother
    is playing is approved for him too (the same check the helper makes before installing)."""
    fam = family_settings(db)
    if not fam["see"]:
        return []
    mine = {g["id"]: g for g in games_for(db, settings, kid.id)}
    out = []
    for sib in settings.kids:
        if sib.id == kid.id:
            continue
        p = presence(db, settings, sib)
        game = db.one("SELECT * FROM games WHERE id=?", (p["game_id"],)) if p["game_id"] else None
        p["in_family_voice"] = p.pop("voice") == "family"
        p["game_title"] = game["title"] if game else ""
        p["join_game"] = mine.get(p["game_id"]) if fam["join"] and p["game_id"] and not p["locked"] else None
        # Not approved for him: the shell can turn the Join into a request to Mom or Dad.
        p["ask_ref"] = (f"{game['launcher']}:{game['launch_ref']}" if game and not p["join_game"]
                        and game["launcher"] in ("steam", "battlenet", "gamepass") else "")
        del p["game_id"]
        out.append(p)
    return out


@app.get("/api/device/family-voice-token")
def device_family_voice_token(kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """The family voice room: both kids (and nobody else) can join it without an invite link."""
    if not family_settings(db)["voice"]:
        raise HTTPException(403, "Family voice is turned off. Ask Mom or Dad.")
    if today_state(db, settings, kid).locked:
        raise HTTPException(403, "Voice chat is off while the PC is locked.")
    return {"url": settings.livekit.url, "room": FAMILY_ROOM,
            "token": livekit_token(settings, f"kid-{kid.id}", kid.name, FAMILY_ROOM)}


class FamilySettings(BaseModel):
    see: bool | None = None     # brothers see what each other is playing
    join: bool | None = None    # one-tap Join into the same game
    voice: bool | None = None   # family voice room


@app.get("/api/parent/family")
def parent_family(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kids = [presence(db, settings, k) for k in settings.kids]
    for k in kids:
        k["in_family_voice"] = k.pop("voice") == "family"
    games = [k["game_id"] for k in kids if k["game_id"]]
    together = len(games) > 1 and len(set(games)) == 1
    return {"settings": family_settings(db), "kids": kids, "playing_together": together,
            "voice_count": sum(k["in_family_voice"] for k in kids)}


@app.put("/api/parent/family")
def parent_set_family(f: FamilySettings, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    new = {**family_settings(db), **{k: v for k, v in f.model_dump().items() if v is not None}}
    db.x("INSERT INTO kv(key,value) VALUES('family',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(new),))
    if not new["voice"]:
        # Turned off mid-call: take the kids out of the room now. Best effort, LiveKit may be down.
        for k in settings.kids:
            try:
                httpx.post(f"{settings.livekit.api_url}/twirp/livekit.RoomService/RemoveParticipant",
                           json={"room": FAMILY_ROOM, "identity": f"kid-{k.id}"},
                           headers={"Authorization": f"Bearer {livekit_admin_token(settings, FAMILY_ROOM)}"}, timeout=3)
            except httpx.HTTPError:
                pass
    return new


# --------------------------------------------------------------------------- parent API

class Login(BaseModel):
    name: str
    password: str


# Wrong passwords, per device and name. The kids' PCs are on the same network as the hub, so the sign-in
# page is one address away from a kid who wants to guess Mom's password.
LOGIN_TRIES = 6
LOGIN_WINDOW_SECONDS = 10 * 60
_login_failures: dict[tuple[str, str], list[float]] = {}


def client_ip(request: Request) -> str:
    h = request.headers
    return h.get("cf-connecting-ip") or h.get("x-real-ip") or (request.client.host if request.client else "")


@app.post("/api/parent/login")
def parent_login(body: Login, request: Request, response: Response, settings: Settings = Depends(get_settings)):
    key = (client_ip(request), body.name.strip().lower()[:60])
    now_ts = time.time()
    recent = [t for t in _login_failures.get(key, []) if now_ts - t < LOGIN_WINDOW_SECONDS]
    if len(recent) >= LOGIN_TRIES:
        wait = int((LOGIN_WINDOW_SECONDS - (now_ts - recent[0])) // 60) + 1
        raise HTTPException(429, f"Too many wrong passwords. Try again in {wait} minute{'' if wait == 1 else 's'}.")
    p = next((p for p in settings.parents if p.name.lower() == body.name.strip().lower()), None)
    if not p or not verify_password(body.password, p.password_hash):
        if len(_login_failures) > 5000:
            _login_failures.clear()   # never let a flood of made-up names grow this without limit
        _login_failures[key] = recent + [now_ts]
        raise HTTPException(401, "wrong name or password")
    _login_failures.pop(key, None)
    response.set_cookie(SESSION_COOKIE, make_session(settings, p.name), max_age=SESSION_SECONDS,
                        httponly=True, samesite="strict", secure=os.environ.get("HUB_SECURE_COOKIE") == "1")
    return {"name": p.name}


@app.post("/api/parent/logout")
def parent_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {}


@app.get("/api/parent/overview")
def parent_overview(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    out = []
    for kid in settings.kids:
        s = db.one("SELECT * FROM status WHERE kid_id=?", (kid.id,))
        online = False
        if s:
            seen = datetime.fromisoformat(s["seen_at"])
            online = rules.local_now(settings.timezone) - seen < timedelta(minutes=3)
        out.append({
            "id": kid.id, "name": kid.name, "online": online,
            "running": s["running"] if s and online else "",
            "seen_at": s["seen_at"] if s else None,
            "time": today_state(db, settings, kid).__dict__,
            "consoles": consoles.console_today(db, kid.id, rules.day_key(rules.local_now(settings.timezone))),
            "pending": db.one("SELECT COUNT(*) n FROM requests WHERE kid_id=? AND state='pending'", (kid.id,))["n"],
            "apps": db.q("SELECT app,state,detail,at FROM device_apps WHERE kid_id=? ORDER BY app", (kid.id,)),
            "auto_update": auto_update(db, kid.id),
            "installs": install_counts(installs_for(db, settings, kid, rules.local_now(settings.timezone))),
            "flagged_today": db.one("SELECT COUNT(*) n FROM searches WHERE kid_id=? AND flagged=1 AND at >= ?",
                                    (kid.id, rules.day_key(rules.local_now(settings.timezone))))["n"],
        })
    return {"parent": parent, "kids": out, "decisions": settings.decisions.model_dump(), "builds": updates.pipeline()}


def install_counts(pc: dict) -> dict:
    tones = [g["plain"]["tone"] for g in pc["games"]]
    return {"busy": tones.count("busy"), "needs": tones.count("needs") + tones.count("bad")}


@app.get("/api/parent/searches/{kid_id}")
def parent_searches(kid_id: str, limit: int = 200, days: int = 0,
                    parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid_or_404(settings, kid_id)
    return db.q("SELECT term, at, flagged FROM searches WHERE kid_id=? AND at >= ? ORDER BY at DESC LIMIT ?",
                (kid_id, since(settings, days), min(limit, 1000)))


def since(settings: Settings, days: int) -> str:
    """Start of the window the dashboard asked for: 1 = today, 7 = the last week, 0 = everything kept.
    A day prefix, so it compares against every timestamp shape in the DB (see retention._cutoff)."""
    if days <= 0:
        return ""
    return (rules.local_now(settings.timezone) - timedelta(days=days - 1)).strftime("%Y-%m-%d")


@app.get("/api/parent/visits/{kid_id}")
def parent_visits(kid_id: str, source: str | None = None, limit: int = 200, days: int = 0,
                  parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid_or_404(settings, kid_id)
    sql, args = "SELECT * FROM visits WHERE kid_id=? AND at >= ?", [kid_id, since(settings, days)]
    if source in ("chrome", "dns"):
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY at DESC LIMIT ?"
    args.append(min(limit, 1000))
    return db.q(sql, args)


@app.get("/api/parent/requests")
def parent_requests(state: str = "pending", parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    return db.q("SELECT * FROM requests WHERE state=? ORDER BY created_at DESC LIMIT 100", (state,))


class Decision(BaseModel):
    approve: bool
    minutes: int | None = Field(default=None, ge=1, le=240)     # for time requests, override the requested minutes
    game_id: int | None = None     # for game requests, the catalogue game to approve
    steam_appid: str | None = None  # ...or a Steam app to add to the catalogue and approve in one step
    ref: str = Field(default="", pattern=r"^((steam|battlenet|gamepass):[A-Za-z0-9]{1,20})?$")  # ...or any search result
    reason: str = Field(default="", max_length=200)   # shown to the kid, e.g. why it's a no


@app.post("/api/parent/requests/{rid}")
async def parent_decide(rid: int, d: Decision, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    r = db.one("SELECT * FROM requests WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404)
    if r["state"] != "pending":
        raise HTTPException(409, f"already {r['state']}")
    kid = kid_or_404(settings, r["kid_id"])
    if d.approve:
        if r["kind"] == "time":
            minutes = d.minutes or int(r["detail"])
            grant_time(db, settings, kid, minutes, parent)
        elif r["kind"] == "game":
            if d.game_id is None and d.steam_appid:
                d.ref = f"steam:{d.steam_appid}"
            if d.game_id is None and d.ref:
                d.game_id = await add_game_from_ref(db, settings, d.ref)
            if d.game_id is None:
                raise HTTPException(422, "pick or add the catalogue game to approve")
            approve(db, kid.id, d.game_id)
        elif r["kind"] == "site":
            db.x("INSERT OR IGNORE INTO sites(domain) VALUES(?)", (r["detail"].lower(),))
    db.x("UPDATE requests SET state=?, decided_by=?, decided_at=datetime('now'), reason=? WHERE id=?",
         ("approved" if d.approve else "declined", parent, d.reason.strip(), rid))
    # Tell the kid on his screen (same path as a parent's message).
    what = {"time": f"{d.minutes or r['detail']} more minutes", "site": r["detail"]}.get(r["kind"], r["detail"])
    if r["kind"] == "game" and d.approve and d.game_id:
        g = db.one("SELECT title FROM games WHERE id=?", (d.game_id,))
        what = g["title"] if g else what
    note = f" {parent} says: \"{d.reason.strip()}\"" if d.reason.strip() else ""
    if d.approve:
        where = " It's installing on your PC now. Look for it on your home screen." if r["kind"] == "game" else ""
        text = f"Yes! {what} is approved.{where}{note}"
    else:
        text = f"Not this time: {what}.{note}"
    db.x("INSERT INTO commands(kid_id,kind,arg,created_by) VALUES(?,?,?,?)", (kid.id, "message", text[:300], parent))
    return db.one("SELECT * FROM requests WHERE id=?", (rid,))


def grant_time(db: DB, settings: Settings, kid: Kid, minutes: int, parent: str) -> None:
    now = rules.local_now(settings.timezone)
    st = today_state(db, settings, kid, now)
    until = ""
    # After bedtime, outside the play windows, or when the stop comes before the extra minutes run out,
    # the grant also lets him play past the stop until the minutes are used.
    if st.lock_reason in ("bedtime", "outside_hours") or st.minutes_to_stop < minutes:
        until = (now + timedelta(minutes=minutes)).isoformat()
    db.x("INSERT INTO grants(kid_id,day,minutes,by,until) VALUES(?,?,?,?,?)",
         (kid.id, rules.day_key(now), minutes, parent, until))


class Grant(BaseModel):
    minutes: int = Field(gt=0, le=240)


@app.post("/api/parent/kids/{kid_id}/grant")
def parent_grant(kid_id: str, g: Grant, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid = kid_or_404(settings, kid_id)
    grant_time(db, settings, kid, g.minutes, parent)
    return today_state(db, settings, kid).__dict__


# Playtimes ------------------------------------------------------------------

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class PlayWindow(BaseModel):
    start: str = Field(pattern=HHMM)
    end: str = Field(pattern=r"^(([01]\d|2[0-3]):[0-5]\d|24:00)$")


class Playtime(BaseModel):
    days: list[int] = Field(min_length=1, max_length=7)       # 0 = Monday; save the same rule to each
    daily_minutes: int = Field(ge=0, le=24 * 60)
    bedtime: str = Field(default="", pattern=r"^(([01]\d|2[0-3]):[0-5]\d)?$")
    windows: list[PlayWindow] = Field(default_factory=list, max_length=8)


def playtime_json(db: DB, kid: Kid, weekday: int) -> dict:
    r = rule_for(db, kid, weekday)
    row = db.one("SELECT updated_by, updated_at FROM playtimes WHERE kid_id=? AND weekday=?", (kid.id, weekday))
    by = row["updated_by"] if row else ""
    return {"day": weekday, "name": rules.DAYS[weekday], "daily_minutes": r.daily_minutes, "bedtime": r.bedtime,
            "windows": [{"start": s, "end": e} for s, e in r.windows], "custom": bool(by), "updated_by": by}


@app.get("/api/parent/playtimes")
def parent_playtimes(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    today = rules.local_now(settings.timezone).weekday()
    return {"today": today, "kids": [{"id": k.id, "name": k.name, "days": [playtime_json(db, k, d) for d in range(7)]}
                                     for k in settings.kids]}


@app.put("/api/parent/playtimes/{kid_id}")
def parent_set_playtime(kid_id: str, p: Playtime, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid = kid_or_404(settings, kid_id)
    if any(d < 0 or d > 6 for d in p.days):
        raise HTTPException(422, "days are 0 (Monday) to 6 (Sunday)")
    wins = sorted((w.start, w.end) for w in p.windows)
    for s, e in wins:
        if e <= s:
            raise HTTPException(422, f"play time {s} to {e} ends before it starts")
    for (s1, e1), (s2, _) in zip(wins, wins[1:]):
        if s2 < e1:
            raise HTTPException(422, f"play times {s1}-{e1} and {s2} overlap")
    with db.tx() as c:
        for d in sorted(set(p.days)):
            c.execute("""INSERT INTO playtimes(kid_id,weekday,daily_minutes,bedtime,windows,updated_by,updated_at)
                         VALUES(?,?,?,?,?,?,datetime('now'))
                         ON CONFLICT(kid_id,weekday) DO UPDATE SET daily_minutes=excluded.daily_minutes, bedtime=excluded.bedtime,
                         windows=excluded.windows, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                      (kid.id, d, p.daily_minutes, p.bedtime, json.dumps([list(w) for w in wins]), parent))
    return {"days": [playtime_json(db, kid, d) for d in range(7)], "time": today_state(db, settings, kid).__dict__}


# Remote control -------------------------------------------------------------

class Pause(BaseModel):
    minutes: int = Field(default=0, ge=0, le=24 * 60)   # 0 = until resumed (capped at end of day)


# Consoles (shared daily time) -----------------------------------------------

def consoles_on(settings: Settings) -> None:
    if not settings.consoles.enabled:
        raise HTTPException(404, "Consoles are turned off in config.yaml (consoles: enabled: false).")


@app.get("/api/parent/consoles")
def parent_consoles(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    consoles_on(settings)
    day = rules.day_key(rules.local_now(settings.timezone))
    link = db.one("SELECT provider,label,linked_by,linked_at,synced_at,error FROM console_links WHERE provider=?", (consoles.NINTENDO,))
    devices = []
    for d in db.q("SELECT * FROM console_devices WHERE provider=? ORDER BY name", (consoles.NINTENDO,)) if link else []:
        players = db.q("""SELECT p.player_id, p.nickname, p.kid_id, COALESCE(u.minutes,0) AS minutes, COALESCE(u.games,'[]') AS games
                          FROM console_players p LEFT JOIN console_usage u ON u.provider=p.provider AND u.device_id=p.device_id
                            AND u.player_id=p.player_id AND u.day=?
                          WHERE p.provider=? AND p.device_id=? ORDER BY p.nickname""", (day, consoles.NINTENDO, d["device_id"]))
        for p in players:
            p["games"] = json.loads(p["games"])
        devices.append({"device_id": d["device_id"], "name": d["name"], "model": d["model"], "limit_kid": d["limit_kid"],
                        "stop_game": bool(d["stop_game"]), "limit_now": d["limit_now"], "extra_time": bool(d["extra_time"]),
                        "stops_game": bool(d["stops_game"]), "players": players})
    xlink = db.one("SELECT provider,label,linked_by,linked_at,synced_at,error FROM console_links WHERE provider=?", (xbox.XBOX,))
    members = db.q("""SELECT p.player_id, p.nickname, p.role, p.kid_id, p.enforce, p.blocked, p.hub_block,
                        COALESCE(u.minutes,0) AS minutes, COALESCE(u.games,'[]') AS games
                      FROM console_players p LEFT JOIN console_usage u ON u.provider=p.provider AND u.device_id=p.device_id
                        AND u.player_id=p.player_id AND u.day=?
                      WHERE p.provider=? ORDER BY p.role DESC, p.nickname""", (day, xbox.XBOX)) if xlink else []
    for m in members:
        m["games"], m["enforce"], m["blocked"] = json.loads(m["games"]), bool(m["enforce"]), bool(m["blocked"])
    return {"kids": [{"id": k.id, "name": k.name} for k in settings.kids], "nintendo": link, "devices": devices,
            "xbox": xlink and {**xlink, "members": members}}


class ConsoleFinish(BaseModel):
    link: str = Field(min_length=10, max_length=4000)


@app.post("/api/parent/consoles/nintendo/start")
async def parent_nintendo_start(parent: str = Depends(current_parent), settings: Settings = Depends(get_settings)):
    consoles_on(settings)
    return {"login_url": await consoles.nintendo_start_login()}


@app.post("/api/parent/consoles/nintendo/finish")
async def parent_nintendo_finish(body: ConsoleFinish, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    consoles_on(settings)
    try:
        token, label = await consoles.nintendo_finish_login(body.link)
    except consoles.LoginExpired as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "Nintendo didn't accept that link. Tap Connect again and paste the newest link.")
    now = rules.local_now(settings.timezone).isoformat()
    db.x("""INSERT INTO console_links(provider,token,label,linked_by,linked_at) VALUES(?,?,?,?,?)
            ON CONFLICT(provider) DO UPDATE SET token=excluded.token, label=excluded.label, linked_by=excluded.linked_by,
              linked_at=excluded.linked_at, error=''""", (consoles.NINTENDO, token, label, parent, now))
    await consoles.sync_once(db, settings, lambda kid: today_state(db, settings, kid))
    return parent_consoles(parent, db, settings)


@app.delete("/api/parent/consoles/nintendo", status_code=204)
async def parent_nintendo_unlink(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Forget the account. The Switch keeps whatever limit it has today; set it in Nintendo's app if needed."""
    consoles_on(settings)
    await consoles._drop_client()
    for t in ("console_links", "console_devices", "console_players", "console_usage"):
        db.x(f"DELETE FROM {t} WHERE provider=?", (consoles.NINTENDO,))


@app.post("/api/parent/consoles/sync")
async def parent_consoles_sync(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    consoles_on(settings)
    await consoles.sync_once(db, settings, lambda kid: today_state(db, settings, kid))
    await xbox.sync_once(db, settings, lambda kid: today_state(db, settings, kid))
    return parent_consoles(parent, db, settings)


class ConsolePlayer(BaseModel):
    kid_id: str = ""


@app.put("/api/parent/consoles/nintendo/devices/{device_id}/players/{player_id}")
def parent_console_player(device_id: str, player_id: str, body: ConsolePlayer, parent: str = Depends(current_parent),
                          db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    consoles_on(settings)
    if body.kid_id:
        kid_or_404(settings, body.kid_id)
    if not db.one("SELECT 1 FROM console_players WHERE provider=? AND device_id=? AND player_id=?", (consoles.NINTENDO, device_id, player_id)):
        raise HTTPException(404, "No such player")
    db.x("UPDATE console_players SET kid_id=? WHERE provider=? AND device_id=? AND player_id=?",
         (body.kid_id, consoles.NINTENDO, device_id, player_id))
    return parent_consoles(parent, db, settings)


class ConsoleDevice(BaseModel):
    limit_kid: str = ""        # '' = the hub leaves this console's limit alone
    stop_game: bool = True


@app.put("/api/parent/consoles/nintendo/devices/{device_id}")
async def parent_console_device(device_id: str, body: ConsoleDevice, parent: str = Depends(current_parent),
                                db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    consoles_on(settings)
    if body.limit_kid:
        kid_or_404(settings, body.limit_kid)
    if not db.one("SELECT 1 FROM console_devices WHERE provider=? AND device_id=?", (consoles.NINTENDO, device_id)):
        raise HTTPException(404, "No such console")
    db.x("UPDATE console_devices SET limit_kid=?, stop_game=? WHERE provider=? AND device_id=?",
         (body.limit_kid, int(body.stop_game), consoles.NINTENDO, device_id))
    if body.limit_kid:   # apply now rather than at the next poll
        await consoles.sync_once(db, settings, lambda kid: today_state(db, settings, kid))
    return parent_consoles(parent, db, settings)


# Xbox (Microsoft Family Safety); see xbox.py

@app.post("/api/parent/consoles/xbox/start")
def parent_xbox_start(parent: str = Depends(current_parent), settings: Settings = Depends(get_settings)):
    consoles_on(settings)
    return {"login_url": xbox.LOGIN_URL}


@app.post("/api/parent/consoles/xbox/finish")
async def parent_xbox_finish(body: ConsoleFinish, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    consoles_on(settings)
    if "code=" not in body.link:
        raise HTTPException(400, "That isn't the right address. After signing in, copy the address of the blank page: it contains \"code=\".")
    try:
        token, label = await xbox.finish_login(body.link)
    except Exception:
        raise HTTPException(400, "Microsoft didn't accept that address. Each one works once: tap Connect again, sign in, and paste the new one.")
    now = rules.local_now(settings.timezone).isoformat()
    db.x("""INSERT INTO console_links(provider,token,label,linked_by,linked_at) VALUES(?,?,?,?,?)
            ON CONFLICT(provider) DO UPDATE SET token=excluded.token, label=excluded.label, linked_by=excluded.linked_by,
              linked_at=excluded.linked_at, error=''""", (xbox.XBOX, token, label, parent, now))
    await xbox.sync_once(db, settings, lambda kid: today_state(db, settings, kid))
    return parent_consoles(parent, db, settings)


@app.delete("/api/parent/consoles/xbox", status_code=204)
async def parent_xbox_unlink(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Forget the account. Lifts any Xbox block the hub set first, so nobody is left locked out."""
    consoles_on(settings)
    link = db.one("SELECT token FROM console_links WHERE provider=?", (xbox.XBOX,))
    for m in db.q("SELECT player_id FROM console_players WHERE provider=? AND hub_block != ''", (xbox.XBOX,)) if link else []:
        try:
            await xbox.set_block(link["token"], m["player_id"], None)
        except Exception:
            pass   # unlinking must work even when Microsoft is unreachable
    await xbox.drop_client()
    for t in ("console_links", "console_devices", "console_players", "console_usage"):
        db.x(f"DELETE FROM {t} WHERE provider=?", (xbox.XBOX,))


class XboxMember(BaseModel):
    kid_id: str = ""
    enforce: bool = False      # block his Xbox while he is locked on the hub


@app.put("/api/parent/consoles/xbox/members/{member_id}")
async def parent_xbox_member(member_id: str, body: XboxMember, parent: str = Depends(current_parent),
                             db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    consoles_on(settings)
    if body.kid_id:
        kid_or_404(settings, body.kid_id)
    if not db.one("SELECT 1 FROM console_players WHERE provider=? AND player_id=?", (xbox.XBOX, member_id)):
        raise HTTPException(404, "No such family member")
    db.x("UPDATE console_players SET kid_id=?, enforce=? WHERE provider=? AND player_id=?",
         (body.kid_id, int(body.enforce and bool(body.kid_id)), xbox.XBOX, member_id))
    await xbox.sync_once(db, settings, lambda kid: today_state(db, settings, kid))   # lock or unlock now, not at the next poll
    return parent_consoles(parent, db, settings)


@app.post("/api/parent/kids/{kid_id}/pause")
def parent_pause(kid_id: str, p: Pause, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid = kid_or_404(settings, kid_id)
    now = rules.local_now(settings.timezone)
    until = now + timedelta(minutes=p.minutes) if p.minutes else now.replace(hour=23, minute=59, second=59)
    db.x("""INSERT INTO pauses(kid_id,until,by) VALUES(?,?,?)
            ON CONFLICT(kid_id) DO UPDATE SET until=excluded.until, by=excluded.by""", (kid.id, until.isoformat(), parent))
    return today_state(db, settings, kid).__dict__


@app.delete("/api/parent/kids/{kid_id}/pause")
def parent_resume(kid_id: str, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid = kid_or_404(settings, kid_id)
    db.x("DELETE FROM pauses WHERE kid_id=?", (kid.id,))
    return today_state(db, settings, kid).__dict__


class CommandIn(BaseModel):
    kind: Literal["message", "restart", "shutdown", "install_app"]
    arg: str = Field(default="", max_length=300)


@app.post("/api/parent/kids/{kid_id}/commands", status_code=201)
def parent_command(kid_id: str, c: CommandIn, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid = kid_or_404(settings, kid_id)
    if c.kind == "message" and not c.arg.strip():
        raise HTTPException(422, "message is empty")
    # "kidspc" is the kid shell and helper themselves: put the waiting build in now.
    if c.kind == "install_app" and c.arg not in ("steam", "chrome", "battlenet", "kidspc"):
        raise HTTPException(422, "unknown app")
    cid = db.x("INSERT INTO commands(kid_id,kind,arg,created_by) VALUES(?,?,?,?)", (kid.id, c.kind, c.arg.strip(), parent))
    return {"id": cid}


class AutoUpdateIn(BaseModel):
    on: bool


@app.put("/api/parent/kids/{kid_id}/auto-update")
def parent_auto_update(kid_id: str, body: AutoUpdateIn, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """On: a new build goes in as soon as it arrives, signing the kid out for a minute unless a game is running.
    Off (the default): it waits for the next restart or sign-in."""
    kid = kid_or_404(settings, kid_id)
    db.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
         (f"auto_update:{kid.id}", "1" if body.on else "0"))
    return {"auto_update": body.on}


@app.get("/api/parent/kids/{kid_id}/activity")
def parent_activity(kid_id: str, limit: int = 50, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid_or_404(settings, kid_id)
    rows = db.q("SELECT kind, detail, at FROM events WHERE kid_id=? ORDER BY at DESC, id DESC LIMIT ?", (kid_id, min(limit, 500) * 4))
    events, prev_filter = [], {}
    for e in reversed(rows):            # oldest first, so a repeated web filter line is dropped after its first
        if e["kind"] in HIDDEN_EVENTS:
            continue
        if e["kind"] == "web_filter":
            if prev_filter.get("v") == e["detail"]:
                continue
            prev_filter["v"] = e["detail"]
            e = {**e, "detail": WEB_FILTER_WORDS.get(e["detail"], e["detail"])}
        events.append(e)
    events = events[::-1][:min(limit, 500)]
    cmds = db.q("SELECT kind, arg, state, result, created_by, created_at, done_at FROM commands WHERE kid_id=? ORDER BY id DESC LIMIT 20", (kid_id,))
    return {"events": events, "commands": cmds}


# Installs: every PC, every approved game, where it stands -----------------------------------

def installs_for(db: DB, settings: Settings, kid: Kid, now: datetime) -> dict:
    s = db.one("SELECT seen_at FROM status WHERE kid_id=?", (kid.id,))
    on = installs.online(s["seen_at"] if s else None, now)
    launchers = {a["app"]: a for a in db.q("SELECT app,state,detail,at FROM device_apps WHERE kid_id=?", (kid.id,))}
    rows = db.q("""SELECT g.id AS game_id, g.title, g.launcher, g.launch_ref, g.image_url, a.auto_install AS install,
                          i.state, i.detail, i.done_bytes, i.total_bytes, i.at, i.retry, i.stage, i.changed_at, i.rate
                   FROM approvals a JOIN games g ON g.id=a.game_id
                   LEFT JOIN game_installs i ON i.kid_id=a.kid_id AND i.game_id=a.game_id
                   WHERE a.kid_id=? ORDER BY g.title""", (kid.id,))
    if not settings.decisions.game_pass_enabled:
        rows = [r for r in rows if r["launcher"] != "gamepass"]
    games = []
    for r in rows:
        r = with_art(r)
        r["install"] = bool(r["install"])
        r["state"] = r["state"] or ("queued" if r["install"] else "not_installed")
        r["plain"] = installs.plain(r, kid.name, on, launchers.get(r["launcher"]))
        r["plain"]["since"] = installs.since(r, now)
        games.append(r)
    free = (db.one("SELECT value FROM kv WHERE key=?", (f"disk_free_gb:{kid.id}",)) or {}).get("value")
    return {"id": kid.id, "name": kid.name, "online": on, "seen_at": s["seen_at"] if s else None,
            "disk_free_gb": float(free) if free else None,
            "launchers": [{"app": k, "state": v["state"], "detail": v["detail"]} for k, v in sorted(launchers.items())
                          if k in ("steam", "battlenet", "chrome")],
            "games": games}


@app.get("/api/parent/installs")
def parent_installs(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """The parents' view of every PC, like an IT office's: each approved game, installed or how far along, and what to do if stuck."""
    now = rules.local_now(settings.timezone)
    pcs = [installs_for(db, settings, kid, now) for kid in settings.kids]
    return {"summary": installs.summary(pcs), "pcs": pcs, "hub": hub_status()}


def hub_version() -> str:
    return os.environ.get("HUB_VERSION", "dev")


def hub_status() -> dict:
    """For the PCs page: which version the hub runs, what the self-updater last did, and whether main is ahead."""
    out = {"version": hub_version(), "self_update": False}
    state = Path(os.environ.get("HUB_DB", "hub.db")).resolve().parent / "hub-update.json"
    try:
        s = json.loads(state.read_text())
        out["self_update"] = True
        for k in ("result", "message", "title", "updated_at", "checked_at", "failed_version", "failed_title"):
            if s.get(k):
                out[k] = s[k]
    except (OSError, ValueError):
        pass
    head = updates.seen.get("head", "")
    if head and out["version"] != "dev" and head != out["version"] and head != out.get("failed_version"):
        out["behind"] = {"version": head, "title": updates.seen.get("head_title", "")}
    return out


class InstallIn(BaseModel):
    install: bool


@app.put("/api/parent/installs/{kid_id}/{gid}")
def parent_set_install(kid_id: str, gid: int, body: InstallIn, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Install: put an approved game on the PC now. Off: stop pushing it (it stays approved, and stays on the PC if it got there)."""
    kid = kid_or_404(settings, kid_id)
    if not db.one("SELECT 1 FROM approvals WHERE kid_id=? AND game_id=?", (kid.id, gid)):
        raise HTTPException(404, "that game isn't approved for him")
    if body.install:
        approve(db, kid.id, gid)
    else:
        db.x("UPDATE approvals SET auto_install=0 WHERE kid_id=? AND game_id=?", (kid.id, gid))
    return {"install": body.install}


@app.post("/api/parent/installs/{kid_id}/{gid}/retry")
def parent_retry_install(kid_id: str, gid: int, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Try again: the PC starts this game's install over (restarting the store app if it has to), and its launcher too if that failed."""
    kid = kid_or_404(settings, kid_id)
    g = db.one("SELECT g.launcher FROM approvals a JOIN games g ON g.id=a.game_id WHERE a.kid_id=? AND a.game_id=?", (kid.id, gid))
    if not g:
        raise HTTPException(404, "that game isn't approved for him")
    approve(db, kid.id, gid)
    db.x("UPDATE game_installs SET retry=retry+1, state='queued', detail='', done_bytes=0, total_bytes=0 WHERE kid_id=? AND game_id=? AND state!='installed'",
         (kid.id, gid))
    launcher = db.one("SELECT state FROM device_apps WHERE kid_id=? AND app=?", (kid.id, g["launcher"]))
    if launcher and launcher["state"] == "failed":
        db.x("INSERT INTO commands(kid_id,kind,arg,created_by) VALUES(?,?,?,?)", (kid.id, "install_app", g["launcher"], parent))
    return installs_for(db, settings, kid, rules.local_now(settings.timezone))


# Catalogue ------------------------------------------------------------------

class Game(BaseModel):
    title: str = Field("", max_length=120)  # blank for a Steam game: looked up from the store
    launcher: Literal["steam", "battlenet", "gamepass", "direct"]
    launch_ref: str = Field(min_length=1, max_length=300)
    rating: str = ""
    image_url: str = ""
    approved_for: list[str] = Field(default_factory=list)


@app.get("/api/parent/games")
def parent_games(parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    """The catalogue, with who each game is approved for and, per kid, where it stands on his PC and how much he plays it."""
    games = db.q("SELECT * FROM games ORDER BY title")
    appr = db.q("SELECT * FROM approvals")
    inst = {(r["kid_id"], r["game_id"]): r["state"] for r in db.q("SELECT kid_id, game_id, state FROM game_installs")}
    play = {(r["kid_id"], r["game_id"]): r for r in db.q("SELECT kid_id, game_id, minutes, last_at FROM game_play")}
    for g in games:
        g["approved_for"] = [a["kid_id"] for a in appr if a["game_id"] == g["id"]]
        g["kids"] = {k: {"install": inst.get((k, g["id"]), ""),
                         "minutes": (play.get((k, g["id"])) or {}).get("minutes", 0),
                         "last_played": (play.get((k, g["id"])) or {}).get("last_at", "")} for k in g["approved_for"]}
        with_art(g)
    return games


# Ideas ----------------------------------------------------------------------

def ideas_cap(db: DB, kid_id: str) -> str:
    v = (db.one("SELECT value FROM kv WHERE key=?", (f"ideas_rating:{kid_id}",)) or {}).get("value")
    return v if v in discover.CAPS else discover.DEFAULT_CAP


_steam_quiet_until = 0.0


async def ideas_with_details(db: DB) -> list[dict]:
    """The idea list with Steam's own name, art, rating and blurb where the hub can get them (cached a day).
    A slow or unreachable Steam only means the written-in title and rating are used."""
    global _steam_quiet_until
    ideas = discover.all_ideas()
    quiet = time.time() < _steam_quiet_until   # Steam didn't answer lately: use what's cached, don't wait on it again

    async def details(i: dict) -> dict:
        if i["launcher"] != "steam":
            return {}
        if quiet:
            row = db.one("SELECT value FROM kv WHERE key=?", (f"steam:{i['launch_ref']}",))
            return json.loads(row["value"])["app"] if row else {}
        try:
            return await asyncio.wait_for(steam_app(i["launch_ref"], db), 5)
        except Exception:
            return {}
    found = await asyncio.gather(*(details(i) for i in ideas))
    if not quiet and not any(found):
        _steam_quiet_until = time.time() + 600
    for i, d in zip(ideas, found):
        i["image_url"] = (d or {}).get("image_url") or (STEAM_ART.format(i["launch_ref"]) if i["launcher"] == "steam" else "")
        i["title"] = (d or {}).get("name") or i["title"]
        i["esrb"] = (d or {}).get("esrb") or i["esrb"]
        i["description"] = (d or {}).get("description", "")
        i["adult"] = bool((d or {}).get("adult"))
        i["ref"] = f"{i['launcher']}:{i['launch_ref']}"
    return [i for i in ideas if not i["adult"]]


STORE_LABEL = {"steam": "Steam", "battlenet": "Battle.net", "gamepass": "PC Game Pass"}


@app.get("/api/device/games/discover")
async def device_game_discover(kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Shelves for "Get more games": what his brothers have that he doesn't, then the ideas list, up to his
    rating cap. Games already approved for him are left out; ones he already asked for say so (asked)."""
    mine = {(g["launcher"], g["launch_ref"].lower()) for g in games_for(db, settings, kid.id)}
    pending = db.q("SELECT detail, ref FROM requests WHERE kid_id=? AND kind='game' AND state='pending'", (kid.id,))
    asked_refs = {r["ref"].lower() for r in pending if r["ref"]}
    asked_titles = {r["detail"].lower() for r in pending}
    gp = settings.decisions.game_pass_enabled

    def hit(launcher: str, ref: str, title: str, image: str, esrb: str, description: str = "") -> dict:
        r = f"{launcher}:{ref}"
        return {"ref": r, "name": title, "image_url": image, "esrb": esrb, "description": description,
                "store": STORE_LABEL.get(launcher, ""), "asked": r.lower() in asked_refs or title.lower() in asked_titles}

    shelves = []
    # The family's own catalogue first: approved for a brother, not for him. A parent already said yes once.
    sibs = [k for k in settings.kids if k.id != kid.id]
    if sibs and family_settings(db)["see"]:
        rows = db.q(f"""SELECT DISTINCT g.* FROM games g JOIN approvals a ON a.game_id=g.id
                        WHERE a.kid_id IN ({",".join("?" * len(sibs))}) ORDER BY g.title""", tuple(k.id for k in sibs))
        fam = [hit(g["launcher"], g["launch_ref"], g["title"], with_art(g)["image_url"], g["rating"]) for g in rows
               if (g["launcher"], g["launch_ref"].lower()) not in mine and g["launcher"] in STORE_LABEL
               and (gp or g["launcher"] != "gamepass")]
        if fam:
            shelves.append({"title": f"{sibs[0].name} has these" if len(sibs) == 1 else "Your family has these", "games": fam[:12]})
    cap = ideas_cap(db, kid.id)
    if cap != "off":
        seen = {(s["ref"].lower()) for sh in shelves for s in sh["games"]}
        by_shelf: dict[str, list[dict]] = {}
        for i in await ideas_with_details(db):
            if (i["launcher"], i["launch_ref"].lower()) in mine or i["ref"].lower() in seen or not discover.rating_ok(i["esrb"], cap):
                continue
            by_shelf.setdefault(i["shelf"], []).append(hit(i["launcher"], i["launch_ref"], i["title"], i["image_url"], i["esrb"], i["description"]))
        shelves += [{"title": t, "games": g} for t, g in by_shelf.items() if g]
    return {"cap": cap, "shelves": shelves}


@app.get("/api/parent/games/ideas")
async def parent_game_ideas(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """The ideas list for the dashboard: each game with whether it's in the catalogue and who it's approved for,
    plus each kid's rating cap (which ideas his screen shows)."""
    cat = {(g["launcher"], g["launch_ref"].lower()): g["id"] for g in db.q("SELECT id, launcher, launch_ref FROM games")}
    appr = db.q("SELECT kid_id, game_id FROM approvals")
    out = []
    for i in await ideas_with_details(db):
        gid = cat.get((i["launcher"], i["launch_ref"].lower()))
        i["game_id"] = gid
        i["approved_for"] = [a["kid_id"] for a in appr if a["game_id"] == gid] if gid else []
        i["store"] = STORE_LABEL.get(i["launcher"], "")
        out.append(i)
    return {"caps": {k.id: ideas_cap(db, k.id) for k in settings.kids}, "choices": discover.CAPS, "ideas": out}


class IdeasCap(BaseModel):
    rating: Literal["off", "E", "E10+", "T", "M"]


@app.put("/api/parent/kids/{kid_id}/ideas")
def parent_set_ideas_cap(kid_id: str, body: IdeasCap, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid_or_404(settings, kid_id)
    db.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"ideas_rating:{kid_id}", body.rating))
    return {"rating": body.rating}


@app.post("/api/parent/games", status_code=201)
async def parent_add_game(g: Game, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    for k in g.approved_for:
        kid_or_404(settings, k)
    gid = await add_game(db, g)
    for k in g.approved_for:
        approve(db, k, gid)
    return {"id": gid}


async def add_game(db: DB, g: Game) -> int:
    """Insert a catalogue game (or return the existing one for the same launcher and reference)."""
    g.launch_ref = g.launch_ref.strip()
    if g.launcher == "steam":
        g.launch_ref = g.launch_ref.rstrip("/").split("/app/")[-1].split("/")[0]  # accept a pasted store link
        if not g.launch_ref.isdigit():
            raise HTTPException(400, "Steam games need the app id (the number in the store link)")
    if g.launcher == "battlenet" and g.launch_ref.lower() in gamesearch.BATTLENET_BY_CODE:
        b = gamesearch.BATTLENET_BY_CODE[g.launch_ref.lower()]   # known product code: canonical case, title and rating
        g.launch_ref, g.title, g.rating = b["code"], g.title.strip() or b["title"], g.rating or b["esrb"]
    existing = db.one("SELECT id FROM games WHERE launcher=? AND launch_ref=?", (g.launcher, g.launch_ref))
    if existing:
        return existing["id"]
    if g.launcher == "steam":
        if not g.title.strip() or not g.image_url:
            info = await steam_lookup(g.launch_ref)
            g.title = g.title.strip() or info.get("name", "")
            g.image_url = g.image_url or info.get("header_image", "")
    if not g.title.strip():
        raise HTTPException(400, "Give the game a title")
    return db.x("INSERT INTO games(title,launcher,launch_ref,rating,image_url) VALUES(?,?,?,?,?)",
                (g.title.strip(), g.launcher, g.launch_ref, g.rating, g.image_url))


async def add_game_from_ref(db: DB, settings: Settings, ref: str) -> int:
    """Add a game picked from search, by "steam:620", "battlenet:WoW" or "gamepass:9NBLGGH4R315"."""
    launcher, _, code = ref.partition(":")
    if launcher == "steam":
        return await add_game(db, Game(launcher="steam", launch_ref=code))
    if launcher == "battlenet":
        if code.lower() not in gamesearch.BATTLENET_BY_CODE:
            raise HTTPException(400, "That isn't a Battle.net game the hub knows")
        return await add_game(db, Game(launcher="battlenet", launch_ref=code))
    if not settings.decisions.game_pass_enabled:
        raise HTTPException(400, "Game Pass is switched off on the hub (decisions.game_pass_enabled)")
    hit = next((x for x in await gamesearch.gamepass_catalogue(db) or [] if x["id"] == code), None)
    if not hit:
        raise HTTPException(400, "That game isn't in PC Game Pass")
    return await add_game(db, Game(launcher="gamepass", launch_ref=code, title=hit["title"], rating=hit["esrb"], image_url=hit["image_url"]))


@app.get("/api/parent/games/search")
async def parent_game_search(q: str, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Every store at once: one row per game with art, rating and the platforms it's on.
    Each platform says which launcher and reference to add, so nobody needs to know an id. Best effort."""
    q = q.strip()[:100]
    if len(q) < 2:
        return {"results": [], "xbox": "skipped"}
    steam, xbox_cat = await asyncio.gather(steam_search(q, db, kid_safe=False), gamesearch.gamepass_catalogue(db))
    in_cat = {(g["launcher"], g["launch_ref"]): g["id"] for g in db.q("SELECT id, launcher, launch_ref FROM games")}
    results = gamesearch.merge(q, steam, gamesearch.battlenet_search(q), gamesearch.xbox_search(q, xbox_cat or []),
                               settings.decisions.game_pass_enabled, in_cat)
    return {"results": results, "xbox": "ok" if xbox_cat is not None else "unavailable"}


class FromSearch(BaseModel):
    ref: str = Field(pattern=r"^(steam|battlenet|gamepass):[A-Za-z0-9]{1,20}$")   # a platform's launcher:launch_ref
    approved_for: list[str] = Field(default_factory=list)


@app.post("/api/parent/games/from-search", status_code=201)
async def parent_add_from_search(body: FromSearch, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Add a search result to the catalogue and approve it for the chosen kids in one step."""
    for k in body.approved_for:
        kid_or_404(settings, k)
    gid = await add_game_from_ref(db, settings, body.ref)
    for k in body.approved_for:
        approve(db, k, gid)
    return {"id": gid}


@app.get("/api/parent/steam/search")
async def parent_steam_search(q: str, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    """Steam store matches for a game name, so a kid's request can be approved in one step. Best effort."""
    return await steam_search(q, db, kid_safe=False)


@app.get("/api/parent/steam/app/{appid}")
async def parent_steam_app(appid: str, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    if not appid.isdigit():
        raise HTTPException(400)
    return await steam_app(appid, db)


class Approval(BaseModel):
    approved: bool


@app.put("/api/parent/games/{gid}/kids/{kid_id}")
def parent_set_approval(gid: int, kid_id: str, a: Approval, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid_or_404(settings, kid_id)
    if a.approved:
        approve(db, kid_id, gid)
    else:
        db.x("DELETE FROM approvals WHERE kid_id=? AND game_id=?", (kid_id, gid))
        db.x("DELETE FROM game_installs WHERE kid_id=? AND game_id=?", (kid_id, gid))
    return {}


def approve(db: DB, kid_id: str, gid: int) -> None:
    """Approve a game for a kid and put it on his PC's install list: it installs there by itself."""
    if not db.one("SELECT 1 FROM games WHERE id=?", (gid,)):
        raise HTTPException(404, "no such game")
    db.x("""INSERT INTO approvals(kid_id,game_id,auto_install,approved_at) VALUES(?,?,1,?)
            ON CONFLICT(kid_id,game_id) DO UPDATE SET auto_install=1,
            approved_at=CASE WHEN COALESCE(approved_at,'')='' THEN excluded.approved_at ELSE approved_at END""",
         (kid_id, gid, datetime.now(timezone.utc).isoformat()))
    db.x("""INSERT INTO game_installs(kid_id,game_id,state) VALUES(?,?,'queued') ON CONFLICT(kid_id,game_id)
            DO UPDATE SET state=CASE WHEN state='installed' THEN state ELSE 'queued' END""", (kid_id, gid))


@app.delete("/api/parent/games/{gid}")
def parent_delete_game(gid: int, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    db.x("DELETE FROM games WHERE id=?", (gid,))
    return {}


# Sites ----------------------------------------------------------------------

class Site(BaseModel):
    domain: str = Field(min_length=3, max_length=200)


@app.get("/api/parent/sites")
def parent_sites(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    return {"mode": settings.decisions.web_mode, "sites": allowed_sites(db, settings)}


@app.post("/api/parent/sites")
def parent_add_site(s: Site, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    db.x("INSERT OR IGNORE INTO sites(domain) VALUES(?)", (s.domain.lower().strip(),))
    return {}


@app.delete("/api/parent/sites/{domain}")
def parent_del_site(domain: str, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    db.x("DELETE FROM sites WHERE domain=?", (domain.lower(),))
    return {}


# Voice invites --------------------------------------------------------------

# History: how long it is kept, and clearing it --------------------------------

@app.get("/api/parent/retention")
def parent_retention(parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    days = retention.get(db)
    return {"days": days, "choices": list(retention.CHOICES),
            "kinds": [{"kind": k, "label": label, "days": days[k]} for k, (label, _) in retention.KINDS.items()]}


class RetentionIn(BaseModel):
    days: dict[str, int]


@app.put("/api/parent/retention")
def parent_set_retention(r: RetentionIn, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    bad = [k for k, v in r.days.items() if k not in retention.KINDS or v not in retention.CHOICES]
    if bad:
        raise HTTPException(422, f"unknown kind or number of days: {', '.join(bad)}")
    days = retention.put(db, r.days)
    retention.prune(db, rules.local_now(settings.timezone))   # a shorter window takes effect now, not in an hour
    return {"days": days}


@app.delete("/api/parent/history/{kind}")
def parent_clear_history(kind: str, kid_id: str | None = None, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    if kind not in retention.KINDS:
        raise HTTPException(404)
    if kid_id:
        kid_or_404(settings, kid_id)
    return {"deleted": retention.clear(db, kind, kid_id=kid_id)}


class InviteIn(BaseModel):
    kid_id: str
    friend_name: str = Field(min_length=1, max_length=60)


@app.get("/api/parent/invites")
def parent_invites(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    rows = db.q("SELECT * FROM invites ORDER BY created_at DESC")
    for r in rows:
        r["link"] = f"{settings.livekit.public_join_base}/join/{r['code']}"
    return rows


@app.post("/api/parent/invites", status_code=201)
def parent_create_invite(i: InviteIn, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid_or_404(settings, i.kid_id)
    code = secrets.token_urlsafe(18)
    db.x("INSERT INTO invites(code,kid_id,friend_name) VALUES(?,?,?)", (code, i.kid_id, i.friend_name))
    return {"code": code, "link": f"{settings.livekit.public_join_base}/join/{code}"}


@app.delete("/api/parent/invites/{code}")
def parent_revoke_invite(code: str, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    inv = db.one("SELECT * FROM invites WHERE code=?", (code,))
    if not inv:
        raise HTTPException(404)
    db.x("UPDATE invites SET revoked=1, revoked_at=datetime('now') WHERE code=? AND revoked=0", (code,))
    # Kick the friend if they are in the room right now. Best effort: LiveKit may be down.
    room = f"room-{inv['kid_id']}"
    try:
        httpx.post(f"{settings.livekit.api_url}/twirp/livekit.RoomService/RemoveParticipant",
                   json={"room": room, "identity": f"friend-{code[:8]}"},
                   headers={"Authorization": f"Bearer {livekit_admin_token(settings, room)}"}, timeout=5)
    except httpx.HTTPError:
        pass
    return {}


# --------------------------------------------------------------------------- friend join

def live_invite(db: DB, code: str) -> dict:
    inv = db.one("SELECT * FROM invites WHERE code=? AND revoked=0", (code,))
    if not inv:
        raise HTTPException(404, "This invite link is no longer active.")
    return inv


@app.get("/join/{code}", response_class=HTMLResponse)
def join_page(code: str, db: DB = Depends(get_db)):
    live_invite(db, code)
    return FileResponse(STATIC / "join.html")


@app.post("/api/voice/join/{code}")
def voice_join(code: str, db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    inv = live_invite(db, code)
    if settings.decisions.friend_verification == "discord_oauth" and not inv["verified_by"]:
        # TODO(discord_oauth): redirect the friend's parent through Discord OAuth, store their
        # Discord user id in invites.verified_by, then allow joining. Not built yet.
        raise HTTPException(403, "A parent needs to confirm this invite first.")
    kid = kid_or_404(settings, inv["kid_id"])
    room = f"room-{kid.id}"
    # Identity is tied to the invite, so revoking it lets the hub kick that friend (see voice/README).
    return {"url": settings.livekit.url, "room": room, "kid_name": kid.name, "friend_name": inv["friend_name"],
            "token": livekit_token(settings, f"friend-{code[:8]}", inv["friend_name"], room, ttl=3 * 3600)}



# New-PC setup: one line a parent pastes into PowerShell on a fresh Windows PC ---------------------
#   irm http://<hub>/setup/<code> | iex
# The code names the kid; the script it returns claims the PC's tokens, downloads the newest build
# from this hub and runs the installer. Codes expire, so a code seen over someone's shoulder goes stale.

SETUP_CODE_HOURS = 12
SETUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O or 1/I, it gets read off a phone
SETUP_SCRIPT = Path(__file__).parent / "setup_pc.ps1"


def setup_code_row(db: DB, code: str) -> dict | None:
    r = db.one("SELECT * FROM setup_codes WHERE code=?", (code.strip().upper(),))
    return r if r and r["expires_at"] > time.time() else None


def hub_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.post("/api/parent/kids/{kid_id}/setup-code", status_code=201)
def parent_setup_code(kid_id: str, request: Request, parent: str = Depends(current_parent), db: DB = Depends(get_db),
                      settings: Settings = Depends(get_settings)):
    kid = kid_or_404(settings, kid_id)
    code = "".join(secrets.choice(SETUP_ALPHABET) for _ in range(8))
    expires = time.time() + SETUP_CODE_HOURS * 3600
    db.x("INSERT INTO setup_codes(code,kid_id,created_by,expires_at) VALUES(?,?,?,?)", (code, kid.id, parent, expires))
    have = {a: updates.current(app.state.updates_dir, a) is not None for a in updates.ARCHES}
    return {"code": code, "kid_id": kid.id, "command": f"irm {hub_base(request)}/setup/{code} | iex",
            "expires_at": datetime.fromtimestamp(expires).astimezone().isoformat(timespec="minutes"),
            "builds": have}


@app.get("/setup/{code}")
def setup_script(code: str, request: Request, db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """The bootstrap script. Holds no secret itself: it trades the code for the tokens when it runs."""
    row = setup_code_row(db, code)
    if not row or not settings.kid(row["kid_id"]):
        body = ('Write-Host "This setup code is not valid any more. Make a new one on the dashboard: '
                'the kid\'s card, Set up a new PC." -ForegroundColor Red\n')
        return Response(body, media_type="text/plain; charset=utf-8", status_code=200)
    kid = settings.kid(row["kid_id"])
    script = (SETUP_SCRIPT.read_text(encoding="utf-8")
              .replace("__HUB__", hub_base(request)).replace("__CODE__", row["code"]).replace("__KIDNAME__", kid.name))
    return Response(script, media_type="text/plain; charset=utf-8")


class SetupClaimIn(BaseModel):
    computer: str = Field(default="", max_length=64)
    arch: Literal["x64", "arm64"] = "x64"


@app.post("/api/setup/{code}/claim")
def setup_claim(code: str, body: SetupClaimIn, db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    row = setup_code_row(db, code)
    kid = settings.kid(row["kid_id"]) if row else None
    if not kid:
        raise HTTPException(404, "setup code not valid any more")
    build = updates.current(app.state.updates_dir, body.arch)
    if not build:
        raise HTTPException(409, f"the hub has no {body.arch} PC build yet; turn on updates in config.yaml")
    now = rules.local_now(settings.timezone).isoformat(timespec="seconds")
    db.x("UPDATE setup_codes SET used_at=?, used_by=? WHERE code=?", (now, body.computer, row["code"]))
    db.x("INSERT INTO events(kid_id,kind,detail,at) VALUES(?,?,?,?)", (kid.id, "pc_setup", f"setup started on {body.computer or 'a PC'}", now))
    return {"kid_id": kid.id, "kid_name": kid.name, "windows_user": kid.id, "device_token": kid.device_token,
            "extension_token": kid.extension_token, "web_mode": settings.decisions.web_mode, "build": build}


# --------------------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz(db: DB = Depends(get_db)):
    """The self-updater's health check (deploy/hub-autoupdate.sh): up, database readable, which version."""
    db.one("SELECT COUNT(*) AS n FROM sqlite_master")
    return {"ok": True, "version": hub_version()}
