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
    assert origin_allowed(s, "8.8.8.8", "/static/fonts/nunito.woff2")
    assert not origin_allowed(s, "8.8.8.8", "/static/app.js")   # but not the dashboard's code
    assert not origin_allowed(s, "100.100.1.1", "/")
    s.decisions.remote_access = "tailscale"
    assert origin_allowed(s, "100.100.1.1", "/")
    r = client.get("/api/parent/overview", headers={"x-real-ip": "8.8.8.8"})
    assert r.status_code == 403
    # Through the Cloudflare tunnel the last hop is local; the visitor's real address still decides.
    r = client.get("/api/parent/overview", headers={"cf-connecting-ip": "8.8.8.8", "x-real-ip": "192.168.1.5"})
    assert r.status_code == 403
    assert client.get("/static/app.css", headers={"cf-connecting-ip": "8.8.8.8"}).status_code == 200


def test_pages_are_revalidated_not_served_stale(client):
    for path in ("/", "/static/app.js", "/static/app.css"):
        assert client.get(path).headers["cache-control"] == "no-cache, private", path
    assert "cache-control" not in client.get("/healthz").headers


KID = Kid(id="a", name="A", device_token="t", daily_minutes={"weekday": 60, "weekend": 120},
          bedtime={"weekday": "20:30", "weekend": "21:30"})
TZ = ZoneInfo("UTC")


def day(kid, now):
    return rules.default_rule(kid, now.weekday())


def test_rules_bedtime_and_warning():
    tue_eve = datetime(2026, 11, 24, 20, 25, tzinfo=TZ)   # Tuesday, 5 min to bedtime
    st = rules.compute(day(KID, tue_eve), tue_eve, used=10, granted=0)
    assert st.minutes_left == 5 and st.warn and not st.locked
    late = datetime(2026, 11, 24, 20, 40, tzinfo=TZ)
    st = rules.compute(day(KID, late), late, used=10, granted=0)
    assert st.locked and st.lock_reason == "bedtime"
    st = rules.compute(day(KID, late), late, used=10, granted=20, late_until=datetime(2026, 11, 24, 21, 0, tzinfo=TZ))
    assert not st.locked and st.minutes_left == 20


def test_rules_out_of_time_and_weekend():
    sat = datetime(2026, 11, 28, 12, 0, tzinfo=TZ)
    assert rules.compute(day(KID, sat), sat, used=60, granted=0).minutes_left == 60
    st = rules.compute(day(KID, sat), sat, used=120, granted=0)
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


# Playtimes -------------------------------------------------------------------

AFTER_SCHOOL = rules.DayRule(daily_minutes=90, bedtime="20:30", windows=[("15:30", "17:30"), ("19:00", "20:30")])


def test_playtime_windows_lock_outside_and_cap_inside():
    tue = lambda h, m: datetime(2026, 11, 24, h, m, tzinfo=TZ)
    every_day = lambda wd: AFTER_SCHOOL
    st = rules.compute(AFTER_SCHOOL, tue(12, 0), used=0, granted=0, rule_for=every_day)
    assert st.locked and st.lock_reason == "outside_hours" and st.minutes_left == 0
    assert st.next_play == tue(15, 30).isoformat()
    st = rules.compute(AFTER_SCHOOL, tue(17, 20), used=0, granted=0, rule_for=every_day)
    assert not st.locked and st.minutes_left == 10 and st.warn and st.minutes_to_stop == 10   # window ends 17:30
    st = rules.compute(AFTER_SCHOOL, tue(17, 45), used=30, granted=0, rule_for=every_day)
    assert st.lock_reason == "outside_hours" and st.next_play == tue(19, 0).isoformat()
    st = rules.compute(AFTER_SCHOOL, tue(20, 40), used=30, granted=0, rule_for=every_day)
    assert st.lock_reason == "bedtime"
    # After the last window the next play time is tomorrow's first window.
    no_eve = rules.DayRule(daily_minutes=60, bedtime="21:00", windows=[("15:30", "17:30")])
    st = rules.compute(no_eve, tue(18, 0), used=0, granted=0, rule_for=lambda wd: no_eve)
    assert st.next_play == datetime(2026, 11, 25, 15, 30, tzinfo=TZ).isoformat()
    # Out of minutes wins over the window.
    assert rules.compute(AFTER_SCHOOL, tue(16, 0), used=90, granted=0).lock_reason == "time"
    # A parent's grant outside the windows lets him play until it runs out.
    st = rules.compute(AFTER_SCHOOL, tue(12, 0), used=0, granted=20, late_until=tue(12, 20))
    assert not st.locked and st.minutes_left == 20


