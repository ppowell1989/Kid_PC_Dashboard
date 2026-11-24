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
    assert origin_allowed(s, "8.8.8.8", "/static/app.css")      # ...and the stylesheet that page uses
    assert not origin_allowed(s, "8.8.8.8", "/static/app.js")   # but not the dashboard's code
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


def test_update_endpoints(parent):
    import json as _json
    from app.main import app as _app
    d = _app.state.updates_dir
    assert parent.get("/api/device/update?arch=arm64", headers=ADAM).status_code == 404
    (d / "kidspc-win-arm64.zip").write_bytes(b"PK-fake")
    (d / "arm64.json").write_text(_json.dumps({"version": "abc123", "sha256": "x", "size": 7}))
    assert parent.get("/api/device/update?arch=arm64").status_code == 401        # parent cookie is not a device
    assert parent.get("/api/device/update?arch=arm64", headers=ADAM).json()["version"] == "abc123"
    assert parent.get("/api/device/update/file?arch=arm64", headers=ADAM).content == b"PK-fake"


def test_fetch_builds_from_github_release(tmp_path):
    import asyncio
    import httpx
    from app import updates
    from app.config import Settings

    calls = []

    def handler(req: httpx.Request):
        calls.append(str(req.url))
        if req.url.path.endswith("/releases/tags/test-build"):
            assert req.headers["authorization"] == "Bearer tkn"
            return httpx.Response(200, json={"target_commitish": "sha1", "assets": [
                {"name": "kidspc-win-arm64.zip", "url": "https://api.github.com/repos/o/r/releases/assets/1"}]})
        if req.url.path.endswith("/assets/1"):
            return httpx.Response(302, headers={"location": "https://objects.example/zip"})
        if req.url.host == "objects.example":
            return httpx.Response(200, content=b"ZIPDATA")
        return httpx.Response(404)

    s = Settings.model_validate({"updates": {"enabled": True, "github_repo": "o/r", "github_token": "tkn"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            first = await updates.fetch_once(s, tmp_path, c)
            second = await updates.fetch_once(s, tmp_path, c)
            return first, second

    first, second = asyncio.run(run())
    assert first == ["arm64@sha1"] and second == []
    assert (tmp_path / "kidspc-win-arm64.zip").read_bytes() == b"ZIPDATA"
    assert updates.current(tmp_path, "arm64")["version"] == "sha1"


def test_steam_game_from_store_link_gets_title_and_art(parent, monkeypatch):
    from app import main

    async def fake_lookup(appid):
        assert appid == "291550"
        return {"name": "Brawlhalla", "header_image": "https://example/291550/header.jpg"}
    monkeypatch.setattr(main, "steam_lookup", fake_lookup)
    r = parent.post("/api/parent/games", json={"launcher": "steam", "launch_ref": "https://store.steampowered.com/app/291550/Brawlhalla/", "approved_for": ["adam"]})
    assert r.status_code == 201
    g = next(g for g in parent.get("/api/parent/games").json() if g["launch_ref"] == "291550")
    assert g["title"] == "Brawlhalla" and g["image_url"].endswith("header.jpg")
    # Steam unreachable and no title: refuse rather than add a nameless game
    async def offline(appid): return {}
    monkeypatch.setattr(main, "steam_lookup", offline)
    assert parent.post("/api/parent/games", json={"launcher": "steam", "launch_ref": "440"}).status_code == 400
    # existing game without art gets the CDN header
    parent.post("/api/parent/games", json={"title": "TF2", "launcher": "steam", "launch_ref": "440"})
    tf2 = next(g for g in parent.get("/api/parent/games").json() if g["launch_ref"] == "440")
    assert "/apps/440/header.jpg" in tf2["image_url"]


def test_family_chat(parent):
    barrett = {"Authorization": "Bearer tok-barrett"}
    assert parent.post("/api/parent/chat", json={"text": "Dinner in 10"}).status_code == 201
    assert parent.post("/api/device/chat", headers=ADAM, json={"text": "ok!"}).status_code == 201
    assert parent.post("/api/device/chat", headers=ADAM, json={"text": ""}).status_code == 422
    msgs = parent.get("/api/device/chat", headers=barrett).json()
    assert [m["text"] for m in msgs] == ["Dinner in 10", "ok!"]
    assert msgs[1]["sender"] == "Adam" and not msgs[1]["mine"]
    assert parent.get("/api/device/chat", headers=ADAM).json()[1]["mine"]
    assert parent.get("/api/parent/chat").json()[0]["mine"]
    assert [m["text"] for m in parent.get(f"/api/parent/chat?after={msgs[0]['id']}").json()] == ["ok!"]


def test_approve_game_request_straight_from_steam(parent, monkeypatch):
    from app import main

    async def fake_lookup(appid):
        return {"name": "Slay the Spire 2", "header_image": "https://example/h.jpg"}
    monkeypatch.setattr(main, "steam_lookup", fake_lookup)
    parent.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "slay the spire 2"})
    rid = parent.get("/api/parent/requests").json()[0]["id"]
    assert parent.post(f"/api/parent/requests/{rid}", json={"approve": True, "steam_appid": "2868840"}).status_code == 200
    games = parent.get("/api/device/rules", headers=ADAM).json()["games"]
    assert any(g["title"] == "Slay the Spire 2" and g["launch_ref"] == "2868840" for g in games)


def test_kid_game_search_hides_adult_titles_and_request_carries_the_pick(parent, monkeypatch):
    from app import main

    async def store(q):
        return [{"id": 1, "name": "Fun Game", "type": "app"}, {"id": 2, "name": "Adult Game", "type": "app"}, {"id": 3, "name": "Fun Game OST", "type": "app"}]

    async def app_(appid, db=None):
        return {"1": {"appid": "1", "name": "Fun Game", "type": "game", "adult": False, "esrb": "E10+", "image_url": "x"},
                "2": {"appid": "2", "name": "Adult Game", "type": "game", "adult": True, "esrb": "AO", "image_url": "y"},
                "3": {"appid": "3", "name": "Fun Game OST", "type": "music", "adult": False, "esrb": "", "image_url": "z"}}[appid]
    monkeypatch.setattr(main, "steam_store_search", store)
    monkeypatch.setattr(main, "steam_app", app_)
    kid = parent.get("/api/device/steam/search?q=fun", headers=ADAM).json()
    assert [g["name"] for g in kid] == ["Fun Game"]
    assert len(parent.get("/api/parent/steam/search?q=fun").json()) == 3   # parents see every match
    assert parent.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "Fun Game", "ref": "steam:1"}).status_code == 201
    assert parent.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "x", "ref": "rm -rf"}).status_code == 422
    assert parent.get("/api/parent/requests").json()[0]["ref"] == "steam:1"


