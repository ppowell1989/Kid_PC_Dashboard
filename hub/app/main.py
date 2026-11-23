"""Kids PC hub: rules, reporting, requests, and voice invites for the kids' PCs.

Three audiences:
  /api/device/*  the shell, admin helper and Chrome extension on each PC (Bearer device token)
  /api/parent/*  the parents' phone dashboard (session cookie)
  /join/*        friends joining voice chat from any browser (invite code)
"""
from __future__ import annotations

import asyncio
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

from . import adguard, rules
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
    task = None
    if settings.adguard.enabled and os.environ.get("HUB_DISABLE_POLLERS") != "1":
        task = asyncio.create_task(adguard.poll_forever(app.state.db, settings))
    yield
    if task:
        task.cancel()


app = FastAPI(title="Kids PC hub", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def remote_access_gate(request: Request, call_next):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    if not origin_allowed(get_settings(), ip, request.url.path):
        return JSONResponse({"detail": "hub is home-network only"}, status_code=403)
    return await call_next(request)


# --------------------------------------------------------------------------- helpers

def today_state(db: DB, settings: Settings, kid: Kid, now: datetime | None = None) -> rules.TimeState:
    now = now or rules.local_now(settings.timezone)
    day = rules.day_key(now)
    used = (db.one("SELECT minutes FROM usage WHERE kid_id=? AND day=?", (kid.id, day)) or {}).get("minutes", 0)
    g = db.one("SELECT COALESCE(SUM(minutes),0) AS m, MAX(until) AS u FROM grants WHERE kid_id=? AND day=?", (kid.id, day))
    late_until = datetime.fromisoformat(g["u"]) if g and g["u"] else None
    return rules.compute(kid, now, used, g["m"] if g else 0, late_until)


def games_for(db: DB, settings: Settings, kid_id: str) -> list[dict]:
    rows = db.q("""SELECT g.* FROM games g JOIN approvals a ON a.game_id = g.id
                   WHERE a.kid_id=? ORDER BY g.title""", (kid_id,))
    if not settings.decisions.game_pass_enabled:
        rows = [r for r in rows if r["launcher"] != "gamepass"]
    return rows


def allowed_sites(db: DB, settings: Settings) -> list[str]:
    extra = [r["domain"] for r in db.q("SELECT domain FROM sites ORDER BY domain")]
    return sorted(set(settings.allowed_sites) | set(extra))


def kid_or_404(settings: Settings, kid_id: str) -> Kid:
    kid = settings.kid(kid_id)
    if not kid:
        raise HTTPException(404, "no such kid")
    return kid


# --------------------------------------------------------------------------- device API

class Heartbeat(BaseModel):
    running: str = ""            # title of the foreground game/app, "" when on the home screen
    active: bool = False         # true when a game or Chrome is in use (counts against time)


class KidRequest(BaseModel):
    kind: Literal["game", "time", "site"]
    detail: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)


class Visit(BaseModel):
    url: str = Field(max_length=2000)
    title: str = Field(default="", max_length=300)
    at: str                      # ISO time from the extension


