"""A game needs the program it installs through. Approving the game is what gets that program onto the PC."""
from .conftest import ADAM

BARRETT = {"Authorization": "Bearer tok-barrett"}


def test_approving_a_battlenet_game_gets_battlenet_installed(parent):
    assert parent.get("/api/device/rules", headers=ADAM).json()["managed_apps"] == ["steam", "chrome"]
    parent.post("/api/parent/games", json={"title": "World of Warcraft", "launcher": "battlenet", "launch_ref": "WoW", "approved_for": ["adam"]})
    assert parent.get("/api/device/rules", headers=ADAM).json()["managed_apps"] == ["steam", "chrome", "battlenet"]
    # Barrett is not approved for it, so his PC is left alone.
    assert parent.get("/api/device/rules", headers=BARRETT).json()["managed_apps"] == ["steam", "chrome"]


def test_a_parent_can_retry_the_battlenet_install(parent):
    cid = parent.post("/api/parent/kids/adam/commands", json={"kind": "install_app", "arg": "battlenet"}).json()["id"]
    assert [c["arg"] for c in parent.get("/api/device/rules", headers=ADAM).json()["commands"] if c["id"] == cid] == ["battlenet"]


def test_parents_see_a_missing_launcher_in_activity(parent):
    r = parent.post("/api/device/events", headers=ADAM, json=[
        {"kind": "launcher_missing", "detail": "Battle.net is not on this PC; Adam wanted World of Warcraft. Installing it now.", "at": "2026-11-24T04:30:00Z"},
        {"kind": "launch_failed", "detail": "Chrome could not start: The system cannot find the file specified.", "at": "2026-11-24T04:31:00Z"}])
    assert r.status_code == 204
    kinds = [e["kind"] for e in parent.get("/api/parent/kids/adam/activity").json()["events"]]
    assert kinds[:2] == ["launch_failed", "launcher_missing"]
