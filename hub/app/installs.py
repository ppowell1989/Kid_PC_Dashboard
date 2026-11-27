"""Putting approved games on the kids' PCs, the way a school's IT office pushes software to its computers.

The parent approves a game; the hub adds it to that PC's install list; the PC's helper installs it by itself
and reports how far it got with every heartbeat. This module turns those reports into words a parent can act
on without knowing what a launcher or a manifest is. The wording lives here, in the hub, so the dashboard and
any later phone app say the same thing.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# What the helper may report for one game on one PC.
STATES = (
    "queued",         # the hub wants it there; the PC hasn't picked it up yet
    "waiting",        # the PC has it on its list but something else has to happen first (see detail)
    "installing",     # downloading or unpacking; done_bytes/total_bytes say how far
    "installed",
    "needs_signin",   # the store app (Steam, Battle.net, Epic Games) has to be signed in on that PC once
    "not_owned",      # the store says the kid's account doesn't have this game
    "no_space",       # the PC's drive is too full
    "stuck",          # the store didn't start it; Try again usually does
    "failed",
    "manual",         # this kind of game can't be installed from afar (yet)
    "not_installed",  # approved, but not on the install list and not on the PC
    "removing",       # a parent asked for it to come off the PC; the PC is on it (see stage/detail)
)
REPORTABLE = tuple(s for s in STATES if s != "queued")

STORE = {"steam": "Steam", "battlenet": "Battle.net", "epic": "the Epic Games app", "gamepass": "the Xbox app", "direct": "the home server"}


def size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.0f} MB"
    return f"{max(n, 0) // 1024} KB"


def rate_text(bps: float) -> str:
    return f"{bps / (1 << 20):.1f} MB/s" if bps >= 1 << 20 else f"{max(bps, 0) / 1024:.0f} KB/s"


def left_text(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "under a minute left"
    if m < 60:
        return f"about {m} min left"
    return f"about {m // 60} h {m % 60:02d} min left"


def speed(prev: dict, state: str, done: int, now: datetime) -> float:
    """Download speed in bytes a second, from progress between two reports, smoothed so one slow minute doesn't swing it."""
    if state != "installing" or prev.get("state") != "installing" or not prev.get("at"):
        return 0.0
    try:
        secs = (now - datetime.fromisoformat(prev["at"])).total_seconds()
    except ValueError:
        return float(prev.get("rate") or 0)
    if secs <= 0 or secs > 300:
        return float(prev.get("rate") or 0)
    now_rate = max(0, done - (prev.get("done_bytes") or 0)) / secs
    old = float(prev.get("rate") or 0)
    return now_rate if not old else 0.5 * old + 0.5 * now_rate


