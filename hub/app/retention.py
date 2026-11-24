"""How long the hub keeps history, and the clean-up that enforces it.

The windows live in the database (kv key "retention"), not config.yaml, so parents change them from the
dashboard and a hosted hub can keep one set per family later. 0 days means keep forever.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from .db import DB

log = logging.getLogger("hub.retention")

# kind -> (label for the dashboard, default days)
KINDS: dict[str, tuple[str, int]] = {
    "visits":   ("Web pages and sites", 30),
    "searches": ("Searches", 90),
    "activity": ("PC activity log", 30),
    "requests": ("Answered requests", 30),
    "invites":  ("Revoked voice links", 7),
    "chat":     ("Family chat", 90),
}
CHOICES = (0, 1, 7, 14, 30, 90, 180, 365)
PRUNE_EVERY_SECONDS = 3600


def get(db: DB) -> dict[str, int]:
    row = db.one("SELECT value FROM kv WHERE key='retention'")
    saved = json.loads(row["value"]) if row else {}
    return {k: int(saved.get(k, default)) for k, (_, default) in KINDS.items()}


def put(db: DB, days: dict[str, int]) -> dict[str, int]:
    cur = get(db)
    cur.update({k: int(v) for k, v in days.items() if k in KINDS})
    db.x("INSERT INTO kv(key,value) VALUES('retention',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(cur),))
    return cur


def _cutoff(now: datetime, days: int) -> str:
    """The UTC day before which rows go. Timestamps in the DB come in a few shapes ("2026-11-24 03:00:00" from
    SQLite, ISO with an offset from the PCs and AdGuard), so compare by day: every shape starts with YYYY-MM-DD."""
    return (now.astimezone(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


# kind -> [(sql deleting rows older than ?, optionally for one kid)]
_SQL = {
    "visits":   ["DELETE FROM visits WHERE at < ?{kid}"],
    "searches": ["DELETE FROM searches WHERE at < ?{kid}"],
    "activity": ["DELETE FROM events WHERE at < ?{kid}",
                 "DELETE FROM commands WHERE state != 'pending' AND created_at < ?{kid}"],
    "requests": ["DELETE FROM requests WHERE state != 'pending' AND COALESCE(NULLIF(decided_at,''), created_at) < ?{kid}"],
    "invites":  ["DELETE FROM invites WHERE revoked=1 AND COALESCE(NULLIF(revoked_at,''), created_at) < ?{kid}"],
    "chat":     ["DELETE FROM chat WHERE at < ?"],   # one family room, not per kid
}


def clear(db: DB, kind: str, before: str = "9999", kid_id: str | None = None) -> int:
    """Delete one kind of history older than `before` (default: all of it), for one kid or everyone."""
    n = 0
    with db.tx() as c:
        for sql in _SQL[kind]:
            if kid_id and "{kid}" in sql:
                cur = c.execute(sql.format(kid=" AND kid_id=?"), (before, kid_id))
            else:
                cur = c.execute(sql.format(kid=""), (before,))
            n += cur.rowcount
    return n


def prune(db: DB, now: datetime) -> dict[str, int]:
    out = {}
    for kind, days in get(db).items():
        if days > 0:
            n = clear(db, kind, _cutoff(now, days))
            if n:
                out[kind] = n
    if out:
        log.info("pruned old history: %s", out)
    return out


async def prune_forever(db: DB, now) -> None:
    while True:
        try:
            prune(db, now())
        except Exception as exc:  # never let clean-up take the hub down
            log.warning("prune failed: %s", exc)
        await asyncio.sleep(PRUNE_EVERY_SECONDS)
