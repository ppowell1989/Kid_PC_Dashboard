"""How the hub tells a parent where a PC build is: published, still being built, or waiting on the PC."""
import asyncio

import httpx

from app import updates
from app.config import Settings

from .conftest import ADAM


def test_dashboard_is_told_when_a_newer_build_is_still_being_made(tmp_path):
    updates.seen.clear()
    world = {"release": "sha1", "head": "sha1"}
    commits = {
        "sha1": {"sha": "sha1", "commit": {"message": "Faster polling", "committer": {"date": "2026-11-24T03:40:00Z"}}},
        "sha2": {"sha": "sha2", "commit": {"message": "Merge pull request #10 from o/branch\n\nShell: Power button on the home screen",
                                          "committer": {"date": "2026-11-24T04:02:00Z"}}},
    }

    def handler(req: httpx.Request):
        path = req.url.path
        if path.endswith("/releases/tags/test-build"):
            return httpx.Response(200, json={"target_commitish": world["release"], "published_at": "2026-11-24T03:44:00Z", "assets": []})
        if path.endswith("/commits/main"):
            return httpx.Response(200, json=commits[world["head"]])
        if "/commits/" in path:
            return httpx.Response(200, json=commits[path.rsplit("/", 1)[1]])
        return httpx.Response(404)

    s = Settings.model_validate({"updates": {"enabled": True, "github_repo": "o/r", "github_token": "tkn"}})

    async def look():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await updates.fetch_once(s, tmp_path, c)
        return updates.pipeline()

    try:
        now = asyncio.run(look())
        assert now["published"] == "sha1" and now["published_title"] == "Faster polling" and "building" not in now

        world["head"] = "sha2"                      # a pull request was merged; its build is not out yet
        now = asyncio.run(look())
        assert now["published"] == "sha1"
        assert now["building"]["version"] == "sha2" and now["building"]["title"] == "Shell: Power button on the home screen"
        assert now["building"]["waiting_minutes"] == 0
        late = updates.pipeline(now=updates.seen["head_first_seen"] + 20 * 60)
        assert late["building"]["waiting_minutes"] == 20   # the dashboard says the build may have failed

        world["release"] = "sha2"                   # the build finished
        now = asyncio.run(look())
        assert now["published"] == "sha2" and now["published_title"] == "Shell: Power button on the home screen" and "building" not in now
    finally:
        updates.seen.clear()


def test_builds_still_arrive_when_the_branch_cannot_be_read(tmp_path):
    updates.seen.clear()

    def handler(req: httpx.Request):
        if req.url.path.endswith("/releases/tags/test-build"):
            return httpx.Response(200, json={"target_commitish": "sha1", "assets": []})
        return httpx.Response(403)                  # a token without access to commits

    s = Settings.model_validate({"updates": {"enabled": True, "github_repo": "o/r", "github_token": "tkn"}})

    async def look():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await updates.fetch_once(s, tmp_path, c)

    try:
        assert asyncio.run(look()) == []
        assert updates.pipeline() == {"published": "sha1", "published_at": "", "published_title": ""}
    finally:
        updates.seen.clear()


def test_overview_carries_the_build_pipeline(parent):
    updates.seen.clear()
    assert parent.get("/api/parent/overview").json()["builds"] == {}
    updates.seen.update(published="sha1", head="sha2", head_title="Power button", head_first_seen=0)
    try:
        b = parent.get("/api/parent/overview").json()["builds"]
        assert b["published"] == "sha1" and b["building"]["title"] == "Power button"
    finally:
        updates.seen.clear()


def test_install_now_is_a_command_the_pc_receives(parent):
    cid = parent.post("/api/parent/kids/adam/commands", json={"kind": "install_app", "arg": "kidspc"}).json()["id"]
    pending = parent.get("/api/device/rules", headers=ADAM).json()["commands"]
    assert [(c["id"], c["kind"], c["arg"], c["created_by"]) for c in pending] == [(cid, "install_app", "kidspc", "Patrick")]
    assert parent.post("/api/parent/kids/adam/commands", json={"kind": "install_app", "arg": "notepad"}).status_code == 422


def test_only_a_signed_in_parent_changes_how_updates_go_in(client):
    assert client.put("/api/parent/kids/adam/auto-update", json={"on": True}).status_code == 401
    assert client.post("/api/parent/kids/adam/commands", json={"kind": "install_app", "arg": "kidspc"}).status_code == 401


def test_updates_wait_for_a_restart_unless_a_parent_says_right_away(parent):
    assert parent.get("/api/device/rules", headers=ADAM).json()["auto_update"] is False
    assert parent.put("/api/parent/kids/nobody/auto-update", json={"on": True}).status_code == 404
    assert parent.put("/api/parent/kids/adam/auto-update", json={"on": True}).json() == {"auto_update": True}
    assert parent.get("/api/device/rules", headers=ADAM).json()["auto_update"] is True
    assert parent.get("/api/device/rules", headers={"Authorization": "Bearer tok-barrett"}).json()["auto_update"] is False
    kids = {k["id"]: k for k in parent.get("/api/parent/overview").json()["kids"]}
    assert kids["adam"]["auto_update"] is True and kids["barrett"]["auto_update"] is False
    parent.put("/api/parent/kids/adam/auto-update", json={"on": False})
    assert parent.get("/api/device/rules", headers=ADAM).json()["auto_update"] is False
