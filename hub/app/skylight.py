"""Skylight chore points: the kids see their points on the home screen and can spend them on extra time.

Skylight (the family calendar) has a chore chart where kids earn reward points. It has no public API;
this talks to the API its own apps use, as worked out by the open-source skylight-mcp project
(github.com/chrischall/skylight-mcp, docs/MOBILE_API_FINDINGS.md). It is unofficial and can change.
Every call here is best effort: when Skylight doesn't answer, the points card hides and nothing else breaks.

Sign-in: a parent types the Skylight email and password on the dashboard once. They go straight to
Skylight and are never stored; the hub keeps only the token pair Skylight hands back (kv "skylight").
If Skylight emails a 6-digit code, the dashboard asks for it and the half-finished sign-in waits here
in memory for 10 minutes.

Stored in kv "skylight" (JSON): access/refresh token, expiry, frame (household) id, which Skylight
family member each kid is (members: {kid_id: category_id}), the price (points for minutes), the daily
cap in minutes, and mode ("instant", or "approve" = a parent says yes first).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from .db import DB

AUTH = "https://app.ourskylight.com"
API = AUTH + "/api"
API_VERSION = "2026-05-01"          # Skylight's apps send this; some features answer 422 without it
CLIENT_ID = "skylight-mobile"
REDIRECT_URI = "https://ourskylight.com/welcome"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DEVICE = {"skylight_api_client_device_platform": "web", "skylight_api_client_device_name": "kids-pc-hub",
          "skylight_api_client_device_os_version": "1", "skylight_api_client_device_app_version": "1",
          "skylight_api_client_device_hardware": "server", "source": "js-mobile"}
KEY = "skylight"
DEFAULTS = {"price_points": 10, "price_minutes": 15, "daily_cap": 30, "mode": "instant", "members": {}}
BY = "Skylight points"              # grants.by for minutes bought with points (counts toward the daily cap)
BALANCE_SECONDS = 60


class SkylightError(Exception):
    """Something a parent can read: wrong password, Skylight unreachable, not linked."""


def load(db: DB) -> dict:
    row = db.one("SELECT value FROM kv WHERE key=?", (KEY,))
    cfg = {**DEFAULTS, **(json.loads(row["value"]) if row else {})}
    # Skylight ids are compared as strings everywhere (balances are keyed by str); older saves held numbers
    cfg["members"] = {k: str(v) for k, v in (cfg.get("members") or {}).items() if v not in (None, "")}
    if cfg.get("frame_id") is not None:
        cfg["frame_id"] = str(cfg["frame_id"])
    return cfg


def save(db: DB, cfg: dict) -> None:
    db.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, json.dumps(cfg)))


def linked(cfg: dict) -> bool:
    return bool(cfg.get("refresh_token") or cfg.get("access_token"))


# ---------------------------------------------------------------- sign-in

_pending: dict[str, dict] = {}       # sign-ins waiting for the emailed code: id -> {client, form, verifier, at}


def _form_token(html: str) -> str | None:
    m = (re.search(r'name=["\']authenticity_token["\'][^>]*value=["\']([^"\']+)', html)
         or re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']authenticity_token', html)
         or re.search(r'name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)', html))
    return m.group(1) if m else None


def _path(r: httpx.Response) -> str:
    loc = r.headers.get("location")
    return urlparse(urljoin(str(r.request.url), loc)).path if r.is_redirect and loc else ""


async def sign_in(email: str, password: str) -> dict:
    """Step one. Returns {"tokens": {...}} when done, or {"code_needed": id} when Skylight emailed a code."""
    c = httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}, follow_redirects=False)
    try:
        page = await c.get(f"{AUTH}/auth/session/new")
        token = _form_token(page.text)
        if not token:
            raise SkylightError(f"Skylight's sign-in page didn't load (HTTP {page.status_code}). Try again in a minute.")
        r = await c.post(f"{AUTH}/auth/session", data={"authenticity_token": token, "email": email, "password": password},
                         headers={"Origin": AUTH, "Referer": f"{AUTH}/auth/session/new"})
        path = _path(r)
        if path == "/auth/session/success":
            return {"tokens": await _finish(c)}
        if path.startswith("/auth/mfa_challenge"):
            ch = await c.get(urljoin(f"{AUTH}/auth/session", r.headers["location"]), follow_redirects=True)
            form = _form_token(ch.text)
            if not form:
                raise SkylightError("Skylight asked for an emailed code, but its code page didn't load. Try again.")
            pid = secrets.token_urlsafe(12)
            _pending[pid] = {"client": c, "form": form, "at": time.time()}
            c = None   # kept open for the code
            return {"code_needed": pid}
        raise SkylightError("Skylight didn't accept that email and password (or it is limiting sign-ins for a few minutes).")
    except httpx.HTTPError:
        raise SkylightError("Couldn't reach Skylight from the hub.")
    finally:
        if c is not None:
            await c.aclose()


async def submit_code(pid: str, code: str) -> dict:
    p = _pending.get(pid)
    if not p or time.time() - p["at"] > 600:
        _pending.pop(pid, None)
        raise SkylightError("That sign-in timed out. Start again.")
    c = p["client"]
    try:
        r = await c.post(f"{AUTH}/auth/mfa_challenge", data={"authenticity_token": p["form"], "code": re.sub(r"\s", "", code)},
                         headers={"Origin": AUTH, "Referer": f"{AUTH}/auth/mfa_challenge/new"})
        path = _path(r)
        if path == "/auth/session/success":
            _pending.pop(pid, None)
            tokens = await _finish(c)
            await c.aclose()
            return {"tokens": tokens}
        if path.startswith("/auth/mfa_challenge"):
            again = await c.get(urljoin(f"{AUTH}/auth/mfa_challenge", r.headers["location"]), follow_redirects=True)
            p["form"] = _form_token(again.text) or p["form"]
            raise SkylightError("Skylight didn't accept that code. Check the newest email and try again.")
        _pending.pop(pid, None)
        await c.aclose()
        raise SkylightError("Skylight ended the sign-in. Start again.")
    except httpx.HTTPError:
        raise SkylightError("Couldn't reach Skylight from the hub.")


async def _finish(c: httpx.AsyncClient) -> dict:
    """OAuth authorization code with PKCE, then the token exchange."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = f"{AUTH}/oauth/authorize?" + urlencode({"client_id": CLIENT_ID, "response_type": "code", "scope": "everything",
                                                   "redirect_uri": REDIRECT_URI, "code_challenge": challenge,
                                                   "code_challenge_method": "S256"})
    code = None
    for _ in range(4):
        r = await c.get(url)
        loc = r.headers.get("location")
        if not r.is_redirect or not loc:
            break
        dest = urljoin(url, loc)
        if dest.startswith(REDIRECT_URI):
            code = parse_qs(urlparse(dest).query).get("code", [None])[0]
            break
        if urlparse(dest).netloc != urlparse(AUTH).netloc:
            break
        url = dest
    if not code:
        raise SkylightError("Skylight signed in but didn't hand over access. Its sign-in may have changed.")
    r = await c.post(f"{AUTH}/oauth/token", data={"grant_type": "authorization_code", "client_id": CLIENT_ID, "scope": "everything",
                                                  "skylight_api_client_device_fingerprint": secrets.token_hex(16), **DEVICE,
                                                  "redirect_uri": REDIRECT_URI, "code": code, "code_verifier": verifier},
                     headers={"Accept": "application/json"})
    return _tokens(r)


