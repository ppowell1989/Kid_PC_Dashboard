"""Game consoles share the kids' daily time with their PCs.

Today only the Nintendo Switch (Switch and Switch 2) is supported. The hub signs in to the parent's
Nintendo account through the same private service the "Nintendo Switch Parental Controls" phone app
uses (via the open-source pynintendoparental library, the one Home Assistant ships). That service is
not a public API: Nintendo can change it at any time, and then this stops working until the library
catches up. The PCs are not affected when that happens.

What it does every few minutes, once a parent has linked the account:
  1. Reads each Switch's play time today for every player (user profile on the Switch) and the games played.
  2. Counts the minutes of players a parent matched to a kid into that kid's day, so the PC shows less time
     left after Switch play (rules.compute sees PC + console minutes as one "used" number).
  3. For a Switch a parent set to "use <kid>'s time", sets that Switch's own daily limit to
     (minutes played on it today + the kid's minutes left). The Switch then stops the game itself when time
     runs out, even with no internet. When the kid is locked (time up, bedtime, outside play times, paused)
     the limit becomes what he has already played, which ends play right away.

Nintendo's play figures reach the cloud only when the Switch checks in (roughly every 15 minutes while
online), so console minutes can show up late on the PC, and the Switch can run a few minutes over.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from . import rules
from .db import DB

log = logging.getLogger("hub.consoles")

NINTENDO = "nintendo"
MAX_SWITCH_LIMIT = 360          # the Switch accepts 0..360 minutes a day
WRITE_SLACK = 5                 # don't rewrite a Switch's limit for a change smaller than this (except to stop play)


@dataclass
class PlayerSnap:
    player_id: str
    nickname: str
    minutes: int
    games: list[dict] = field(default_factory=list)   # [{"name": ..., "minutes": ...}]


@dataclass
class DeviceSnap:
    device_id: str
    name: str
    model: str
    limit: int | None                 # Switch's daily limit today; None = no limit
    extra_time_active: bool = False   # a parent added bonus time in Nintendo's app; the limit can't change until it ends
    stops_game: bool = False          # "Suspend software" is on, rather than an alarm only
    players: list[PlayerSnap] = field(default_factory=list)


# Nintendo client --------------------------------------------------------------------------------------
# Kept behind three small functions so tests (and later providers) can swap them out.

_pending_login = {}   # provider -> Authenticator waiting for the parent's pasted link (one at a time)
_client: dict = {}    # cached signed-in client: {"token": ..., "np": NintendoParental}


class LoginExpired(Exception):
    """The hub restarted (or another parent started over) between Connect and pasting the link."""


async def nintendo_start_login() -> str:
    from pynintendoparental import Authenticator
    old = _pending_login.pop(NINTENDO, None)
    if old is not None:
        await old.client_session.close()
    auth = Authenticator()
    _pending_login[NINTENDO] = auth
    return auth.login_url


async def nintendo_finish_login(pasted: str) -> tuple[str, str]:
    """Returns (session_token, account label). The pasted text is the "Select this person" link."""
    auth = _pending_login.pop(NINTENDO, None)
    if auth is None:
        raise LoginExpired("Start again: tap Connect, sign in, then paste the link.")
    try:
        await auth.async_complete_login(pasted.strip())
        account = getattr(auth, "account", None) or {}
        label = account.get("nickname") or account.get("screenName") or "Nintendo account"
        return auth.session_token, label
    finally:
        await auth.client_session.close()


async def _nintendo(token: str, tz: str):
    from pynintendoparental import Authenticator, NintendoParental
    c = _client.get(NINTENDO)
    if c and c["token"] == token:
        return c["np"]
    if c:
        await c["auth"].client_session.close()
    auth = Authenticator(session_token=token)
    await auth.async_complete_login(use_session_token=True)
    np = NintendoParental(auth, tz, "en-US")
    _client[NINTENDO] = {"token": token, "np": np, "auth": auth}
    return np


async def _drop_client() -> None:
    c = _client.pop(NINTENDO, None)
    if c:
        await c["auth"].client_session.close()


async def nintendo_snapshot(token: str, tz: str) -> list[DeviceSnap]:
    np = await _nintendo(token, tz)
    await np.update(monthly_summary=False)
    out = []
    for d in np.devices.values():
        players = [PlayerSnap(p.player_id, p.nickname or "Player", int(p.playing_time or 0),
                              [{"name": getattr(a.application, "name", "") or "A game", "minutes": int(a.playing_time or 0)}
                               for a in (p.apps or [])][:10])
                   for p in d.players]
        limit = d.limit_time if d.limit_time not in (None, -1) else None
        out.append(DeviceSnap(d.device_id, d.name or "Switch", d.model, int(limit) if limit is not None else None,
                              bool(d.extra_playing_time), bool(d.forced_termination_mode), players))
    return out


async def nintendo_set_limit(token: str, tz: str, device_id: str, minutes: int, stop_game: bool) -> None:
    from pynintendoparental.enum import RestrictionMode
    np = await _nintendo(token, tz)
    dev = np.devices[device_id]
    if stop_game and not dev.forced_termination_mode:
        await dev.set_restriction_mode(RestrictionMode.FORCED_TERMINATION)
    await dev.update_max_daily_playtime(minutes)


# Rules ------------------------------------------------------------------------------------------------

def console_minutes(db: DB, kid_id: str, day: str) -> int:
    """Minutes the kid played on consoles today (players a parent matched to him)."""
    r = db.one("""SELECT COALESCE(SUM(u.minutes),0) AS m FROM console_usage u
                  JOIN console_players p ON p.provider=u.provider AND p.device_id=u.device_id AND p.player_id=u.player_id
                  WHERE p.kid_id=? AND u.day=?""", (kid_id, day))
    return r["m"] if r else 0


def console_today(db: DB, kid_id: str, day: str) -> list[dict]:
    """Per console, what the kid played today, for the parent's Now card."""
    rows = db.q("""SELECT d.name AS device, u.minutes, u.games FROM console_usage u
                   JOIN console_players p ON p.provider=u.provider AND p.device_id=u.device_id AND p.player_id=u.player_id
                   LEFT JOIN console_devices d ON d.provider=u.provider AND d.device_id=u.device_id
                   WHERE p.kid_id=? AND u.day=? AND u.minutes > 0""", (kid_id, day))
    return [{"device": r["device"] or "Switch", "minutes": r["minutes"], "games": json.loads(r["games"] or "[]")} for r in rows]


