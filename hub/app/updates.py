"""Keeps the latest PC build on the hub so the helpers can update themselves.

The hub polls the repo's "test-build" GitHub release (private, so it needs a token with
Contents: read) and stores each zip under <data>/updates/. The helpers ask the hub, never GitHub,
so the PCs need no GitHub credentials.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import httpx

from .config import Settings

log = logging.getLogger("hub.updates")
ARCHES = {"x64": "kidspc-win-x64.zip", "arm64": "kidspc-win-arm64.zip"}


def update_dir(db_path: str) -> Path:
    d = Path(db_path).resolve().parent / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def current(dirpath: Path, arch: str) -> dict | None:
    meta = dirpath / f"{arch}.json"
    if not meta.exists() or not (dirpath / ARCHES[arch]).exists():
        return None
    return json.loads(meta.read_text())


async def fetch_once(settings: Settings, dirpath: Path, client: httpx.AsyncClient) -> list[str]:
    u = settings.updates
    headers = {"Authorization": f"Bearer {u.github_token}", "Accept": "application/vnd.github+json"}
    r = await client.get(f"https://api.github.com/repos/{u.github_repo}/releases/tags/{u.release_tag}", headers=headers)
    r.raise_for_status()
    rel = r.json()
    version = rel.get("target_commitish", "")
    fetched = []
    for arch, name in ARCHES.items():
        have = current(dirpath, arch)
        if have and have["version"] == version:
            continue
        asset = next((a for a in rel.get("assets", []) if a["name"] == name), None)
        if not asset:
            continue
        tmp = dirpath / (name + ".part")
        h = hashlib.sha256()
        async with client.stream("GET", asset["url"], headers={**headers, "Accept": "application/octet-stream"},
                                 follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                async for chunk in resp.aiter_bytes(1 << 16):
                    f.write(chunk)
                    h.update(chunk)
        tmp.replace(dirpath / name)
        (dirpath / f"{arch}.json").write_text(json.dumps({"version": version, "sha256": h.hexdigest(),
                                                           "size": (dirpath / name).stat().st_size}))
        fetched.append(f"{arch}@{version[:8]}")
    return fetched


async def poll_forever(settings: Settings, dirpath: Path) -> None:
    async with httpx.AsyncClient(timeout=120) as client:
        while True:
            try:
                got = await fetch_once(settings, dirpath, client)
                if got:
                    log.info("downloaded PC builds: %s", ", ".join(got))
            except Exception as exc:
                log.warning("update check failed: %s", exc)
            await asyncio.sleep(settings.updates.poll_minutes * 60)