def test_playtime_no_windows_and_no_bedtime():
    st = rules.compute(rules.DayRule(daily_minutes=120), datetime(2026, 11, 24, 23, 30, tzinfo=TZ), used=0, granted=0)
    assert not st.locked and st.minutes_left == 30   # runs to midnight
    st = rules.compute(rules.DayRule(daily_minutes=0, bedtime="21:00"), datetime(2026, 11, 24, 12, 0, tzinfo=TZ), used=0, granted=0)
    assert st.locked and st.lock_reason == "time"     # 0 minutes = no play that day


def test_playtimes_api_defaults_save_and_enforce(parent):
    from app.main import app
    # The DB owns the rules: config.yaml's values were copied in at startup, one row per kid per day.
    assert app.state.db.one("SELECT COUNT(*) n FROM playtimes")["n"] == 14
    pt = parent.get("/api/parent/playtimes").json()
    assert pt["today"] == 1   # frozen at Tuesday
    adam = next(k for k in pt["kids"] if k["id"] == "adam")
    assert [d["name"] for d in adam["days"]] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert adam["days"][1] == {"day": 1, "name": "Tue", "daily_minutes": 60, "bedtime": "21:00", "windows": [], "custom": False, "updated_by": ""}

    # Weekdays: after school only. Now is Tuesday noon, so Adam locks.
    body = {"days": [0, 1, 2, 3, 4], "daily_minutes": 90, "bedtime": "20:30",
            "windows": [{"start": "19:00", "end": "20:30"}, {"start": "15:30", "end": "17:30"}]}
    r = parent.put("/api/parent/playtimes/adam", json=body).json()
    assert r["days"][1]["windows"] == [{"start": "15:30", "end": "17:30"}, {"start": "19:00", "end": "20:30"}]   # sorted
    assert r["days"][1]["custom"] and r["days"][1]["updated_by"] == "Patrick" and not r["days"][5]["custom"]
    assert r["time"]["lock_reason"] == "outside_hours" and r["time"]["next_play"].startswith("2026-11-24T15:30")
    dev = parent.get("/api/device/rules", headers=ADAM).json()["time"]
    assert dev["locked"] and dev["lock_reason"] == "outside_hours" and dev["minutes_left"] == 0
    assert parent.get("/api/device/rules", headers={"Authorization": "Bearer tok-barrett"}).json()["time"]["locked"] is False

    # Granting time outside the windows unlocks him for that long.
    st = parent.post("/api/parent/kids/adam/grant", json={"minutes": 15}).json()
    assert not st["locked"] and st["minutes_left"] == 15

    # Bad input is refused.
    bad = lambda **kw: parent.put("/api/parent/playtimes/adam", json={**body, **kw}).status_code
    assert bad(windows=[{"start": "17:00", "end": "16:00"}]) == 422
    assert bad(windows=[{"start": "15:00", "end": "17:00"}, {"start": "16:30", "end": "18:00"}]) == 422
    assert bad(bedtime="25:00") == 422 and bad(days=[7]) == 422 and bad(daily_minutes=-1) == 422
    assert parent.put("/api/parent/playtimes/nobody", json=body).status_code == 404
    # A window may run to midnight, and bedtime may be left empty.
    assert bad(bedtime="", windows=[{"start": "18:00", "end": "24:00"}]) == 200


