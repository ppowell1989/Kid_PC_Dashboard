from datetime import datetime
from zoneinfo import ZoneInfo

import jwt

from app import adguard, rules
from app.config import Kid, load_settings
from app.security import origin_allowed

from .conftest import ADAM


def test_device_needs_token(client):
    assert client.get("/api/device/rules").status_code == 401
    assert client.get("/api/device/rules", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_parent_needs_login(client):
    assert client.get("/api/parent/overview").status_code == 401
    assert client.post("/api/parent/login", json={"name": "Patrick", "password": "bad"}).status_code == 401


def test_catalogue_approval_and_game_pass_switch(parent):
    parent.post("/api/parent/games", json={"title": "Minecraft", "launcher": "gamepass", "launch_ref": "Microsoft.MinecraftUWP", "approved_for": ["adam"]})
    parent.post("/api/parent/games", json={"title": "Portal 2", "launcher": "steam", "launch_ref": "620", "approved_for": ["adam"]})
    parent.post("/api/parent/games", json={"title": "Overwatch", "launcher": "battlenet", "launch_ref": "Pro", "approved_for": ["barrett"]})
    games = parent.get("/api/device/rules", headers=ADAM).json()["games"]
    # Game Pass title hidden because game_pass_enabled is false; Barrett's game not shown to Adam
    assert [g["title"] for g in games] == ["Portal 2"]


def test_heartbeat_counts_minutes_once_per_interval(parent):
    r1 = parent.post("/api/device/heartbeat", headers=ADAM, json={"running": "Portal 2", "active": True}).json()
    r2 = parent.post("/api/device/heartbeat", headers=ADAM, json={"running": "Portal 2", "active": True}).json()
    assert r1["time"]["used"] == 1 and r2["time"]["used"] == 1   # second beat too soon to count
    idle = parent.post("/api/device/heartbeat", headers=ADAM, json={"running": "", "active": False}).json()
    assert idle["time"]["used"] == 1
    kids = parent.get("/api/parent/overview").json()["kids"]
    adam = next(k for k in kids if k["id"] == "adam")
    assert adam["online"] and adam["time"]["minutes_left"] == 59


def test_time_request_flow(parent):
    rid = parent.post("/api/device/requests", headers=ADAM, json={"kind": "time", "detail": "30"}).json()["id"]
    dup = parent.post("/api/device/requests", headers=ADAM, json={"kind": "time", "detail": "30"}).json()
    assert dup["duplicate"] and dup["id"] == rid
    assert parent.post(f"/api/parent/requests/{rid}", json={"approve": True}).json()["state"] == "approved"
    t = parent.get("/api/device/rules", headers=ADAM).json()["time"]
    assert t["granted"] == 30 and t["minutes_left"] == 90
    assert parent.post(f"/api/parent/requests/{rid}", json={"approve": True}).status_code == 409


def test_game_request_needs_catalogue_pick(parent):
    gid = parent.post("/api/parent/games", json={"title": "Rocket League", "launcher": "steam", "launch_ref": "252950"}).json()["id"]
    rid = parent.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "Rocket League"}).json()["id"]
    assert parent.post(f"/api/parent/requests/{rid}", json={"approve": True}).status_code == 422
    parent.post(f"/api/parent/requests/{rid}", json={"approve": True, "game_id": gid})
    assert [g["title"] for g in parent.get("/api/device/rules", headers=ADAM).json()["games"]] == ["Rocket League"]


def test_site_request_adds_to_allow_list(parent):
    rid = parent.post("/api/device/requests", headers=ADAM, json={"kind": "site", "detail": "Scratch.mit.edu"}).json()["id"]
    parent.post(f"/api/parent/requests/{rid}", json={"approve": True})
    assert "scratch.mit.edu" in parent.get("/api/device/rules", headers=ADAM).json()["web"]["allowed_sites"]


def test_chrome_visits_reported(parent):
    ext = {"Authorization": "Bearer ext-adam"}
    assert parent.get("/api/device/rules", headers=ext).status_code == 401   # extension token can't read rules
    r = parent.post("/api/device/visits", headers=ext, json=[{"url": "https://en.wikipedia.org/wiki/Cat", "title": "Cat", "at": "2026-11-23T20:00:00Z"}])
    assert r.status_code == 204
    v = parent.get("/api/parent/visits/adam?source=chrome").json()
    assert v[0]["domain"] == "en.wikipedia.org" and v[0]["title"] == "Cat"
    assert parent.get("/api/parent/visits/barrett").json() == []


def test_invite_join_and_revoke(parent):
    inv = parent.post("/api/parent/invites", json={"kid_id": "adam", "friend_name": "Sam"}).json()
    assert inv["link"].startswith("http://hub/join/")
    j = parent.post(f"/api/voice/join/{inv['code']}").json()
    claims = jwt.decode(j["token"], "s" * 32, ["HS256"])
    assert claims["video"]["room"] == "room-adam" and claims["name"] == "Sam"
    assert parent.get(f"/join/{inv['code']}").status_code == 200
    parent.delete(f"/api/parent/invites/{inv['code']}")   # LiveKit unreachable in tests: must not fail
    assert parent.post(f"/api/voice/join/{inv['code']}").status_code == 404