def _tokens(r: httpx.Response) -> dict:
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code >= 300 or not j.get("access_token"):
        raise SkylightError(f"Skylight refused the sign-in token (HTTP {r.status_code}).")
    return {"access_token": j["access_token"], "refresh_token": j.get("refresh_token", ""),
            "expires_at": time.time() + float(j.get("expires_in") or 604800)}


# ---------------------------------------------------------------- the API

_lock = asyncio.Lock()


async def _access(db: DB, cfg: dict) -> str:
    if not linked(cfg):
        raise SkylightError("Skylight isn't linked.")
    if cfg.get("access_token") and time.time() < cfg.get("expires_at", 0) - 300:
        return cfg["access_token"]
    async with _lock:
        cfg.update({k: v for k, v in load(db).items() if k in ("access_token", "refresh_token", "expires_at")})
        if cfg.get("access_token") and time.time() < cfg.get("expires_at", 0) - 300:
            return cfg["access_token"]
        try:
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as c:
                r = await c.post(f"{AUTH}/oauth/token", data={"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"],
                                                             "client_id": CLIENT_ID}, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            raise SkylightError("Couldn't reach Skylight from the hub.")
        t = _tokens(r)
        t["refresh_token"] = t["refresh_token"] or cfg["refresh_token"]
        cur = load(db)
        cur.update(t)
        save(db, cur)
        cfg.update(t)
        return t["access_token"]


async def call(db: DB, method: str, path: str, body: dict | None = None) -> dict | list:
    cfg = load(db)
    token = await _access(db, cfg)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.request(method, API + path, json=body, headers={
                "Authorization": f"Bearer {token}", "skylight-api-version": API_VERSION,
                "Accept": "application/json", "User-Agent": UA})
    except httpx.HTTPError:
        raise SkylightError("Couldn't reach Skylight from the hub.")
    if r.status_code == 401:
        raise SkylightError("Skylight signed the hub out. Link it again on the dashboard.")
    if r.status_code >= 300:
        raise SkylightError(f"Skylight said no (HTTP {r.status_code}).")
    return r.json() if r.content else {}