def _stub_stores(monkeypatch, gamepass=True):
    from app import gamesearch, main

    steam = {"2357570": {"appid": "2357570", "name": "Overwatch® 2", "type": "game", "image_url": "ow.jpg", "esrb": "T", "adult": False},
             "105600": {"appid": "105600", "name": "Terraria", "type": "game", "image_url": "t.jpg", "esrb": "T", "adult": False}}

    async def store(q):
        return [{"id": int(k), "name": v["name"]} for k, v in steam.items() if gamesearch.matches(q, v["name"])]

    async def app_(appid, db=None):
        return steam.get(appid, {})

    async def fetch(client):
        if not gamepass:
            raise RuntimeError("offline")
        return [gamesearch.xbox_product({"ProductId": "9NBLGGH4R315", "LocalizedProperties": [{"ProductTitle": "Minecraft for Windows",
                    "Images": [{"ImagePurpose": "BoxArt", "Uri": "//store-images.s-microsoft.com/image/mc"}]}],
                    "MarketProperties": [{"ContentRatings": [{"RatingSystem": "ESRB", "RatingId": "ESRB:E10"}]}]}),
                gamesearch.xbox_product({"ProductId": "9PGW18NPBZV5", "LocalizedProperties": [{"ProductTitle": "Overwatch 2"}]})]

    monkeypatch.setattr(main, "steam_store_search", store)
    monkeypatch.setattr(main, "steam_app", app_)
    monkeypatch.setattr(gamesearch, "_fetch_gamepass", fetch)


def test_game_search_merges_stores_and_fills_in_the_launcher(parent, monkeypatch):
    _stub_stores(monkeypatch)
    r = parent.get("/api/parent/games/search?q=overwatch").json()
    assert r["xbox"] == "ok"
    ow = r["results"][0]
    assert ow["title"] == "Overwatch® 2" and ow["image_url"] == "ow.jpg" and ow["esrb"] == "T"
    assert [p["platform"] for p in ow["platforms"]] == ["steam", "battlenet", "xbox"]
    assert ow["default"] == 0 and ow["platforms"][2]["addable"] is False   # Game Pass is off in the test config

    # Battle.net-only games come from the curated list, with no Steam id anywhere.
    wow = parent.get("/api/parent/games/search?q=world of war").json()["results"][0]
    p = wow["platforms"][0]
    assert (p["launcher"], p["launch_ref"], p["addable"]) == ("battlenet", "WoW", True)
    assert parent.post("/api/parent/games/from-search", json={"ref": "battlenet:WoW", "approved_for": ["adam"]}).status_code == 201
    g = parent.get("/api/device/rules", headers=ADAM).json()["games"]
    assert [(x["title"], x["launcher"], x["launch_ref"], x["rating"]) for x in g] == [("World of Warcraft", "battlenet", "WoW", "T")]
    # Adding it again returns the same game, and search now says it's in the catalogue.
    again = parent.post("/api/parent/games/from-search", json={"ref": "battlenet:wow", "approved_for": ["barrett"]}).json()["id"]
    assert again == g[0]["id"]
    assert parent.get("/api/parent/games/search?q=world of warcraft").json()["results"][0]["platforms"][0]["game_id"] == again


def test_game_pass_results_follow_the_decision_switch(parent, monkeypatch):
    _stub_stores(monkeypatch)
    mc = parent.get("/api/parent/games/search?q=minecraft").json()["results"][0]
    assert mc["platforms"][0]["platform"] == "xbox" and mc["default"] is None and mc["esrb"] == "E10+"
    assert mc["image_url"] == "https://store-images.s-microsoft.com/image/mc?w=460"
    assert parent.post("/api/parent/games/from-search", json={"ref": "gamepass:9NBLGGH4R315"}).status_code == 400


def test_game_search_works_without_microsoft(parent, monkeypatch):
    _stub_stores(monkeypatch, gamepass=False)
    r = parent.get("/api/parent/games/search?q=terraria").json()
    assert r["xbox"] == "unavailable" and r["results"][0]["platforms"][0]["launch_ref"] == "105600"