def test_rules_carry_a_family_dns_filter_by_default(client):
    dns = client.get("/api/device/rules", headers=ADAM).json()["web"]["dns"]
    assert dns["filter"] == "cloudflare_family" and "1.1.1.3" in dns["servers"] and dns["doh"].startswith("https://family.")


def test_history_upload_flags_searches(parent):
    rules_ = parent.get("/api/device/rules", headers=ADAM).json()["web"]["search"]
    assert "udm=14" in rules_["url"] and "google.com/search" in rules_["block"]
    h = {"visits": [{"url": "https://en.wikipedia.org/wiki/Cat", "title": "Cat", "at": "2026-11-24T11:00:00-05:00"}],
         "searches": [{"term": "minecraft seeds", "at": "2026-11-24T11:00:00-05:00"},
                      {"term": "Sexy pics", "at": "2026-11-24T11:01:00-05:00"},
                      {"term": "essex county", "at": "2026-11-24T11:02:00-05:00"}]}
    assert parent.post("/api/device/history", headers=ADAM, json=h).status_code == 204
    assert parent.post("/api/device/history", headers=ADAM, json=h).status_code == 204   # re-sent: no duplicates
    s = parent.get("/api/parent/searches/adam").json()
    assert [(x["term"], x["flagged"]) for x in s] == [("essex county", 0), ("Sexy pics", 1), ("minecraft seeds", 0)]
    assert next(k for k in parent.get("/api/parent/overview").json()["kids"] if k["id"] == "adam")["flagged_today"] == 1
    assert parent.get("/api/parent/visits/adam").json()[0]["domain"] == "en.wikipedia.org"


def test_decision_reason_reaches_the_kid(parent):
    parent.post("/api/device/requests", headers=ADAM, json={"kind": "site", "detail": "roblox.com"})
    rid = parent.get("/api/parent/requests").json()[0]["id"]
    parent.post(f"/api/parent/requests/{rid}", json={"approve": False, "reason": "Not until homework is done"})
    msgs = [c["arg"] for c in parent.get("/api/device/rules", headers=ADAM).json()["commands"] if c["kind"] == "message"]
    assert any("Not this time: roblox.com" in m and "Not until homework is done" in m for m in msgs)