def _rows(doc: dict | list) -> list[dict]:
    """JSON:API rows flattened: id, type, attributes, and each to-one relationship's id as <name>_id."""
    if isinstance(doc, list):   # reward_points answers a plain array: [{category_id, current_point_balance, lifetime_points_earned}]
        return [{**r, "id": str(r.get("id", r.get("category_id", "")))} for r in doc if isinstance(r, dict)]
    data = doc.get("data") or []
    out = []
    for d in data if isinstance(data, list) else [data]:
        # attributes can carry their own numeric "id"; the string one from the row wins, so ids always match
        row = {**(d.get("attributes") or {}), "id": str(d.get("id", "")), "type": d.get("type", "")}
        for name, rel in (d.get("relationships") or {}).items():
            if isinstance(rel, dict) and isinstance(rel.get("data"), dict):
                row[f"{name}_id"] = str(rel["data"].get("id", ""))
        out.append(row)
    return out


async def frames(db: DB) -> list[dict]:
    return [{"id": r["id"], "name": r.get("name") or r.get("title") or f"Skylight {r['id']}"} for r in _rows(await call(db, "GET", "/frames"))]


async def people(db: DB, frame: str) -> list[dict]:
    """Family members on the chore chart (Skylight calls them categories)."""
    rows = _rows(await call(db, "GET", f"/frames/{frame}/categories"))
    return [{"id": r["id"], "name": r.get("label") or r["id"]} for r in rows
            if r.get("linked_to_profile") or r.get("selected_for_chore_chart")] or [{"id": r["id"], "name": r.get("label") or r["id"]} for r in rows]


def _balance_of(row: dict) -> int | None:
    for k in ("current_point_balance", "point_balance", "balance", "current_balance", "points", "current_points"):
        if isinstance(row.get(k), (int, float)):
            return int(row[k])
    for k, v in row.items():   # the shape isn't documented: take the first number that looks like a balance
        if "balance" in k and isinstance(v, (int, float)):
            return int(v)
    return None


def _member_of(row: dict) -> str:
    return str(row.get("category_id") or row.get("category") or row.get("id"))


_balances: dict = {"at": 0.0, "by_member": {}, "error": ""}


async def balances(db: DB, fresh: bool = False) -> dict[str, int]:
    """Points per Skylight member, cached a minute."""
    if not fresh and time.time() - _balances["at"] < BALANCE_SECONDS:
        return _balances["by_member"]
    cfg = load(db)
    try:
        rows = _rows(await call(db, "GET", f"/frames/{cfg['frame_id']}/reward_points"))
        _balances.update(at=time.time(), by_member={_member_of(r): b for r in rows if (b := _balance_of(r)) is not None}, error="")
    except SkylightError as e:
        _balances.update(at=time.time(), error=str(e))
    return _balances["by_member"]


async def add_points(db: DB, member: str, points: int) -> None:
    cfg = load(db)
    await call(db, "POST", f"/frames/{cfg['frame_id']}/reward_points", {"category_ids": [member], "points": points})
    if member in _balances["by_member"]:
        _balances["by_member"][member] += points


def bought_today(db: DB, kid_id: str, day: str) -> int:
    return (db.one("SELECT COALESCE(SUM(minutes),0) AS m FROM grants WHERE kid_id=? AND day=? AND by=?", (kid_id, day, BY)) or {"m": 0})["m"]


def purchases(db: DB, kid_id: str, day: str) -> list[dict]:
    """Today's minutes bought with points, newest first, so a parent can give one back."""
    return db.q("SELECT id, minutes, points, at FROM grants WHERE kid_id=? AND day=? AND by=? ORDER BY id DESC", (kid_id, day, BY))


def offer(db: DB, kid_id: str, day: str, lock_reason: str, minutes_to_stop: int) -> dict | None:
    """What the kid's screen shows: his points, the price, and whether he can buy now (and if not, why, in his words).
    None when Skylight isn't set up for him. Uses the cached balance; never waits on Skylight."""
    cfg = load(db)
    member = cfg["members"].get(kid_id)
    if not linked(cfg) or not member or not cfg.get("frame_id"):
        return None
    bal = _balances["by_member"].get(member)
    if bal is None:
        return None
    left = max(0, cfg["daily_cap"] - bought_today(db, kid_id, day))
    why = ("You've bought all the extra time you can today." if left < cfg["price_minutes"]
           else "Not while Mom or Dad paused the PC." if lock_reason == "paused"
           else "Not outside play time." if lock_reason in ("bedtime", "outside_hours")
           else "Bedtime is too close." if minutes_to_stop < cfg["price_minutes"]
           else f"You need {cfg['price_points']} points." if bal < cfg["price_points"] else "")
    return {"balance": bal, "price_points": cfg["price_points"], "price_minutes": cfg["price_minutes"],
            "can_buy": not why, "why": why, "left_today": left}


async def refresh_forever(db: DB) -> None:
    """Keep the balances fresh so the kids' screens never wait on Skylight."""
    while True:
        try:
            cfg = load(db)
            if linked(cfg) and cfg.get("frame_id") and cfg["members"]:
                await balances(db, fresh=True)
        except Exception:
            pass
        await asyncio.sleep(BALANCE_SECONDS)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()
