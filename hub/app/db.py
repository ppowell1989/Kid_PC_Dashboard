"""SQLite storage. One file, no ORM, so it is easy to inspect and back up on TrueNAS."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    launcher    TEXT NOT NULL CHECK (launcher IN ('steam','battlenet','gamepass','direct')),
    launch_ref  TEXT NOT NULL,          -- steam appid, battle.net product code, gamepass AUMID, or exe path
    rating      TEXT DEFAULT '',        -- ESRB, informational
    image_url   TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS approvals (     -- which kid may see which catalogue game
    kid_id   TEXT NOT NULL,
    game_id  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    PRIMARY KEY (kid_id, game_id)
);
CREATE TABLE IF NOT EXISTS usage (         -- minutes played per kid per local day
    kid_id  TEXT NOT NULL,
    day     TEXT NOT NULL,
    minutes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kid_id, day)
);
CREATE TABLE IF NOT EXISTS grants (        -- extra minutes a parent approved for a day
    id      INTEGER PRIMARY KEY,
    kid_id  TEXT NOT NULL,
    day     TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    by      TEXT NOT NULL,
    until   TEXT DEFAULT '',            -- ISO local time; set when granted after bedtime
    at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS status (        -- last heartbeat per kid
    kid_id       TEXT PRIMARY KEY,
    seen_at      TEXT NOT NULL,
    running      TEXT DEFAULT '',
    counted_at   REAL NOT NULL DEFAULT 0,   -- unix time of the last heartbeat that counted a minute
    locked       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY,
    kid_id      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('game','time','site')),
    detail      TEXT NOT NULL,          -- game title / minutes / domain
    note        TEXT DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','approved','declined')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_by  TEXT DEFAULT '',
    decided_at  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS visits (
    id      INTEGER PRIMARY KEY,
    kid_id  TEXT NOT NULL,
    source  TEXT NOT NULL CHECK (source IN ('chrome','dns')),
    domain  TEXT NOT NULL,
    url     TEXT DEFAULT '',            -- page URL (chrome only)
    title   TEXT DEFAULT '',
    blocked INTEGER NOT NULL DEFAULT 0,
    at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS visits_kid_at ON visits(kid_id, at);
CREATE TABLE IF NOT EXISTS sites (         -- parent-managed allow-list additions
    domain TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    kid_id      TEXT NOT NULL,
    friend_name TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    revoked     INTEGER NOT NULL DEFAULT 0,
    verified_by TEXT DEFAULT ''          -- discord user id when friend_verification = discord_oauth
);
CREATE TABLE IF NOT EXISTS device_apps (    -- what the helper reports about managed apps on each PC
    kid_id TEXT NOT NULL,
    app    TEXT NOT NULL,
    state  TEXT NOT NULL,                -- installed / installing / failed / manual
    detail TEXT DEFAULT '',
    at     TEXT NOT NULL,
    PRIMARY KEY (kid_id, app)
);
CREATE TABLE IF NOT EXISTS commands (       -- one-off actions a parent sends to a PC
    id         INTEGER PRIMARY KEY,
    kid_id     TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('message','restart','shutdown','install_app')),
    arg        TEXT DEFAULT '',
    state      TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','failed')),
    result     TEXT DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    done_at    TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (         -- things the PC reports: game stopped, unapproved game, app installed
    id     INTEGER PRIMARY KEY,
    kid_id TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT DEFAULT '',
    at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kid_at ON events(kid_id, at);
CREATE TABLE IF NOT EXISTS pauses (         -- "pause now": locked until a parent resumes or the time passes
    kid_id TEXT PRIMARY KEY,
    until  TEXT NOT NULL,
    by     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat (           -- one family chat: parents and kids
    id     INTEGER PRIMARY KEY,
    sender TEXT NOT NULL,                     -- display name
    kid_id TEXT,                              -- set when a kid sent it
    text   TEXT NOT NULL,
    at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return cur.lastrowid
