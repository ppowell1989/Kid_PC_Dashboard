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
CREATE TABLE IF NOT EXISTS searches (        -- search terms read from Chrome's history on the kid PC
    id      INTEGER PRIMARY KEY,
    kid_id  TEXT NOT NULL,
    term    TEXT NOT NULL,
    at      TEXT NOT NULL,
    flagged INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kid_id, term, at)
);
CREATE TABLE IF NOT EXISTS chat (           -- every chat message; conv says which conversation (see chat.py)
    id     INTEGER PRIMARY KEY,
    sender TEXT NOT NULL,                     -- display name
    kid_id TEXT,                              -- set when a kid sent it
    text   TEXT NOT NULL,
    at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playtimes (      -- per kid, per day of week; no row = config.yaml defaults
    kid_id        TEXT NOT NULL,
    weekday       INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),   -- 0 = Monday
    daily_minutes INTEGER NOT NULL,
    bedtime       TEXT NOT NULL DEFAULT '',  -- "HH:MM", "" = none
    windows       TEXT NOT NULL DEFAULT '[]', -- JSON [["15:30","17:30"], ...]; [] = any time before bedtime
    updated_by    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (kid_id, weekday)
);
CREATE TABLE IF NOT EXISTS game_installs (  -- where each approved game stands on each kid's PC, as the helper last said
    kid_id      TEXT NOT NULL,
    game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    state       TEXT NOT NULL DEFAULT 'queued',   -- see installs.STATES
    detail      TEXT DEFAULT '',                  -- the helper's own words, for the parents
    done_bytes  INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    retry       INTEGER NOT NULL DEFAULT 0,       -- bumped by "Try again"; the helper starts over when it changes
    at          TEXT NOT NULL DEFAULT '',         -- last report from the PC
    PRIMARY KEY (kid_id, game_id)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS setup_codes (    -- one command a parent pastes on a new PC to make it a kid's PC
    code       TEXT PRIMARY KEY,
    kid_id     TEXT NOT NULL,
    created_by TEXT NOT NULL,
    expires_at REAL NOT NULL,             -- unix time
    used_at    TEXT DEFAULT '',           -- last time a PC used it (it keeps working until it expires, so a failed run can be repeated)
    used_by    TEXT DEFAULT ''            -- the PC's computer name
);
CREATE TABLE IF NOT EXISTS console_links (   -- a parent's linked console account (today: Nintendo); token never leaves the hub
    provider  TEXT PRIMARY KEY,
    token     TEXT NOT NULL,
    label     TEXT DEFAULT '',
    linked_by TEXT DEFAULT '',
    linked_at TEXT DEFAULT '',
    synced_at TEXT DEFAULT '',
    error     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS console_devices (  -- each console on a linked account
    provider     TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    name         TEXT DEFAULT '',
    model        TEXT DEFAULT '',
    limit_kid    TEXT DEFAULT '',             -- the console's own daily limit follows this kid's time left ('' = leave it alone)
    stop_game    INTEGER NOT NULL DEFAULT 1,  -- also turn on "Suspend software" so the game stops at the limit
    limit_now    INTEGER,                     -- the console's limit today as last read or set
    limit_set_at TEXT DEFAULT '',
    extra_time   INTEGER NOT NULL DEFAULT 0,
    stops_game   INTEGER NOT NULL DEFAULT 0,
    seen_at      TEXT DEFAULT '',
    PRIMARY KEY (provider, device_id)
);
CREATE TABLE IF NOT EXISTS console_players (  -- user profiles on a console, matched by a parent to a kid
    provider  TEXT NOT NULL,
    device_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    nickname  TEXT DEFAULT '',
    kid_id    TEXT DEFAULT '',                 -- '' = not counted toward anyone
    PRIMARY KEY (provider, device_id, player_id)
);
CREATE TABLE IF NOT EXISTS console_usage (    -- a player's minutes on a console for a local day (Nintendo's running total)
    provider  TEXT NOT NULL,
    device_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    day       TEXT NOT NULL,
    minutes   INTEGER NOT NULL DEFAULT 0,
    games     TEXT DEFAULT '[]',
    PRIMARY KEY (provider, device_id, player_id, day)
);
"""


MIGRATIONS = [
    ("requests", "ref", "TEXT DEFAULT ''"),
    ("requests", "reason", "TEXT DEFAULT ''"),
    ("status", "game_id", "INTEGER"),            # catalogue game running now, so a brother can join it
    ("status", "voice", "TEXT DEFAULT ''"),      # voice room the kid is in: '', 'family' or 'friends'   # parent's note when deciding, shown to the kid   # e.g. "steam:291550" when the kid picked an exact game
    ("invites", "revoked_at", "TEXT DEFAULT ''"),   # when a parent revoked it; old revoked links are pruned
    # 1 = the hub puts this game on the kid's PC by itself. Approvals made before automatic installs existed
    # stay 0 (the kid installs them from Get more games), so an upgrade never starts a pile of big downloads.
    ("approvals", "auto_install", "INTEGER NOT NULL DEFAULT 0"),
    # Clearer install status: what the store is doing in a few words, when the state, stage or progress last moved
    # (not just when the PC last reported), and the download speed worked out from progress between reports.
    ("game_installs", "stage", "TEXT DEFAULT ''"),
    ("game_installs", "changed_at", "TEXT DEFAULT ''"),
    ("game_installs", "rate", "REAL NOT NULL DEFAULT 0"),   # bytes per second, smoothed
    # Chat beyond the one family room: the kids' own chat and one chat per invited friend, with pictures and links.
    ("chat", "conv", "TEXT NOT NULL DEFAULT 'family'"),   # family / kids / friend-<hash of invite code>
    ("chat", "friend", "INTEGER NOT NULL DEFAULT 0"),     # 1 = an invited friend sent it from the invite page
    ("chat", "picture", "TEXT DEFAULT ''"),               # media key of the picture (a clip's still frame for a video); see media.py
    ("chat", "video", "TEXT DEFAULT ''"),                 # media key of a video clip
    ("chat", "links", "TEXT DEFAULT '[]'"),               # JSON list of the links in the text
    ("chat", "flagged", "INTEGER NOT NULL DEFAULT 0"),    # a word from the flag list (same list as searches)
    ("chat", "removed_by", "TEXT DEFAULT ''"),            # parent who hid it from the kids
    # Xbox (Microsoft Family Safety) members are console players too; these only matter for provider 'xbox'.
    ("console_players", "role", "TEXT DEFAULT ''"),              # Family Safety role: User (child) or Admin (organizer)
    ("console_players", "enforce", "INTEGER NOT NULL DEFAULT 0"),  # 1 = block his Xbox when he is locked on the hub
    ("console_players", "blocked", "INTEGER NOT NULL DEFAULT 0"),  # Xbox blocked right now, as last read or set
    ("console_players", "hub_block", "TEXT DEFAULT ''"),          # until when the hub blocked it ('' = hub didn't)
]


class DB:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:   # columns added after a table first shipped
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self._conn.execute("CREATE INDEX IF NOT EXISTS chat_conv_id ON chat(conv, id)")
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