def test_approve_game_request_with_a_battlenet_pick(parent):
    rid = parent.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "Hearthstone"}).json()["id"]
    assert parent.post(f"/api/parent/requests/{rid}", json={"approve": True, "ref": "battlenet:WTCG"}).status_code == 200
    assert [x["launch_ref"] for x in parent.get("/api/device/rules", headers=ADAM).json()["games"]] == ["WTCG"]


# --- bedtime holds through the night ------------------------------------------------------------

def test_midnight_does_not_unlock_a_day_with_no_windows():
    from app import rules
    rule = rules.DayRule(daily_minutes=90, bedtime="20:30")
    same = lambda wd: rule
    at = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=TZ)
    for now in (at(25, 0, 5), at(25, 3, 0), at(25, 5, 59)):
        st = rules.compute(rule, now, used=0, granted=0, rule_for=same)
        assert st.locked and st.lock_reason == "bedtime" and st.minutes_left == 0, now
        assert st.next_play == at(25, 6, 0).isoformat()
    st = rules.compute(rule, at(25, 6, 0), used=0, granted=0, rule_for=same)
    assert not st.locked and st.minutes_left == 90
    # After bedtime the lock screen can say when play starts again.
    assert rules.compute(rule, at(24, 21, 0), used=0, granted=0, rule_for=same).next_play == at(25, 6, 0).isoformat()


def test_no_bedtime_the_night_before_leaves_the_small_hours_open():
    from app import rules
    rule = rules.DayRule(daily_minutes=90, bedtime="")
    st = rules.compute(rule, datetime(2026, 11, 25, 0, 30, tzinfo=TZ), used=0, granted=0, rule_for=lambda wd: rule)
    assert not st.locked


def test_extra_time_granted_before_midnight_runs_past_it(parent, monkeypatch):
    from app import rules
    now = [datetime(2026, 11, 24, 23, 50, tzinfo=TZ)]
    monkeypatch.setattr(rules, "local_now", lambda tz: now[0])
    assert parent.get("/api/device/rules", headers=ADAM).json()["time"]["lock_reason"] == "bedtime"
    parent.post("/api/parent/kids/adam/grant", json={"minutes": 30})
    assert not parent.get("/api/device/rules", headers=ADAM).json()["time"]["locked"]
    now[0] = datetime(2026, 11, 25, 0, 10, tzinfo=TZ)
    t = parent.get("/api/device/rules", headers=ADAM).json()["time"]
    assert not t["locked"] and t["minutes_left"] == 10
    now[0] = datetime(2026, 11, 25, 0, 21, tzinfo=TZ)
    assert parent.get("/api/device/rules", headers=ADAM).json()["time"]["lock_reason"] == "bedtime"


def test_minutes_played_offline_are_counted_when_the_pc_comes_back(parent):
    r = parent.post("/api/device/heartbeat", headers=ADAM, json={"running": "Portal 2", "active": True, "offline_minutes": 45}).json()
    assert r["time"]["used"] == 46 and r["time"]["minutes_left"] == 14
    ev = parent.get("/api/parent/kids/adam/activity").json()["events"]
    assert any(e["kind"] == "offline_play" and e["detail"].startswith("45 min") for e in ev)
    # An ordinary heartbeat adds nothing extra and writes no event.
    parent.post("/api/device/heartbeat", headers=ADAM, json={"running": "", "active": False})
    assert len([e for e in parent.get("/api/parent/kids/adam/activity").json()["events"] if e["kind"] == "offline_play"]) == 1
    assert parent.post("/api/device/heartbeat", headers=ADAM, json={"offline_minutes": 5000}).status_code == 422


# --- hardening ----------------------------------------------------------------------------------

def test_wrong_passwords_are_slowed_down(client):
    from app import main
    main._login_failures.clear()
    bad = {"name": "Patrick", "password": "guess"}
    for _ in range(main.LOGIN_TRIES):
        assert client.post("/api/parent/login", json=bad).status_code == 401
    r = client.post("/api/parent/login", json=bad)
    assert r.status_code == 429 and "Try again in" in r.json()["detail"]
    # Even the right password waits now, and another name on the same device is not held up.
    assert client.post("/api/parent/login", json={"name": "Patrick", "password": "pw"}).status_code == 429
    assert client.post("/api/parent/login", json={"name": "Barbara", "password": "x"}).status_code == 401
    main._login_failures.clear()
    assert client.post("/api/parent/login", json={"name": "Patrick", "password": "pw"}).status_code == 200


