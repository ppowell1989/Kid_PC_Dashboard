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
    "needs_signin",   # the store app (Steam, Battle.net) has to be signed in on that PC once
    "not_owned",      # the store says the kid's account doesn't have this game
    "no_space",       # the PC's drive is too full
    "stuck",          # the store didn't start it; Try again usually does
    "failed",
    "manual",         # this kind of game can't be installed from afar (yet)
    "not_installed",  # approved, but not on the install list and not on the PC
)
REPORTABLE = tuple(s for s in STATES if s != "queued")

STORE = {"steam": "Steam", "battlenet": "Battle.net", "gamepass": "the Xbox app", "direct": "the home server"}


def size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.0f} MB"
    return f"{max(n, 0) // 1024} KB"


def plain(row: dict, kid_name: str, online: bool, launcher_app: dict | None) -> dict:
    """One game on one PC, in parent words.

    Returns label (a few words for the pill), text (one sentence on what is happening and what, if anything,
    the parent can do), tone (ok, busy, needs, bad, off) and fix (retry, install, signin or "")."""
    state, detail = row.get("state") or "queued", (row.get("detail") or "").strip()
    store = STORE.get(row.get("launcher", ""), "its store")
    pc = f"{kid_name}'s PC"
    done, total = row.get("done_bytes") or 0, row.get("total_bytes") or 0

    if state == "installed":
        return {"label": "Installed", "text": f"Ready to play on {pc}.", "tone": "ok", "fix": ""}
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
    if state == "queued":
        return {"label": "On its way", "text": f"Sent to {pc}. It starts within a minute.", "tone": "busy", "fix": ""}
    if state == "waiting":
        return {"label": "Waiting", "text": detail or f"Starts by itself soon on {pc}.", "tone": "busy", "fix": ""}
    if state == "installing":
        if total:
            pct = max(0, min(99, int(done * 100 / total)))
            return {"label": f"Downloading {pct}%", "text": f"{size(done)} of {size(total)} in {store}. You can leave it; it carries on by itself.",
                    "tone": "busy", "fix": "", "percent": pct}
        so_far = f" ({size(done)} so far)" if done else ""
        return {"label": "Installing", "text": f"{store} is installing it{so_far}. You can leave it; it carries on by itself.",
                "tone": "busy", "fix": ""}
    if state == "needs_signin":
        return {"label": f"Sign in to {store}", "text": detail or f"{store} has to be signed in on {pc} once, with {kid_name}'s account. "
                "After that, games install by themselves.", "tone": "needs", "fix": "signin"}
    if state == "not_owned":
        return {"label": "Not in his library", "text": detail or f"{store} says {kid_name}'s account doesn't have this game. "
                f"Buy it or add it (free games too) on his account, then press Try again.", "tone": "needs", "fix": "retry"}
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
