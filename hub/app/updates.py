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
import time
from pathlib import Path

import httpx

from .config import Settings

log = logging.getLogger("hub.updates")
ARCHES = {"x64": "kidspc-win-x64.zip", "arm64": "kidspc-win-arm64.zip"}

# What the last look at GitHub found: the build the PCs can have now ("published") and the newest
# change on the branch ("head"). When they differ, a build is still being made, and the dashboard
# says so instead of calling a PC up to date.
seen: dict = {}


def title_of(commit: dict) -> str:
    """A commit's one-line name. For a merged pull request that is the pull request's title."""
    lines = [ln.strip() for ln in commit.get("commit", {}).get("message", "").splitlines() if ln.strip()]
    if not lines:
        return ""
    if lines[0].startswith("Merge pull request") and len(lines) > 1:
        return lines[1][:120]
    return lines[0][:120]


def pipeline(now: float | None = None) -> dict:
    """For the dashboard: is a newer build than the published one on its way?"""
    if not seen.get("published"):
        return {}
    out = {"published": seen["published"], "published_at": seen.get("published_at", ""), "published_title": seen.get("published_title", "")}
    head = seen.get("head", "")
    if head and head != seen["published"]:
        out["building"] = {"version": head, "title": seen.get("head_title", ""), "merged_at": seen.get("head_at", ""),
                           "waiting_minutes": int(((now or time.time()) - seen.get("head_first_seen", time.time())) // 60)}
    return out


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
    await look_at_branch(u, version, rel.get("published_at", ""), headers, client)
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


async def look_at_branch(u, version: str, published_at: str, headers: dict, client: httpx.AsyncClient) -> None:
    """Fills `seen`. Best effort: the builds still reach the PCs if this fails."""
    try:
        if seen.get("published") != version:
            seen.update(published=version, published_at=published_at, published_title="")
            c = await client.get(f"https://api.github.com/repos/{u.github_repo}/commits/{version}", headers=headers)
            if c.status_code == 200:
                seen["published_title"] = title_of(c.json())
        h = await client.get(f"https://api.github.com/repos/{u.github_repo}/commits/{u.branch}", headers=headers)
        h.raise_for_status()
        head = h.json()
        if seen.get("head") != head["sha"]:
            seen.update(head=head["sha"], head_title=title_of(head), head_first_seen=time.time(),
                        head_at=head.get("commit", {}).get("committer", {}).get("date", ""))
    except Exception as exc:
        seen.pop("head", None)
        log.warning("could not read the newest change from GitHub: %s", exc)


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
