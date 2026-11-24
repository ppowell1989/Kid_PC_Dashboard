"""The hub's side of the self-updater (deploy/hub-autoupdate.sh): /healthz and the PCs page's hub card."""
import json
import os

from app import updates


def test_healthz_reports_the_version(client, monkeypatch):
    monkeypatch.setenv("HUB_VERSION", "abc123")
    assert client.get("/healthz").json() == {"ok": True, "version": "abc123"}


def test_hub_without_the_updater_says_so(parent):
    h = parent.get("/api/parent/installs").json()["hub"]
    assert h["self_update"] is False and h["version"] == "dev"


def test_hub_card_shows_the_last_update_and_what_is_on_its_way(parent, monkeypatch):
    monkeypatch.setenv("HUB_VERSION", "v1")
    state = os.path.join(os.path.dirname(os.environ["HUB_DB"]), "hub-update.json")
    with open(state, "w") as f:
        json.dump({"result": "updated", "version": "v1", "title": "Good change", "updated_at": "2026-11-24T10:00:00-05:00"}, f)
    monkeypatch.setitem(updates.seen, "head", "v2")
    monkeypatch.setitem(updates.seen, "head_title", "Next change")
    h = parent.get("/api/parent/installs").json()["hub"]
    assert h["self_update"] and h["result"] == "updated" and h["title"] == "Good change"
    assert h["behind"] == {"version": "v2", "title": "Next change"}


def test_a_failed_version_is_not_shown_as_on_its_way(parent, monkeypatch):
    monkeypatch.setenv("HUB_VERSION", "v1")
    state = os.path.join(os.path.dirname(os.environ["HUB_DB"]), "hub-update.json")
    with open(state, "w") as f:
        json.dump({"result": "rolled_back", "version": "v1", "failed_version": "v2", "failed_title": "Broken",
                   "message": "The new version didn't start properly"}, f)
    monkeypatch.setitem(updates.seen, "head", "v2")
    h = parent.get("/api/parent/installs").json()["hub"]
    assert h["result"] == "rolled_back" and h["failed_title"] == "Broken" and "behind" not in h