def test_voice_tokens_are_microphone_only_and_refused_while_locked(parent):
    tok = parent.get("/api/device/voice-token", headers=ADAM).json()["token"]
    assert jwt.decode(tok, "s" * 32, ["HS256"])["video"]["canPublishSources"] == ["microphone"]
    parent.post("/api/parent/kids/adam/pause", json={"minutes": 0})
    assert parent.get("/api/device/voice-token", headers=ADAM).status_code == 403


def test_time_requests_and_answers_stay_in_range(parent):
    assert parent.post("/api/device/requests", headers=ADAM, json={"kind": "time", "detail": "99999"}).status_code == 422
    rid = parent.post("/api/device/requests", headers=ADAM, json={"kind": "time", "detail": "30"}).json()["id"]
    assert parent.post(f"/api/parent/requests/{rid}", json={"approve": True, "minutes": -5}).status_code == 422
    assert parent.post(f"/api/parent/requests/{rid}", json={"approve": True, "minutes": 20}).status_code == 200


def test_new_pc_setup_code(parent):
    """A parent's setup code turns into a script, and the script's claim returns that kid's tokens and build."""
    import json as _json
    from app.main import app as _app
    d = _app.state.updates_dir
    (d / "kidspc-win-x64.zip").write_bytes(b"PK-fake")
    (d / "x64.json").write_text(_json.dumps({"version": "abc123def456", "sha256": "00", "size": 7}))

    made = parent.post("/api/parent/kids/barrett/setup-code").json()
    code = made["code"]
    assert len(code) == 8 and made["command"] == f"irm http://testserver/setup/{code} | iex"
    assert made["builds"] == {"x64": True, "arm64": False}
    assert parent.post("/api/parent/kids/nobody/setup-code").status_code == 404

    script = parent.get(f"/setup/{code.lower()}").text      # read off a phone: case doesn't matter
    assert "http://testserver" in script and code in script and "Barrett" in script and "tok-barrett" not in script
    assert "not valid" in parent.get("/setup/NOPE1234").text

    claim = parent.post(f"/api/setup/{code}/claim", json={"computer": "BARRETT-PC", "arch": "x64"}).json()
    assert claim["device_token"] == "tok-barrett" and claim["windows_user"] == "barrett" and claim["build"]["version"] == "abc123def456"
    # Usable again until it expires, so a run that failed half-way can simply be repeated.
    assert parent.post(f"/api/setup/{code}/claim", json={"computer": "BARRETT-PC", "arch": "x64"}).status_code == 200
    assert parent.post(f"/api/setup/{code}/claim", json={"arch": "arm64"}).status_code == 409    # no ARM build on this hub
    assert parent.post("/api/setup/NOPE1234/claim", json={}).status_code == 404
    events = parent.get("/api/parent/kids/barrett/activity").json()["events"]
    assert any(e["kind"] == "pc_setup" and "BARRETT-PC" in e["detail"] for e in events)

    # Expired codes stop working.
    _app.state.db.x("UPDATE setup_codes SET expires_at=0 WHERE code=?", (code,))
    assert parent.post(f"/api/setup/{code}/claim", json={}).status_code == 404


def test_setup_is_home_network_only(client):
    from fastapi.testclient import TestClient
    from app.main import app as _app
    with TestClient(_app, client=("8.8.8.8", 5000)) as outside:
        assert outside.get("/setup/ABCDEFGH").status_code == 403
        assert outside.post("/api/setup/ABCDEFGH/claim", json={}).status_code == 403


