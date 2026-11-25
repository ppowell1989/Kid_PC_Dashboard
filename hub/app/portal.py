"""Endpoints behind the reorganised parent dashboard (Patrick's review, 2026-11-25).

  Screen time   the usual week plus changes for single dates (holidays, vacations), shown as a calendar week.
                A dated change ends by itself, so nothing has to be put back afterwards.
  Activity      one feed of what each kid did, in plain words: played, blocked, installs, chats and calls,
                requests, time, and what the parents did.
  Family        add a kid from the dashboard (kept in the database, next to config.yaml's kids), and each
                kid's devices.
  Messages      the approved friends, with when each last called or wrote.

Everything here is a JSON API, so a phone app (or a hosted hub) can use the same calls.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import chat, rules
from .config import Kid, Settings, get_settings
from .db import DB
from .main import (HIDDEN_EVENTS, WEB_FILTER_WORDS, PlayWindow, date_change, day_rule, get_db, kid_or_404, playtime_json,
                   seed_playtimes)
from .security import current_parent

router = APIRouter()


# --------------------------------------------------------------------------- screen time calendar

def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _day_json(db: DB, kid: Kid, d: date) -> dict:
    r = day_rule(db, kid, d)
    ch = date_change(db, kid, d)
    return {"date": d.isoformat(), "weekday": d.weekday(), "daily_minutes": r.daily_minutes, "bedtime": r.bedtime,
            "windows": [{"start": s, "end": e} for s, e in r.windows],
            "changed": bool(ch), "label": ch["label"] if ch else "", "changed_by": ch["updated_by"] if ch else ""}


@router.get("/api/parent/screentime")
def screentime(start: str = "", parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """One calendar week (Monday first) for every kid, plus the usual week and the dated changes still to come."""
    today = rules.local_now(settings.timezone).date()
    try:
        monday = _monday(date.fromisoformat(start)) if start else _monday(today)
    except ValueError:
        raise HTTPException(422, "start is a date like 2026-12-21")
    days = [monday + timedelta(days=i) for i in range(7)]
    out = []
    for k in settings.kids:
        upcoming = db.q("SELECT day, label, daily_minutes FROM playtime_dates WHERE kid_id=? AND day>=? ORDER BY day LIMIT 60",
                        (k.id, today.isoformat()))
        out.append({"id": k.id, "name": k.name, "week": [_day_json(db, k, d) for d in days],
                    "usual": [playtime_json(db, k, wd) for wd in range(7)], "upcoming": _ranges(upcoming)})
    return {"today": today.isoformat(), "start": monday.isoformat(), "kids": out}


def _ranges(rows: list[dict]) -> list[dict]:
    """Back-to-back dates with the same label read as one change: "Beach week, Dec 26 to Dec 31"."""
    out: list[dict] = []
    for r in rows:
        d = date.fromisoformat(r["day"])
        if out and out[-1]["label"] == r["label"] and date.fromisoformat(out[-1]["to"]) + timedelta(days=1) == d:
            out[-1]["to"] = r["day"]
            out[-1]["days"] += 1
        else:
            out.append({"from": r["day"], "to": r["day"], "label": r["label"], "days": 1, "daily_minutes": r["daily_minutes"]})
    return out


class DateChange(BaseModel):
    dates: list[str] = Field(min_length=1, max_length=62)
    daily_minutes: int = Field(ge=0, le=24 * 60)
    bedtime: str = Field(default="", pattern=r"^(\d{2}:\d{2})?$")
    windows: list[PlayWindow] = Field(default_factory=list, max_length=6)
    label: str = Field(default="", max_length=40)


def _dates(raw: list[str]) -> list[date]:
    try:
        return sorted({date.fromisoformat(d) for d in raw})
    except ValueError:
        raise HTTPException(422, "dates look like 2026-12-24")


def _check_windows(wins: list[tuple[str, str]]) -> None:
    for s, e in wins:
        if e <= s:
            raise HTTPException(422, f"play time {s} to {e} ends before it starts")
    for (s1, e1), (s2, _) in zip(wins, wins[1:]):
        if s2 < e1:
            raise HTTPException(422, f"play times {s1}-{e1} and {s2} overlap")


@router.put("/api/parent/screentime/{kid_id}/dates")
def set_dates(kid_id: str, c: DateChange, parent: str = Depends(current_parent), db: DB = Depends(get_db),
              settings: Settings = Depends(get_settings)):
    """Holiday, vacation, a late night: these dates get their own rule, and go back to the usual week by themselves."""
    kid = kid_or_404(settings, kid_id)
    wins = sorted((w.start, w.end) for w in c.windows)
    _check_windows(wins)
    with db.tx() as x:
        for d in _dates(c.dates):
            x.execute("""INSERT INTO playtime_dates(kid_id,day,daily_minutes,bedtime,windows,label,updated_by) VALUES(?,?,?,?,?,?,?)
                         ON CONFLICT(kid_id,day) DO UPDATE SET daily_minutes=excluded.daily_minutes, bedtime=excluded.bedtime,
                         windows=excluded.windows, label=excluded.label, updated_by=excluded.updated_by""",
                      (kid.id, d.isoformat(), c.daily_minutes, c.bedtime, json.dumps([list(w) for w in wins]), c.label.strip(), parent))
    return {"ok": True}


class DateList(BaseModel):
    dates: list[str] = Field(min_length=1, max_length=62)


@router.post("/api/parent/screentime/{kid_id}/dates/clear")
def clear_dates(kid_id: str, body: DateList, parent: str = Depends(current_parent), db: DB = Depends(get_db),
                settings: Settings = Depends(get_settings)):
    """Back to the usual week for these dates."""
    kid = kid_or_404(settings, kid_id)
    for d in _dates(body.dates):
        db.x("DELETE FROM playtime_dates WHERE kid_id=? AND day=?", (kid.id, d.isoformat()))
    return {"ok": True}


class CopyWeek(BaseModel):
    start: str                                   # any date in the week to copy
    weeks: int = Field(default=1, ge=1, le=8)    # how many weeks after it get the same days


@router.post("/api/parent/screentime/{kid_id}/copy-week")
def copy_week(kid_id: str, body: CopyWeek, parent: str = Depends(current_parent), db: DB = Depends(get_db),
              settings: Settings = Depends(get_settings)):
    """Copy one week, as it is (changes included), onto the following week(s) as dated changes."""
    kid = kid_or_404(settings, kid_id)
    try:
        monday = _monday(date.fromisoformat(body.start))
    except ValueError:
        raise HTTPException(422, "start is a date like 2026-12-21")
    label = f"Same as week of {monday.strftime('%b')} {monday.day}"
    n = 0
    week = [(monday + timedelta(days=i), day_rule(db, kid, monday + timedelta(days=i))) for i in range(7)]   # read before the
    with db.tx() as x:                                                                                       # write lock is held
        for src, r in week:
            for w in range(1, body.weeks + 1):
                x.execute("""INSERT INTO playtime_dates(kid_id,day,daily_minutes,bedtime,windows,label,updated_by) VALUES(?,?,?,?,?,?,?)
                             ON CONFLICT(kid_id,day) DO UPDATE SET daily_minutes=excluded.daily_minutes, bedtime=excluded.bedtime,
                             windows=excluded.windows, label=excluded.label, updated_by=excluded.updated_by""",
                          (kid.id, (src + timedelta(weeks=w)).isoformat(), r.daily_minutes, r.bedtime,
                           json.dumps([list(t) for t in r.windows]), label, parent))
                n += 1
    return {"copied": n}


# --------------------------------------------------------------------------- activity feed

# What each PC event means for a parent, and which group it belongs to. {k} is the kid's name.
EVENT_WORDS: dict[str, tuple[str, str]] = {
    "game_blocked": ("blocked", "{k} tried to open a game that isn't approved: {d}"),
    "browser_blocked": ("blocked", "{k} opened another browser; it was closed: {d}"),
    "tool_blocked": ("blocked", "{k} opened a Windows tool he shouldn't have; it was closed: {d}"),
    "lockdown_restored": ("blocked", "A Windows setting was changed on {k}'s PC and was put back: {d}"),
    "shell_restarted": ("blocked", "{k}'s screen was closed and was started again"),
    "game_install_started": ("installs", "Started installing {d}"),
    "game_installed": ("installs", "{d} installed"),
    "game_removed": ("installs", "Taken off {k}'s PC: {d}"),
    "game_install_failed": ("installs", "Didn't install: {d}"),
    "game_install_no_space": ("installs", "{k}'s PC is full: {d}"),
    "game_install_not_owned": ("installs", "Not bought on his account yet: {d}"),
    "game_install_needs_signin": ("installs", "Store needs a sign-in: {d}"),
    "app_installed": ("installs", "Installed {d}"),
    "app_install_failed": ("installs", "Couldn't install {d}"),
    "launcher_missing": ("installs", "Needs a launcher: {d}"),
    "launch_failed": ("installs", "A game wouldn't start: {d}"),
    "pc_setup": ("installs", "PC set up: {d}"),
    "web_filter": ("installs", "Web filter set: {d}"),
    "stopped_time_up": ("time", "Time ran out, so {d} was closed"),
    "offline_play": ("time", "{d}"),
    "points_spent": ("time", "{k}: {d}"),
    "points_returned": ("parents", "{d}"),
}
GROUPS = ("played", "blocked", "installs", "chats", "requests", "time", "parents")


def _local(ts: str, tz: str) -> str:
    """SQLite's datetime('now') is UTC with a space; the PC's times are local ISO. One shape for sorting."""
    if not ts:
        return ""
    if "T" in ts:
        return ts[:19]
    d = datetime.fromisoformat(ts.replace(" ", "T") + "+00:00")
    return d.astimezone(rules.local_now(tz).tzinfo).isoformat(timespec="seconds")[:19]


def _minutes(a: str, b: str) -> int:
    return max(1, round((datetime.fromisoformat(b[:19]) - datetime.fromisoformat(a[:19])).total_seconds() / 60) + 1)


@router.get("/api/parent/feed")
def feed(kid: str = "", days: int = 7, group: str = "", parent: str = Depends(current_parent), db: DB = Depends(get_db),
         settings: Settings = Depends(get_settings)):
    tz = settings.timezone
    kids = [kid_or_404(settings, kid)] if kid else settings.kids
    since = (rules.local_now(tz) - timedelta(days=max(1, min(days, 90)) - 1)).strftime("%Y-%m-%d")
    since_utc = (datetime.now() - timedelta(days=max(1, min(days, 90)))).strftime("%Y-%m-%d")
    items: list[dict] = []

    def add(k: Kid, grp: str, at: str, text: str, **more) -> None:
        if not group or group == grp:
            # Every time as the hub's local clock, without an offset, like the PC's own reports.
            if more.get("until"):
                more["until"] = more["until"][:19]
            items.append({"kid_id": k.id, "group": grp, "at": at[:19], "text": text, **more})

    friend_names = {chat.friend_conv(i["code"]): i["friend_name"] for i in db.q("SELECT code, friend_name FROM invites")}
    for k in kids:
        joins = db.q("SELECT detail, at FROM events WHERE kid_id=? AND kind='friend_joined_voice' AND at>=?", (k.id, since))
        for s in db.q("SELECT * FROM sessions WHERE kid_id=? AND started_at>=? ORDER BY id", (k.id, since)):
            mins = _minutes(s["started_at"], s["ended_at"])
            if s["kind"] == "play":
                add(k, "played", s["started_at"], f"{k.name} played {s['what']}", minutes=mins, until=s["ended_at"])
            else:
                start = (datetime.fromisoformat(s["started_at"]) - timedelta(minutes=2)).isoformat()
                who = sorted({j["detail"] for j in joins if start <= j["at"] <= s["ended_at"]})
                room = "Family voice" if s["what"] == "family" else "Voice call"
                with_ = f" with {', '.join(who)}" if who else (" (nobody else joined)" if s["what"] != "family" else "")
                add(k, "chats", s["started_at"], f"{room}{with_}", minutes=mins, until=s["ended_at"])
        # Text chat: one line per conversation per day, with how many messages he sent.
        for r in db.q("""SELECT conv, substr(at,1,10) day, COUNT(*) n, MIN(at) first, MAX(at) last FROM chat
                         WHERE kid_id=? AND at>=? GROUP BY conv, day""", (k.id, since)):
            title = ("the family chat" if r["conv"] == chat.FAMILY else "the kids' chat" if r["conv"] == chat.KIDS
                     else friend_names.get(r["conv"], "a friend"))
            add(k, "chats", r["first"][:19], f"{k.name} texted with {title}: {r['n']} message{'s' if r['n'] != 1 else ''}")
        last_filter = None
        for e in db.q("SELECT kind, detail, at FROM events WHERE kid_id=? AND at>=? ORDER BY at, id", (k.id, since)):
            if e["kind"] in HIDDEN_EVENTS or e["kind"] == "friend_joined_voice":
                continue
            if e["kind"] == "web_filter":
                if e["detail"] == last_filter:
                    continue
                last_filter = e["detail"]
            grp, words = EVENT_WORDS.get(e["kind"], ("installs", e["kind"].replace("_", " ") + ": {d}"))
            d = WEB_FILTER_WORDS.get(e["detail"], e["detail"]) if e["kind"] == "web_filter" else e["detail"]
            add(k, grp, e["at"][:19], words.format(k=k.name, d=d).rstrip(": "))
        for v in db.q("SELECT domain, at FROM visits WHERE kid_id=? AND blocked=1 AND at>=? ORDER BY at DESC LIMIT 200", (k.id, since)):
            add(k, "blocked", v["at"][:19], f"{k.name} tried to visit {v['domain']} (blocked)")
        for q in db.q("SELECT term, at FROM searches WHERE kid_id=? AND flagged=1 AND at>=?", (k.id, since)):
            add(k, "blocked", q["at"][:19], f"Flagged search: “{q['term']}”")
        for r in db.q("SELECT * FROM requests WHERE kid_id=? AND created_at>=?", (k.id, since_utc)):
            what = {"time": f"{r['detail']} more minutes", "game": r["detail"], "site": r["detail"]}[r["kind"]]
            end = {"pending": "waiting for you", "approved": f"approved by {r['decided_by']}", "declined": f"declined by {r['decided_by']}"}[r["state"]]
            add(k, "requests", _local(r["created_at"], tz), f"{k.name} asked for {what}: {end}")
        for g in db.q("SELECT minutes, by, at FROM grants WHERE kid_id=? AND at>=?", (k.id, since_utc)):
            add(k, "parents", _local(g["at"], tz), f"{g['by']} gave {k.name} {g['minutes']} more minutes")
        for c in db.q("SELECT * FROM commands WHERE kid_id=? AND created_at>=?", (k.id, since_utc)):
            verb = {"message": f"sent {k.name} a message: “{c['arg']}”", "restart": f"restarted {k.name}'s PC",
                    "shutdown": f"shut down {k.name}'s PC", "install_app": f"asked {k.name}'s PC to install {c['arg']}"}[c["kind"]]
            add(k, "parents", _local(c["created_at"], tz), f"{c['created_by']} {verb}" + (" (didn't work)" if c["state"] == "failed" else ""))
        p = db.one("SELECT until, by FROM pauses WHERE kid_id=?", (k.id,))
        if p and p["until"] > rules.local_now(tz).isoformat():
            add(k, "parents", rules.local_now(tz).isoformat(timespec="seconds")[:19], f"{p['by']} paused {k.name}'s PC (still paused)")
    items.sort(key=lambda i: i["at"], reverse=True)
    counts = {g: 0 for g in GROUPS}
    for i in items:
        counts[i["group"]] += 1
    return {"items": items[:400], "counts": counts}


# --------------------------------------------------------------------------- family: kids and devices

class NewKid(BaseModel):
    name: str = Field(min_length=1, max_length=30)


@router.post("/api/parent/kids", status_code=201)
def add_kid(body: NewKid, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Add a kid. His Windows account name is made from his name (letters only), and he gets his own tokens.
    The next step is setting up his first device (a setup code, as for any PC)."""
    name = " ".join(body.name.split())
    if any(k.name.lower() == name.lower() for k in settings.kids):
        raise HTTPException(409, f"There is already someone called {name}.")
    base = re.sub(r"[^a-z]", "", name.lower())[:16] or "kid"
    kid_id, n = base, 2
    while settings.kid(kid_id) or db.one("SELECT 1 FROM kids_added WHERE id=?", (kid_id,)):
        kid_id, n = f"{base}{n}", n + 1
    kid = Kid(id=kid_id, name=name, device_token=secrets.token_urlsafe(32), extension_token=secrets.token_urlsafe(24))
    db.x("INSERT INTO kids_added(id,name,device_token,extension_token,added_by) VALUES(?,?,?,?,?)",
         (kid.id, kid.name, kid.device_token, kid.extension_token, parent))
    settings.kids.append(kid)
    seed_playtimes(db, settings)
    return {"id": kid.id, "name": kid.name}