def target_limit(played_on_device: int, st: rules.TimeState) -> int:
    """The Switch's limit for today: what he has played on it plus what he has left. Locked = stop now."""
    left = 0 if st.locked else st.minutes_left
    return max(0, min(MAX_SWITCH_LIMIT, played_on_device + left))


def needs_write(current: int | None, target: int, played_on_device: int) -> bool:
    if current is None:
        return True
    if target <= played_on_device:          # time's up: make it exact so the Switch stops now
        return current > target
    return abs(current - target) >= WRITE_SLACK


# Sync -------------------------------------------------------------------------------------------------

def save_snapshot(db: DB, provider: str, devices: list[DeviceSnap], day: str, now: datetime) -> None:
    for d in devices:
        db.x("""INSERT INTO console_devices(provider,device_id,name,model,limit_now,extra_time,stops_game,seen_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(provider,device_id) DO UPDATE SET name=excluded.name, model=excluded.model,
                  limit_now=excluded.limit_now, extra_time=excluded.extra_time, stops_game=excluded.stops_game,
                  seen_at=excluded.seen_at""",
             (provider, d.device_id, d.name, d.model, d.limit, int(d.extra_time_active), int(d.stops_game), now.isoformat()))
        for p in d.players:
            db.x("""INSERT INTO console_players(provider,device_id,player_id,nickname) VALUES(?,?,?,?)
                    ON CONFLICT(provider,device_id,player_id) DO UPDATE SET nickname=excluded.nickname""",
                 (provider, d.device_id, p.player_id, p.nickname))
            # Nintendo reports today's running total, so the newest figure replaces the old one.
            db.x("""INSERT INTO console_usage(provider,device_id,player_id,day,minutes,games) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(provider,device_id,player_id,day) DO UPDATE SET minutes=excluded.minutes, games=excluded.games""",
                 (provider, d.device_id, p.player_id, day, p.minutes, json.dumps(p.games)))


async def sync_once(db: DB, settings, today_state) -> dict:
    """One pass for every linked console account. today_state(kid) -> rules.TimeState (PC + consoles)."""
    link = db.one("SELECT * FROM console_links WHERE provider=?", (NINTENDO,))
    if not link:
        return {"linked": False}
    now = rules.local_now(settings.timezone)
    day = rules.day_key(now)
    result = {"linked": True, "writes": []}
    try:
        devices = await nintendo_snapshot(link["token"], settings.timezone)
        save_snapshot(db, NINTENDO, devices, day, now)
        for d in devices:
            row = db.one("SELECT limit_kid, stop_game FROM console_devices WHERE provider=? AND device_id=?", (NINTENDO, d.device_id))
            kid = settings.kid(row["limit_kid"]) if row and row["limit_kid"] else None
            if not kid:
                continue
            if d.extra_time_active:
                continue   # Nintendo refuses limit changes while bonus time from its own app is running
            played = sum(p.minutes for p in d.players)
            target = target_limit(played, today_state(kid))
            if needs_write(d.limit, target, played):
                await nintendo_set_limit(link["token"], settings.timezone, d.device_id, target, bool(row["stop_game"]))
                db.x("UPDATE console_devices SET limit_now=?, limit_set_at=? WHERE provider=? AND device_id=?",
                     (target, now.isoformat(), NINTENDO, d.device_id))
                result["writes"].append({"device_id": d.device_id, "limit": target})
        db.x("UPDATE console_links SET synced_at=?, error='' WHERE provider=?", (now.isoformat(), NINTENDO))
    except Exception as e:   # keep the hub running whatever Nintendo's servers do
        log.warning("Nintendo sync failed: %s", e)
        await _drop_client()
        db.x("UPDATE console_links SET error=? WHERE provider=?", (plain_error(e), NINTENDO))
        result["error"] = plain_error(e)
    return result


def plain_error(e: Exception) -> str:
    text = str(e) or type(e).__name__
    if "InvalidSessionToken" in type(e).__name__ or "401" in text or "invalid_grant" in text:
        return "Nintendo signed the hub out. Connect the account again."
    if "NoDevices" in type(e).__name__:
        return "This Nintendo account has no Switch linked to the Parental Controls app."
    return f"Couldn't reach Nintendo ({text[:120]}). The hub will keep trying."


async def sync_forever(db: DB, settings, today_state, every_seconds: int) -> None:
    while True:
        try:
            await sync_once(db, settings, today_state)
        except Exception:
            log.exception("console sync")
        await asyncio.sleep(every_seconds)

