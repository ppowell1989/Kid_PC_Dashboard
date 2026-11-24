"""Xbox play time shares the kids' daily time, through Microsoft Family Safety.

An Xbox already needs each kid signed in with a Microsoft account in the parent's Microsoft family. The hub
signs in as the parent through the same private service the Family Safety phone app uses (via the open-source
pyfamilysafety library). Like the Nintendo link it is not a public API: Microsoft can change it, and then this
stops until the library catches up. The PCs are not affected.

Every few minutes, once a parent has linked the account:
  1. Reads each family member's Xbox screen time today and the games played.
  2. Counts the minutes of members a parent matched to a kid into that kid's day (rules.compute sees PC +
     consoles as one "used" number).
  3. For a member set to "lock the Xbox when time is up": when the kid is locked on the hub (out of time,
     bedtime, outside play times, paused) the hub blocks that member on Xbox until his next play time, and
     lifts the block as soon as he may play again (a parent's extra time, a new day). Blocks a parent set in
     Microsoft's own app are never lifted by the hub.

Unlike the Switch, the Xbox has no "minutes left" the hub can hand it, so the block comes at the next check
after time runs out (within a few minutes), and Family Safety's own reports can lag a few minutes too.

Stored like the Switch in the console_* tables: provider "xbox", one pseudo-device "xbox", one player per
family member.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import rules
from .db import DB

log = logging.getLogger("hub.xbox")

XBOX = "xbox"
DEVICE = "xbox"            # all of a member's Xboxes count together, so there is one row for "Xbox"

# The sign-in page Microsoft's Family Safety app uses. After signing in, the browser lands on a blank page whose
# address holds "?code=..."; the parent pastes that address back into the dashboard.
LOGIN_URL = ("https://login.live.com/oauth20_authorize.srf?cobrandid=b5d15d4b-695a-4cd5-93c6-13f551b310df"
             "&client_id=000000000004893A&response_type=code"
             "&redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf&response_mode=query"
             "&scope=service%3A%3Afamilymobile.microsoft.com%3A%3AMBI_SSL&lw=1&fl=easi2&login_hint=")


@dataclass
class MemberSnap:
    member_id: str
    name: str
    role: str
    minutes: int                                   # Xbox screen time today
    games: list[dict] = field(default_factory=list)
    blocked: bool = False                          # Xbox blocked right now (by anyone)


# Microsoft client --------------------------------------------------------------------------------------
# Microsoft hands out a new refresh token whenever the old one is used, so every call returns the token to
# store next. Tests replace these three functions.

_client: dict = {}    # {"fs": FamilySafety, "auth": Authenticator}


async def finish_login(pasted: str) -> tuple[str, str]:
    """Returns (refresh token, account label) from the pasted address of the blank sign-in page."""
    from pyfamilysafety import FamilySafety
    await drop_client()
    auth = await _create(pasted.strip(), use_refresh_token=False)
    fs = FamilySafety(auth)
    _client.update(fs=fs, auth=auth)
    return auth.refresh_token, "Microsoft family"


async def _fs(token: str):
    from pyfamilysafety import FamilySafety
    if "fs" in _client:
        return _client["fs"]
    auth = await _create(token, use_refresh_token=True)
    fs = FamilySafety(auth)
    _client.update(fs=fs, auth=auth)
    return fs


async def _create(token: str, use_refresh_token: bool):
    import aiohttp
    from pyfamilysafety import Authenticator
    session = aiohttp.ClientSession()
    try:
        return await Authenticator.create(token, use_refresh_token=use_refresh_token, client_session=session)
    except BaseException:
        await session.close()
        raise


async def drop_client() -> None:
    auth = _client.pop("auth", None)
    _client.pop("fs", None)
    if auth is not None:
        await auth.client_session.close()


def _token(fallback: str) -> str:
    auth = _client.get("auth")
    return auth.refresh_token if auth is not None and auth.refresh_token else fallback


async def snapshot(token: str, day_start: datetime, day_end: datetime) -> tuple[str, list[MemberSnap]]:
    from pyfamilysafety.enum import OverrideTarget
    fs = await _fs(token)
    await fs.update()
    out = []
    for a in fs.accounts:
        usage = await a.get_screentime_usage(start_time=day_start, end_time=day_end, platform="XBOX")
        dev = (usage.get("devices") or {}).get("deviceUsageAggregates") or {}
        apps = (usage.get("applications") or {}).get("appActivity") or []
        games = sorted(({"name": x.get("displayName") or "A game", "minutes": int((x.get("usage") or 0) // 60000)}
                        for x in apps if str(x.get("appId", "")).startswith("x:")), key=lambda g: -g["minutes"])
        name = " ".join(p for p in (a.first_name, a.surname) if p) or "Family member"
        out.append(MemberSnap(a.user_id, name, a.role or "", int((dev.get("totalScreenTime") or 0) // 60000),
                              [g for g in games if g["minutes"] > 0][:10],
                              OverrideTarget.XBOX in (a.blocked_platforms or [])))
    return _token(token), out


async def set_block(token: str, member_id: str, until: datetime | None) -> str:
    """Block the member's Xbox until `until` (aware datetime), or lift the block when until is None."""
    from pyfamilysafety.enum import OverrideTarget, OverrideType
    fs = await _fs(token)
    account = fs.get_account(member_id)
    if until is None:
        await account.override_device(OverrideTarget.XBOX, OverrideType.CANCEL, culture="en-US")
    else:
        # The library stamps the time as UTC without converting it, so hand it UTC.
        await account.override_device(OverrideTarget.XBOX, OverrideType.UNTIL,
                                      valid_until=until.astimezone(timezone.utc).replace(tzinfo=None), culture="en-US")
    return _token(token)