@app.get("/api/device/rules")
def device_rules(kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    st = today_state(db, settings, kid)
    return {
        "kid": {"id": kid.id, "name": kid.name},
        "time": st.__dict__,
        "warning_minutes": rules.WARNING_MINUTES,
        "games": games_for(db, settings, kid.id),
        "web": {"mode": settings.decisions.web_mode, "allowed_sites": allowed_sites(db, settings)},
        "game_pass_enabled": settings.decisions.game_pass_enabled,
        "pending_requests": db.q("SELECT id,kind,detail,state,created_at FROM requests WHERE kid_id=? AND state='pending'", (kid.id,)),
        "friends": db.q("SELECT code,friend_name FROM invites WHERE kid_id=? AND revoked=0", (kid.id,)),
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
    st = today_state(db, settings, kid, now)
    db.x("""INSERT INTO status(kid_id,seen_at,running,counted_at,locked) VALUES(?,?,?,?,?)
            ON CONFLICT(kid_id) DO UPDATE SET seen_at=excluded.seen_at, running=excluded.running,
            counted_at=excluded.counted_at, locked=excluded.locked""",
         (kid.id, now.isoformat(), hb.running, counted_at, int(st.locked)))
    return {"time": st.__dict__}


@app.post("/api/device/requests", status_code=201)
def device_request(req: KidRequest, kid: Kid = Depends(current_device), db: DB = Depends(get_db)):
    if req.kind == "time" and not req.detail.isdigit():
        raise HTTPException(422, "time requests carry minutes as detail")
    dup = db.one("SELECT id FROM requests WHERE kid_id=? AND kind=? AND detail=? AND state='pending'", (kid.id, req.kind, req.detail))
    if dup:
        return {"id": dup["id"], "duplicate": True}
    rid = db.x("INSERT INTO requests(kid_id,kind,detail,note) VALUES(?,?,?,?)", (kid.id, req.kind, req.detail, req.note))
    return {"id": rid}


@app.post("/api/device/visits", status_code=204)
def device_visits(visits: list[Visit], kid: Kid = Depends(current_extension), db: DB = Depends(get_db)):
    for v in visits[:200]:
        host = (urlparse(v.url).hostname or "").lower()
        if not host:
            continue
        db.x("INSERT INTO visits(kid_id,source,domain,url,title,at) VALUES(?,?,?,?,?,?)",
             (kid.id, "chrome", host, v.url, v.title, v.at))
    return Response(status_code=204)


@app.get("/api/device/voice-token")
def device_voice_token(kid: Kid = Depends(current_device), settings: Settings = Depends(get_settings)):
    """The kid's own token for his voice room. One room per kid: friends join his room."""
    return {"url": settings.livekit.url, "room": f"room-{kid.id}",
            "token": livekit_token(settings, f"kid-{kid.id}", kid.name, f"room-{kid.id}")}


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
        })
    return {"parent": parent, "kids": out, "decisions": settings.decisions.model_dump()}


@app.get("/api/parent/visits/{kid_id}")
def parent_visits(kid_id: str, source: str | None = None, limit: int = 200,
                  parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid_or_404(settings, kid_id)
    sql, args = "SELECT * FROM visits WHERE kid_id=?", [kid_id]
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


@app.post("/api/parent/requests/{rid}")
def parent_decide(rid: int, d: Decision, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
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
            if d.game_id is None:
                raise HTTPException(422, "pick or add the catalogue game to approve")
            db.x("INSERT OR IGNORE INTO approvals(kid_id,game_id) VALUES(?,?)", (kid.id, d.game_id))
        elif r["kind"] == "site":
            db.x("INSERT OR IGNORE INTO sites(domain) VALUES(?)", (r["detail"].lower(),))
    db.x("UPDATE requests SET state=?, decided_by=?, decided_at=datetime('now') WHERE id=?",
         ("approved" if d.approve else "declined", parent, rid))
    return db.one("SELECT * FROM requests WHERE id=?", (rid,))


def grant_time(db: DB, settings: Settings, kid: Kid, minutes: int, parent: str) -> None:
    now = rules.local_now(settings.timezone)
    st = today_state(db, settings, kid, now)
    until = ""
    if st.lock_reason == "bedtime" or st.minutes_to_bedtime < minutes:
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


# Catalogue ------------------------------------------------------------------

class Game(BaseModel):
    title: str = Field(min_length=1, max_length=120)
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
    return games


@app.post("/api/parent/games", status_code=201)
def parent_add_game(g: Game, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    gid = db.x("INSERT INTO games(title,launcher,launch_ref,rating,image_url) VALUES(?,?,?,?,?)",
               (g.title, g.launcher, g.launch_ref, g.rating, g.image_url))
    for k in g.approved_for:
        kid_or_404(settings, k)
        db.x("INSERT OR IGNORE INTO approvals(kid_id,game_id) VALUES(?,?)", (k, gid))
    return {"id": gid}


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
    db.x("UPDATE invites SET revoked=1 WHERE code=?", (code,))
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