def ago(iso: str | None, now: datetime) -> str:
    if not iso:
        return ""
    try:
        at = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    mins = int((now - at).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    return f"at {at.strftime('%-I:%M %p')}" if (now - at) < timedelta(hours=20) else f"on {at.strftime('%a %-I:%M %p')}"


def since(row: dict, now: datetime) -> str:
    """When this game's install last moved (state, stage or progress), for the line under it on the PCs page."""
    if row.get("state") in ("installed", "not_installed", None) or not row.get("install"):
        return ""
    when = ago(row.get("changed_at") or row.get("at"), now)
    seen = ago(row.get("at"), now)
    if not when:
        return ""
    return f"Last change {when}" + (f", PC checked in {seen}" if seen and seen != when else "")


def plain(row: dict, kid_name: str, online: bool, launcher_app: dict | None) -> dict:
    """One game on one PC, in parent words.

    Returns label (a few words for the pill), text (one sentence on what is happening and what, if anything,
    the parent can do), tone (ok, busy, needs, bad, off) and fix (retry, install, signin, remove, buy, bought or ""). can_remove marks an installed game the PCs page can take off."""
    state, detail = row.get("state") or "queued", (row.get("detail") or "").strip()
    store = STORE.get(row.get("launcher", ""), "its store")
    pc = f"{kid_name}'s PC"
    done, total = row.get("done_bytes") or 0, row.get("total_bytes") or 0
    stage, rate = (row.get("stage") or "").strip(), float(row.get("rate") or 0)

    if row.get("remove"):
        return removing(row, state, detail, stage, pc, online)
    if state == "installed":
        return {"label": "Installed", "text": f"Ready to play on {pc}.", "tone": "ok", "fix": "",
                "can_remove": row.get("launcher") in ("steam", "battlenet", "epic")}
    if not row.get("install"):
        return {"label": "Not on the PC", "text": f"Approved, so {kid_name} can install it himself from Get more games. "
                "Or press Install to put it on his PC now.", "tone": "off", "fix": "install"}

    # From here on the hub wants it installed.
    launcher_state = (launcher_app or {}).get("state")
    if state in ("queued", "waiting") and launcher_state == "installing":
        return {"label": f"Setting up {store}", "text": f"{store} is being put on {pc} first. The game follows by itself.",
                "tone": "busy", "fix": ""}
    if state in ("queued", "waiting") and launcher_state == "failed":
        return {"label": "Needs a hand", "text": f"{store} wouldn't install by itself on {pc}, so the game can't either. "
                f"{(launcher_app or {}).get('detail', '')}".strip(), "tone": "bad", "fix": "retry"}
    if state != "installing" and not online:
        return {"label": "Waiting for the PC", "text": f"Starts by itself the next time {pc} is on.", "tone": "busy", "fix": ""}
    if row.get("buying") and state not in ("installing",):
        b = row["buying"]
        return {"label": "Being bought", "text": f"{b.get('started_by') or 'A parent'} pressed Buy. It installs on {pc} by itself as soon as "
                f"it's on {kid_name}'s {store} account. Already paid? Press I bought it.", "tone": "busy", "fix": "bought",
                "purchase_id": b.get("id")}
    if (owned_words := ownership(row, state, kid_name, pc)) is not None:
        return owned_words
    if state == "queued":
        return {"label": "On its way", "text": f"Sent to {pc}. It starts within a minute.", "tone": "busy", "fix": ""}
    if state == "waiting":
        return {"label": stage or "Waiting", "text": detail or f"Starts by itself soon on {pc}.", "tone": "busy", "fix": ""}
    if state == "installing":
        doing = stage or "Downloading"
        if total:
            pct = max(0, min(99, int(done * 100 / total)))
            label = f"{doing} {pct}%" if doing in ("Downloading", "Paused", "Checking files") else doing
            how_fast = ""
            if doing == "Downloading" and rate > 0:
                how_fast = f" at {rate_text(rate)}, {left_text((total - done) / rate)}"
            elif doing == "Downloading" and done:
                how_fast = ". It hasn't moved since the last check; the PC may be busy or the internet slow"
            text = f"{size(done)} of {size(total)}{how_fast}." + (f" {detail}" if detail else "") + " You can leave it; it carries on by itself."
            return {"label": label, "text": text, "tone": "needs" if doing == "Paused" else "busy",
                    "fix": "retry" if doing == "Paused" else "", "percent": pct}
        so_far = f" ({size(done)} so far)" if done else ""
        return {"label": doing, "text": detail or f"{store} is installing it{so_far}. You can leave it; it carries on by itself.",
                "tone": "busy", "fix": ""}
    if state == "needs_signin":
        return {"label": f"Sign in to {store}", "text": detail or f"{store} has to be signed in on {pc} once, with {kid_name}'s account. "
                "After that, games install by themselves.", "tone": "needs", "fix": "signin"}
    if state == "not_owned":
        who = f"{kid_name}'s Steam account ({row['steam_name']})" if row.get("steam_name") else f"{kid_name}'s account"
        return {"label": "Not on his account", "text": detail or f"{store} says {who} doesn't have this game. "
                f"Press Buy to get it for him, or add it (free games too) on that account, then press Try again.", "tone": "needs", "fix": "buy"}
    if state == "no_space":
        return {"label": "PC is full", "text": detail or f"There isn't enough room on {pc}. Remove a game he no longer plays, then press Try again.",
                "tone": "bad", "fix": "retry"}
    if state == "stuck":
        return {"label": "Needs a nudge", "text": detail or f"{store} didn't start the download. Try again restarts {store} on {pc}.",
                "tone": "needs", "fix": "retry"}
    if state == "manual":
        return {"label": "Install on the PC", "text": detail or "This kind of game can't be installed from here yet.", "tone": "needs", "fix": ""}
    if state == "not_installed":
        return {"label": "Not on the PC", "text": f"Not installed on {pc} yet.", "tone": "off", "fix": "install"}
    return {"label": "Didn't install", "text": detail or f"Something went wrong on {pc}. Press Try again.", "tone": "bad", "fix": "retry"}


def ownership(row: dict, state: str, kid_name: str, pc: str) -> dict | None:
    """When the hub knows (from Steam) that his account doesn't own a game, say so before anyone waits on it.

    A game shared through Steam Family isn't in his own list but still installs, so the PC keeps trying and these
    words step aside as soon as it starts downloading."""
    if row.get("launcher") != "steam" or row.get("owned") is not False or state not in ("queued", "waiting", "stuck", "not_owned", "failed"):
        return None
    who = f"{kid_name}'s Steam account" + (f" ({row['steam_name']})" if row.get("steam_name") else "")
    title = row.get("title") or "This game"
    if row.get("free"):
        return {"label": "Add it on Steam", "text": f"{title} is free, but it isn't on {who} yet, and Steam won't download a game "
                f"the account hasn't got. {pc} opens Steam's Install box by itself: press Install there once (anyone at the PC can). "
                f"Or add it from the Steam store while signed in as him, then press Try again.", "tone": "needs", "fix": "retry"}
    return {"label": "Not on his account", "text": f"{who} doesn't own {title}, so Steam won't download it. Press Buy to get it "
            "for him (a gift, or Steam Family sharing), and it installs by itself once it's there. (A Family-shared game still "
            "installs; this goes away when it starts.)", "tone": "needs", "fix": "buy"}


def removing(row: dict, state: str, detail: str, stage: str, pc: str, online: bool) -> dict:
    """A game a parent asked to take off the PC."""
    if state == "failed":
        return {"label": "Couldn't remove it", "text": detail or f"It wouldn't come off {pc}. Press Remove to try again.", "tone": "bad", "fix": "remove"}
    if state == "manual":
        return {"label": "Remove it on the PC", "text": detail or "This kind of game can't be removed from here yet.", "tone": "needs", "fix": ""}
    if not online:
        return {"label": "Removing", "text": f"Comes off {pc} the next time it's on. It stays approved, so Install puts it back.", "tone": "busy", "fix": ""}
    return {"label": stage or "Removing", "text": detail or f"Coming off {pc} now. It stays approved, so Install puts it back.", "tone": "busy", "fix": ""}


def steam_line(acct: dict | None, info: dict, has_key: bool, kid_name: str) -> dict:
    """One line about Steam on this kid's PC: which account, and whether the hub can see its games."""
    if not acct:
        return {"text": f"Steam hasn't been signed in on {kid_name}'s PC yet, so the hub doesn't know his account.", "tone": "off"}
    who = acct.get("name") or acct.get("login") or acct["id"]
    also = f" (sign-in {acct['login']})" if acct.get("login") and acct.get("name") and acct["login"] != acct["name"] else ""
    base = f"Steam account: {who}{also}" + (", set by you" if acct.get("source") == "parent" else ", signed in on the PC")
    if not has_key:
        return {"text": f"{base}. Add a Steam Web API key under Connections and the hub can check which games it owns.", "tone": "off", "id": acct["id"]}
    if info.get("private"):
        return {"text": f"{base}. Steam keeps its games private, so the hub can't check what it owns. Signed in as that account, "
                "set Edit Profile, Privacy Settings, Game details to Public.", "tone": "needs", "id": acct["id"]}
    if info.get("error") and "appids" not in info:
        return {"text": f"{base}. {info['error']}", "tone": "needs", "id": acct["id"]}
    if "appids" in info:
        return {"text": f"{base}, {info.get('count', len(info['appids']))} games on it.", "tone": "ok", "id": acct["id"]}
    return {"text": f"{base}. Reading its games from Steam…", "tone": "off", "id": acct["id"]}


def summary(pcs: list[dict]) -> str:
    """One sentence across every PC, for the top of the page."""
    busy = sum(1 for p in pcs for g in p["games"] if g["plain"]["tone"] == "busy")
    needs = sum(1 for p in pcs for g in p["games"] if g["plain"]["tone"] in ("needs", "bad"))
    wanted = sum(1 for p in pcs for g in p["games"] if g["install"] or g["state"] == "installed")
    if needs:
        return f"{needs} game{'s need' if needs > 1 else ' needs'} you. Each one says what to do."
    if busy:
        return f"{busy} game{'s are' if busy > 1 else ' is'} on the way. Nothing to do; they finish by themselves."
    if wanted:
        return "Everything you approved is installed."
    return "Approve a game and it installs on that PC by itself."


def online(seen_at: str | None, now: datetime) -> bool:
    if not seen_at:
        return False
    return now - datetime.fromisoformat(seen_at) < timedelta(minutes=3)