# Sync -------------------------------------------------------------------------------------------------

def block_until(st: rules.TimeState, now: datetime) -> datetime:
    """When to lift a block if the hub isn't around to do it: his next play time, else tomorrow morning."""
    if st.next_play:
        return datetime.fromisoformat(st.next_play)
    morning = rules.parse_hhmm(rules.DAY_START)
    return (now + timedelta(days=1)).replace(hour=morning.hour, minute=morning.minute, second=0, microsecond=0)


def save_snapshot(db: DB, members: list[MemberSnap], day: str, now: datetime) -> None:
    db.x("""INSERT INTO console_devices(provider,device_id,name,model,seen_at) VALUES(?,?,'Xbox','',?)
            ON CONFLICT(provider,device_id) DO UPDATE SET seen_at=excluded.seen_at""", (XBOX, DEVICE, now.isoformat()))
    for m in members:
        db.x("""INSERT INTO console_players(provider,device_id,player_id,nickname,role,blocked) VALUES(?,?,?,?,?,?)
                ON CONFLICT(provider,device_id,player_id) DO UPDATE SET nickname=excluded.nickname, role=excluded.role,
                  blocked=excluded.blocked""", (XBOX, DEVICE, m.member_id, m.name, m.role, int(m.blocked)))
        db.x("""INSERT INTO console_usage(provider,device_id,player_id,day,minutes,games) VALUES(?,?,?,?,?,?)
                ON CONFLICT(provider,device_id,player_id,day) DO UPDATE SET minutes=excluded.minutes, games=excluded.games""",
             (XBOX, DEVICE, m.member_id, day, m.minutes, json.dumps(m.games)))


async def sync_once(db: DB, settings, today_state) -> dict:
    link = db.one("SELECT * FROM console_links WHERE provider=?", (XBOX,))
    if not link:
        return {"linked": False}
    now = rules.local_now(settings.timezone)
    day = rules.day_key(now)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    token = link["token"]
    result = {"linked": True, "blocks": []}

    def keep(t: str) -> None:   # store the newest refresh token straight away, so a crash can't lose it
        nonlocal token
        if t and t != token:
            token = t
            db.x("UPDATE console_links SET token=? WHERE provider=?", (t, XBOX))

    try:
        t, members = await snapshot(token, start, start + timedelta(days=1) - timedelta(seconds=1))
        keep(t)
        save_snapshot(db, members, day, now)
        for m in members:
            row = db.one("SELECT kid_id, enforce, hub_block FROM console_players WHERE provider=? AND device_id=? AND player_id=?",
                         (XBOX, DEVICE, m.member_id))
            kid = settings.kid(row["kid_id"]) if row and row["kid_id"] else None
            if not kid:
                continue
            st = today_state(kid)
            if row["enforce"] and st.locked and not m.blocked:
                until = block_until(st, now)
                keep(await set_block(token, m.member_id, until))
                db.x("UPDATE console_players SET hub_block=?, blocked=1 WHERE provider=? AND device_id=? AND player_id=?",
                     (until.isoformat(), XBOX, DEVICE, m.member_id))
                result["blocks"].append({"member": m.member_id, "until": until.isoformat()})
            elif row["hub_block"] and not (row["enforce"] and st.locked):
                if m.blocked:   # only lift a block the hub itself set
                    keep(await set_block(token, m.member_id, None))
                db.x("UPDATE console_players SET hub_block='', blocked=0 WHERE provider=? AND device_id=? AND player_id=?",
                     (XBOX, DEVICE, m.member_id))
                result["blocks"].append({"member": m.member_id, "until": None})
        db.x("UPDATE console_links SET synced_at=?, error='' WHERE provider=?", (now.isoformat(), XBOX))
    except Exception as e:   # keep the hub running whatever Microsoft's servers do
        log.warning("Xbox sync failed: %s", e)
        await drop_client()
        db.x("UPDATE console_links SET error=? WHERE provider=?", (plain_error(e), XBOX))
        result["error"] = plain_error(e)
    return result


def plain_error(e: Exception) -> str:
    name, text = type(e).__name__, str(e)
    if name == "Unauthorized" or "401" in text:
        return "Microsoft signed the hub out. Connect the account again."
    if name == "RequestDenied":
        return "Microsoft refused the change. Check that this account is an organizer of the family."
    return f"Couldn't reach Microsoft ({(text or name)[:120]}). The hub will keep trying."
