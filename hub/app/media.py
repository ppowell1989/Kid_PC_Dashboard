"""Where chat photos and video clips live, and how a screen is linked to one.

The rule (Patrick, 2026-11-24): media stays on the family's own box. Everything else only holds a link to it.

  * A chat message stores a media key ("3f9a…c1.jpg"), never a path or a URL. The key is random, so it can't be
    guessed, and it means the same thing wherever the file sits.
  * The access-checked endpoints in chat.py decide who may see a file, then answer with a short-lived signed link
    (/media/<key>?exp=…&sig=…). Only that link touches the file.
  * Today the hub and the store are the same TrueNAS app, so the link points back at this hub.
    With a hosted hub later, the family's box keeps this LocalStore and serves /media itself; the hosted hub keeps
    only message rows and keys, and signs links to the box (MEDIA_BASE_URL) with a secret the two share. The photos
    and clips never have to leave the house unless someone outside opens them.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path

KEY_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|mp4)$")
LINK_SECONDS = 3600
TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".mp4": "video/mp4"}


class LocalStore:
    """Media files in one folder on the family's box (next to the hub's database today)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def new_key(self, ext: str) -> str:
        return secrets.token_hex(16) + "." + ext.lstrip(".")

    def path(self, key: str) -> Path:
        if not KEY_RE.match(key or ""):
            raise ValueError("not a media key")
        return self.root / key

    def scratch(self, suffix: str = "") -> Path:
        """A working file for uploads being checked or converted; never served."""
        d = self.root / "incoming"
        d.mkdir(parents=True, exist_ok=True)
        return d / (secrets.token_hex(8) + suffix)

    def ready(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def keys(self) -> list[str]:
        return [f.name for f in self.root.iterdir() if f.is_file() and KEY_RE.match(f.name)] if self.root.exists() else []

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


def base_url() -> str:
    """Where /media is served. Empty = this hub. A hosted hub would set it to the family's box."""
    return os.environ.get("MEDIA_BASE_URL", "").rstrip("/")


def _sig(secret: str, key: str, exp: int) -> str:
    return hmac.new(secret.encode(), f"{key}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def signed_link(secret: str, key: str, seconds: int = LINK_SECONDS) -> str:
    # Rounded up to the next hour so a browser can cache the same link instead of fetching the file again.
    exp = (int(time.time()) // seconds + 2) * seconds
    return f"{base_url()}/media/{key}?exp={exp}&sig={_sig(secret, key, exp)}"


def link_ok(secret: str, key: str, exp: int, sig: str) -> bool:
    return exp > time.time() and hmac.compare_digest(_sig(secret, key, exp), sig or "")