def test_history_is_pruned_and_can_be_cleared(parent):
    from app import retention
    from app.main import app
    db = app.state.db
    h = {"visits": [{"url": "https://old.example/a", "at": "2026-09-01T10:00:00Z"},
                    {"url": "https://new.example/b", "at": "2026-11-24T10:00:00Z"}],
         "searches": [{"term": "old thing", "at": "2026-08-01T10:00:00Z"}, {"term": "new thing", "at": "2026-11-23T10:00:00Z"}]}
    assert parent.post("/api/device/history", headers=ADAM, json=h).status_code == 204
    db.x("INSERT INTO events(kid_id,kind,detail,at) VALUES('adam','game_blocked','x','2026-09-01T10:00:00Z')")
    db.x("INSERT INTO chat(sender,kid_id,text,at) VALUES('Adam','adam','hi','2026-01-01T10:00:00')")
    db.x("INSERT INTO requests(kid_id,kind,detail,state,created_at,decided_at) VALUES('adam','time','15','declined','2026-09-01 10:00:00','2026-09-01 10:05:00')")
    db.x("INSERT INTO requests(kid_id,kind,detail,created_at) VALUES('adam','time','15','2026-09-01 10:00:00')")   # still pending: kept

    r = parent.get("/api/parent/retention").json()
    assert r["days"]["visits"] == 30 and r["days"]["invites"] == 7
    # Saving the settings prunes straight away with the defaults.
    assert parent.put("/api/parent/retention", json={"days": {"visits": 30}}).status_code == 200
    assert [v["domain"] for v in parent.get("/api/parent/visits/adam").json()] == ["new.example"]
    assert [s["term"] for s in parent.get("/api/parent/searches/adam").json()] == ["new thing"]   # 90 days: Aug 1 is gone
    assert db.one("SELECT COUNT(*) n FROM events")["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM chat")["n"] == 0
    assert [q["state"] for q in db.q("SELECT state FROM requests")] == ["pending"]

    # "Today" and "last 7 days" windows on the Web tab.
    assert [s["term"] for s in parent.get("/api/parent/searches/adam?days=1").json()] == []
    assert [s["term"] for s in parent.get("/api/parent/searches/adam?days=7").json()] == ["new thing"]

    # Keep forever (0) and bad values.
    assert parent.put("/api/parent/retention", json={"days": {"visits": 0}}).json()["days"]["visits"] == 0
    assert parent.put("/api/parent/retention", json={"days": {"visits": 5}}).status_code == 422
    assert parent.put("/api/parent/retention", json={"days": {"nope": 7}}).status_code == 422

    # Clear by hand, for one kid.
    parent.post("/api/device/history", headers={"Authorization": "Bearer tok-barrett"}, json={"visits": [{"url": "https://b.example", "at": "2026-11-24T09:00:00Z"}]})
    assert parent.delete("/api/parent/history/visits?kid_id=adam").json() == {"deleted": 1}
    assert parent.get("/api/parent/visits/adam").json() == []
    assert len(parent.get("/api/parent/visits/barrett").json()) == 1
    assert parent.delete("/api/parent/history/everything").status_code == 404
    assert retention.get(db)["visits"] == 0


def test_revoked_invites_can_be_cleared(parent):
    from app.main import app
    a = parent.post("/api/parent/invites", json={"kid_id": "adam", "friend_name": "Sam"}).json()
    b = parent.post("/api/parent/invites", json={"kid_id": "adam", "friend_name": "Max"}).json()
    parent.delete(f"/api/parent/invites/{a['code']}")
    rows = parent.get("/api/parent/invites").json()
    assert {r["friend_name"]: r["revoked"] for r in rows} == {"Sam": 1, "Max": 0}
    assert [r for r in rows if r["revoked"]][0]["revoked_at"]
    # Old revoked links go on their own once past the window.
    app.state.db.x("UPDATE invites SET revoked_at='2026-11-01 10:00:00' WHERE code=?", (a["code"],))
    parent.put("/api/parent/retention", json={"days": {"invites": 7}})
    assert [r["friend_name"] for r in parent.get("/api/parent/invites").json()] == ["Max"]
    parent.delete(f"/api/parent/invites/{b['code']}")
    assert parent.delete("/api/parent/history/invites").json() == {"deleted": 1}
    assert parent.get("/api/parent/invites").json() == []
