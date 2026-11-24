"""Kids PC hub: rules, reporting, requests, and voice invites for the kids' PCs.

Three audiences:
  /api/device/*  the shell, admin helper and Chrome extension on each PC (Bearer device token)
  /api/parent/*  the parents' phone dashboard (session cookie)
  /join/*        friends joining voice chat from any browser (invite code)
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import adguard, gamesearch, retention, rules, updates
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
    seed_playtimes(app.state.db, settings)
    tasks = []
    pollers_on = os.environ.get("HUB_DISABLE_POLLERS") != "1"
    tasks.append(asyncio.create_task(retention.prune_forever(app.state.db, lambda: rules.local_now(settings.timezone))))
    if settings.adguard.enabled and pollers_on:
        tasks.append(asyncio.create_task(adguard.poll_forever(app.state.db, settings)))
    if settings.updates.enabled and settings.updates.github_token and pollers_on:
        tasks.append(asyncio.create_task(updates.poll_forever(settings, app.state.updates_dir)))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Kids PC hub", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def remote_access_gate(request: Request, call_next):
    # A request that came in through Cloudflare (the tunnel) is from outside the house, even though
    # its last hop is a container on this machine. Cloudflare always sets this header itself.
    h = request.headers
    ip = h.get("cf-connecting-ip") or h.get("x-real-ip") or (request.client.host if request.client else "")
    if not origin_allowed(get_settings(), ip, request.url.path):
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
    g = db.one("SELECT COALESCE(SUM(minutes),0) AS m, MAX(until) AS u FROM grants WHERE kid_id=? AND day=?", (kid.id, day))
    late_until = datetime.fromisoformat(g["u"]) if g and g["u"] else None
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


def games_for(db: DB, settings: Settings, kid_id: str) -> list[dict]:
    rows = db.q("""SELECT g.* FROM games g JOIN approvals a ON a.game_id = g.id
                   WHERE a.kid_id=? ORDER BY g.title""", (kid_id,))
    if not settings.decisions.game_pass_enabled:
        rows = [r for r in rows if r["launcher"] != "gamepass"]
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


class Heartbeat(BaseModel):
    running: str = ""            # title of the foreground game/app, "" when on the home screen
    active: bool = False         # true when a game or Chrome is in use (counts against time)
    apps: dict[str, AppState] = Field(default_factory=dict)   # managed apps on this PC
    game_id: int | None = None   # catalogue id of the game in that title, when it is one
    voice: Literal["", "family", "friends"] = ""   # voice room the shell has open


class KidRequest(BaseModel):
    kind: Literal["game", "time", "site"]
    detail: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)
    ref: str = Field(default="", pattern=r"^(steam:\d{1,10})?$")   # the exact game the kid picked from search


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
    return {
        "kid": {"id": kid.id, "name": kid.name},
        "time": st.__dict__,
        "warning_minutes": rules.WARNING_MINUTES,
        "games": games,
        "web": {"mode": settings.decisions.web_mode, "allowed_sites": allowed_sites(db, settings), "dns": dns_for(settings),
                "search": SEARCH[settings.decisions.search]},
        "game_pass_enabled": settings.decisions.game_pass_enabled,
        "managed_apps": managed,
        "auto_update": auto_update(db, kid.id),
        "commands": db.q("SELECT id,kind,arg,created_by FROM commands WHERE kid_id=? AND state='pending' ORDER BY id", (kid.id,)),
        "pending_requests": db.q("SELECT id,kind,detail,state,created_at FROM requests WHERE kid_id=? AND state='pending'", (kid.id,)),
        "friends": db.q("SELECT code,friend_name FROM invites WHERE kid_id=? AND revoked=0", (kid.id,)),
        "family": family_for(db, settings, kid),
        "family_voice": family_settings(db)["voice"],
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
    for app_name, a in list(hb.apps.items())[:10]:
        db.x("""INSERT INTO device_apps(kid_id,app,state,detail,at) VALUES(?,?,?,?,?)
                ON CONFLICT(kid_id,app) DO UPDATE SET state=excluded.state, detail=excluded.detail, at=excluded.at""",
             (kid.id, app_name[:30], a.state, a.detail, now.isoformat()))
    st = today_state(db, settings, kid, now)
    game_id = hb.game_id if hb.game_id is not None and db.one("SELECT 1 FROM games WHERE id=?", (hb.game_id,)) else None
    db.x("""INSERT INTO status(kid_id,seen_at,running,counted_at,locked,game_id,voice) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(kid_id) DO UPDATE SET seen_at=excluded.seen_at, running=excluded.running,
            counted_at=excluded.counted_at, locked=excluded.locked, game_id=excluded.game_id, voice=excluded.voice""",
         (kid.id, now.isoformat(), hb.running, counted_at, int(st.locked), game_id, hb.voice if not st.locked else ""))
    return {"time": st.__dict__}


@app.post("/api/device/requests", status_code=201)
def device_request(req: KidRequest, kid: Kid = Depends(current_device), db: DB = Depends(get_db)):
    if req.kind == "time" and not req.detail.isdigit():
        raise HTTPException(422, "time requests carry minutes as detail")
    dup = db.one("SELECT id FROM requests WHERE kid_id=? AND kind=? AND detail=? AND state='pending'", (kid.id, req.kind, req.detail))
    if dup:
        return {"id": dup["id"], "duplicate": True}
    rid = db.x("INSERT INTO requests(kid_id,kind,detail,note,ref) VALUES(?,?,?,?,?)", (kid.id, req.kind, req.detail, req.note, req.ref))
    return {"id": rid}


@app.get("/api/device/steam/search")
async def device_steam_search(q: str, kid: Kid = Depends(current_device), db: DB = Depends(get_db)):
    """Game search for the kid's "Get more games" screen. Adult-only titles and non-games are filtered out."""
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
        db.x("INSERT INTO events(kid_id,kind,detail,at) VALUES(?,?,?,?)", (kid.id, e.kind, e.detail, e.at))
    return Response(status_code=204)