@router.get("/api/parent/devices")
def devices(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Each kid's devices: every computer that has checked in as his (by its Windows name), plus any a setup code
    was used on that hasn't checked in yet. All of one kid's devices share his rules and his daily time."""
    out = {}
    for k in settings.kids:
        rows = {r["computer"]: {"computer": r["computer"], "name": r["name"], "set_up_at": r["first_seen"], "last_seen": r["last_seen"]}
                for r in db.q("SELECT * FROM devices WHERE kid_id=? ORDER BY last_seen DESC", (k.id,))}
        for r in db.q("""SELECT used_by AS computer, MAX(used_at) AS set_up_at FROM setup_codes
                         WHERE kid_id=? AND used_at IS NOT NULL AND used_at != '' GROUP BY used_by""", (k.id,)):
            if r["computer"] and r["computer"] not in rows:
                rows[r["computer"]] = {"computer": r["computer"], "name": "", "set_up_at": r["set_up_at"], "last_seen": ""}
        out[k.id] = {"devices": list(rows.values()), "added_here": bool(db.one("SELECT 1 FROM kids_added WHERE id=?", (k.id,)))}
    return out


class DeviceName(BaseModel):
    computer: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=40)


@router.put("/api/parent/devices/{kid_id}/name")
def name_device(kid_id: str, body: DeviceName, parent: str = Depends(current_parent), db: DB = Depends(get_db),
                settings: Settings = Depends(get_settings)):
    """A parent's name for a device ("Adam's laptop"). Empty goes back to Windows' computer name."""
    kid_or_404(settings, kid_id)
    now = datetime.now().isoformat(timespec="seconds")
    db.x("""INSERT INTO devices(kid_id,computer,name,first_seen,last_seen) VALUES(?,?,?,?,'')
            ON CONFLICT(kid_id,computer) DO UPDATE SET name=excluded.name""", (kid_id, body.computer, body.name.strip(), now))
    return {"ok": True}


# --------------------------------------------------------------------------- messages: approved friends

@router.get("/api/parent/friends")
def friends(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Approved friends (live invite links) with when each last joined a call and last wrote."""
    out = []
    for inv in db.q("SELECT code, kid_id, friend_name, created_at FROM invites WHERE revoked=0 ORDER BY created_at DESC"):
        conv = chat.friend_conv(inv["code"])
        msgs = db.one("SELECT COUNT(*) n, MAX(at) last FROM chat WHERE conv=?", (conv,))
        call = db.one("SELECT MAX(at) last, COUNT(*) n FROM events WHERE kid_id=? AND kind='friend_joined_voice' AND detail=?",
                      (inv["kid_id"], inv["friend_name"]))
        out.append({"code": inv["code"], "kid_id": inv["kid_id"], "friend_name": inv["friend_name"], "conv": conv,
                    "added": _local(inv["created_at"], settings.timezone), "messages": msgs["n"], "last_message": msgs["last"] or "",
                    "calls": call["n"], "last_call": call["last"] or ""})
    return out


# --------------------------------------------------------------------------- messages: voice calls

@router.get("/api/parent/calls")
def calls(kid: str = "", days: int = 30, parent: str = Depends(current_parent), db: DB = Depends(get_db),
          settings: Settings = Depends(get_settings)):
    """Voice calls, newest first: whose PC, which room, how long, and who else was in it (friends who joined
    with their link, and brothers or sisters in the same room at the same time)."""
    from .main import since
    names = {k.id: k.name for k in settings.kids}
    rows = db.q("SELECT * FROM sessions WHERE kind='voice' AND started_at >= ? ORDER BY started_at DESC LIMIT 200",
                (since(settings, days),))
    out = []
    for r in rows:
        if kid and r["kid_id"] != kid:
            continue
        start, end = r["started_at"][:19], r["ended_at"][:19]
        early = (datetime.fromisoformat(start) - timedelta(minutes=3)).isoformat(timespec="seconds")
        friends = [e["detail"] for e in db.q("""SELECT DISTINCT detail FROM events WHERE kid_id=? AND kind='friend_joined_voice'
                                               AND substr(at,1,19) BETWEEN ? AND ?""", (r["kid_id"], early, end))]
        others = [names.get(o["kid_id"], o["kid_id"]) for o in db.q(
            """SELECT DISTINCT kid_id FROM sessions WHERE kind='voice' AND what=? AND kid_id!=?
               AND substr(started_at,1,19) <= ? AND substr(ended_at,1,19) >= ?""", (r["what"], r["kid_id"], end, start))]
        minutes = max(1, round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60))
        out.append({"kid_id": r["kid_id"], "room": "Family voice" if r["what"] == "family" else "Friends' voice",
                    "started_at": start, "ended_at": end, "minutes": minutes, "with": others + friends})
    return out