def test_remote_access_gate(client):
    s = load_settings(__import__("os").environ["HUB_CONFIG"])
    assert origin_allowed(s, "192.168.1.5", "/api/parent/overview")
    assert not origin_allowed(s, "8.8.8.8", "/api/parent/overview")
    assert origin_allowed(s, "8.8.8.8", "/join/abc")            # friends always reach join pages
    assert not origin_allowed(s, "100.100.1.1", "/")
    s.decisions.remote_access = "tailscale"
    assert origin_allowed(s, "100.100.1.1", "/")
    r = client.get("/api/parent/overview", headers={"x-real-ip": "8.8.8.8"})
    assert r.status_code == 403


KID = Kid(id="a", name="A", device_token="t", daily_minutes={"weekday": 60, "weekend": 120},
          bedtime={"weekday": "20:30", "weekend": "21:30"})
TZ = ZoneInfo("UTC")


def test_rules_bedtime_and_warning():
    tue_eve = datetime(2026, 11, 24, 20, 25, tzinfo=TZ)   # Tuesday, 5 min to bedtime
    st = rules.compute(KID, tue_eve, used=10, granted=0)
    assert st.minutes_left == 5 and st.warn and not st.locked
    late = datetime(2026, 11, 24, 20, 40, tzinfo=TZ)
    st = rules.compute(KID, late, used=10, granted=0)
    assert st.locked and st.lock_reason == "bedtime"
    st = rules.compute(KID, late, used=10, granted=20, late_until=datetime(2026, 11, 24, 21, 0, tzinfo=TZ))
    assert not st.locked and st.minutes_left == 20


def test_rules_out_of_time_and_weekend():
    sat = datetime(2026, 11, 28, 12, 0, tzinfo=TZ)
    assert rules.compute(KID, sat, used=60, granted=0).minutes_left == 60
    st = rules.compute(KID, sat, used=120, granted=0)
    assert st.locked and st.lock_reason == "time"


def test_adguard_import_maps_clients_and_skips_noise(client):
    from app.main import app
    s = load_settings(__import__("os").environ["HUB_CONFIG"])
    entries = [
        {"time": "2026-11-23T20:00:01Z", "client_info": {"name": "adam-pc"}, "question": {"name": "www.roblox.com."}, "reason": "FilteredBlackList"},
        {"time": "2026-11-23T20:00:02Z", "client_info": {"name": "adam-pc"}, "question": {"name": "cdn.steamcontent.com"}, "reason": "NotFilteredNotFound"},
        {"time": "2026-11-23T20:00:03Z", "client_info": {"name": "phone"}, "question": {"name": "news.com"}, "reason": ""},
    ]
    assert adguard.import_entries(app.state.db, s, entries) == 1
    assert adguard.import_entries(app.state.db, s, entries) == 0   # already imported
    row = app.state.db.one("SELECT * FROM visits WHERE source='dns'")
    assert row["domain"] == "www.roblox.com" and row["blocked"] == 1


def test_heartbeat_reports_managed_apps(parent):
    assert parent.get("/api/device/rules", headers=ADAM).json()["managed_apps"] == ["steam", "chrome"]
    parent.post("/api/device/heartbeat", headers=ADAM, json={"apps": {"steam": {"state": "installing"}, "chrome": {"state": "installed"}}})
    parent.post("/api/device/heartbeat", headers=ADAM, json={"apps": {"steam": {"state": "installed"}}})
    adam = next(k for k in parent.get("/api/parent/overview").json()["kids"] if k["id"] == "adam")
    assert {a["app"]: a["state"] for a in adam["apps"]} == {"chrome": "installed", "steam": "installed"}


def test_pause_and_resume(parent):
    st = parent.post("/api/parent/kids/adam/pause", json={"minutes": 30}).json()
    assert st["locked"] and st["lock_reason"] == "paused"
    assert parent.get("/api/device/rules", headers=ADAM).json()["time"]["locked"]
    st = parent.delete("/api/parent/kids/adam/pause").json()
    assert not st["locked"]


def test_commands_round_trip(parent):
    assert parent.post("/api/parent/kids/adam/commands", json={"kind": "message", "arg": " "}).status_code == 422
    cid = parent.post("/api/parent/kids/adam/commands", json={"kind": "message", "arg": "Dinner in 5!"}).json()["id"]
    pending = parent.get("/api/device/rules", headers=ADAM).json()["commands"]
    assert [(c["id"], c["kind"], c["arg"]) for c in pending] == [(cid, "message", "Dinner in 5!")]
    assert parent.get("/api/device/rules", headers={"Authorization": "Bearer tok-barrett"}).json()["commands"] == []
    parent.post(f"/api/device/commands/{cid}", headers=ADAM, json={"ok": True, "result": "shown"})
    assert parent.get("/api/device/rules", headers=ADAM).json()["commands"] == []
    act = parent.get("/api/parent/kids/adam/activity").json()
    assert act["commands"][0]["state"] == "done"


def test_events_feed(parent):
    r = parent.post("/api/device/events", headers=ADAM, json=[{"kind": "game_blocked", "detail": "Roblox", "at": "2026-11-24T01:00:00Z"}])
    assert r.status_code == 204
    assert parent.get("/api/parent/kids/adam/activity").json()["events"][0]["detail"] == "Roblox"