# Family chat ----------------------------------------------------------------

class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


def chat_since(db: DB, after: int) -> list[dict]:
    rows = db.q("SELECT id, sender, kid_id, text, at FROM chat WHERE id>? ORDER BY id DESC LIMIT 100", (after,))
    return rows[::-1]


@app.get("/api/device/chat")
def device_chat(after: int = 0, kid: Kid = Depends(current_device), db: DB = Depends(get_db)):
    return [{**m, "mine": m["kid_id"] == kid.id} for m in chat_since(db, after)]


@app.post("/api/device/chat", status_code=201)
def device_chat_send(m: ChatIn, kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    now = rules.local_now(settings.timezone).isoformat(timespec="seconds")
    return {"id": db.x("INSERT INTO chat(sender,kid_id,text,at) VALUES(?,?,?,?)", (kid.name, kid.id, m.text.strip(), now))}


@app.get("/api/parent/chat")
def parent_chat(after: int = 0, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    return [{**m, "mine": m["kid_id"] is None and m["sender"] == parent} for m in chat_since(db, after)]


@app.post("/api/parent/chat", status_code=201)
def parent_chat_send(m: ChatIn, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    now = rules.local_now(settings.timezone).isoformat(timespec="seconds")
    return {"id": db.x("INSERT INTO chat(sender,kid_id,text,at) VALUES(?,?,?,?)", (parent, None, m.text.strip(), now))}


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
def device_voice_token(kid: Kid = Depends(current_device), settings: Settings = Depends(get_settings)):
    """The kid's own token for his voice room. One room per kid: friends join his room."""
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
        p["ask_ref"] = f"steam:{game['launch_ref']}" if game and not p["join_game"] and game["launcher"] == "steam" else ""
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


@app.post("/api/parent/login")
def parent_login(body: Login, response: Response, settings: Settings = Depends(get_settings)):
    p = next((p for p in settings.parents if p.name.lower() == body.name.lower()), None)
    if not p or not verify_password(body.password, p.password_hash):
        raise HTTPException(401, "wrong name or password")
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
            "pending": db.one("SELECT COUNT(*) n FROM requests WHERE kid_id=? AND state='pending'", (kid.id,))["n"],
            "apps": db.q("SELECT app,state,detail,at FROM device_apps WHERE kid_id=? ORDER BY app", (kid.id,)),
            "auto_update": auto_update(db, kid.id),
            "flagged_today": db.one("SELECT COUNT(*) n FROM searches WHERE kid_id=? AND flagged=1 AND at >= ?",
                                    (kid.id, rules.day_key(rules.local_now(settings.timezone))))["n"],
        })
    return {"parent": parent, "kids": out, "decisions": settings.decisions.model_dump(), "builds": updates.pipeline()}


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
    minutes: int | None = None     # for time requests, override the requested minutes
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
            db.x("INSERT OR IGNORE INTO approvals(kid_id,game_id) VALUES(?,?)", (kid.id, d.game_id))
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
        where = " It's in Get more games now." if r["kind"] == "game" else ""
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
    events = db.q("SELECT kind, detail, at FROM events WHERE kid_id=? ORDER BY at DESC LIMIT ?", (kid_id, min(limit, 500)))
    cmds = db.q("SELECT kind, arg, state, result, created_by, created_at, done_at FROM commands WHERE kid_id=? ORDER BY id DESC LIMIT 20", (kid_id,))
    return {"events": events, "commands": cmds}


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
    games = db.q("SELECT * FROM games ORDER BY title")
    appr = db.q("SELECT * FROM approvals")
    for g in games:
        g["approved_for"] = [a["kid_id"] for a in appr if a["game_id"] == g["id"]]
        with_art(g)
    return games


@app.post("/api/parent/games", status_code=201)
async def parent_add_game(g: Game, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    for k in g.approved_for:
        kid_or_404(settings, k)
    gid = await add_game(db, g)
    for k in g.approved_for:
        db.x("INSERT OR IGNORE INTO approvals(kid_id,game_id) VALUES(?,?)", (k, gid))
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
        db.x("INSERT OR IGNORE INTO approvals(kid_id,game_id) VALUES(?,?)", (k, gid))
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
        db.x("INSERT OR IGNORE INTO approvals(kid_id,game_id) VALUES(?,?)", (kid_id, gid))
    else:
        db.x("DELETE FROM approvals WHERE kid_id=? AND game_id=?", (kid_id, gid))
    return {}


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


# --------------------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}
