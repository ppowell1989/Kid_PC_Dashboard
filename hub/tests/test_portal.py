"""The reorganised dashboard: screen time by date, the activity feed, adding a kid, approved friends."""
from tests.conftest import ADAM

TUE = "2026-11-24"   # conftest freezes "now" at Tuesday 24 Nov 2026, noon UTC


def test_week_shows_usual_rules_and_a_dated_change_wins(parent):
    w = parent.get("/api/parent/screentime").json()
    assert w["start"] == "2026-11-23" and w["today"] == TUE
    adam = next(k for k in w["kids"] if k["id"] == "adam")
    assert [d["date"] for d in adam["week"]][:2] == ["2026-11-23", TUE] and not adam["week"][1]["changed"]
    r = parent.put("/api/parent/screentime/adam/dates", json={"dates": [TUE, "2026-11-25"], "daily_minutes": 0, "label": "Sick day"})
    assert r.status_code == 200
    # Today's rule is the dated one: no play, so he is locked for time.
    st = parent.get("/api/parent/overview").json()["kids"][0]["time"]
    assert st["allowance"] == 0 and st["locked"]
    adam = next(k for k in parent.get("/api/parent/screentime").json()["kids"] if k["id"] == "adam")
    assert adam["week"][1]["changed"] and adam["week"][1]["label"] == "Sick day"
    assert adam["upcoming"] == [{"from": TUE, "to": "2026-11-25", "label": "Sick day", "days": 2, "daily_minutes": 0}]
    parent.post("/api/parent/screentime/adam/dates/clear", json={"dates": [TUE]})
    assert parent.get("/api/parent/overview").json()["kids"][0]["time"]["allowance"] == 60


def test_copy_week_makes_dated_changes_next_week(parent):
    parent.put("/api/parent/screentime/adam/dates", json={"dates": ["2026-11-27"], "daily_minutes": 240, "label": "Holiday"})
    assert parent.post("/api/parent/screentime/adam/copy-week", json={"start": TUE, "weeks": 2}).json()["copied"] == 14
    nxt = next(k for k in parent.get("/api/parent/screentime?start=2026-12-01").json()["kids"] if k["id"] == "adam")["week"]
    assert nxt[4]["daily_minutes"] == 240 and nxt[4]["changed"] and nxt[0]["daily_minutes"] == 60


def test_bad_windows_are_refused(parent):
    r = parent.put("/api/parent/screentime/adam/dates", json={"dates": [TUE], "daily_minutes": 60,
                                                              "windows": [{"start": "15:00", "end": "14:00"}]})
    assert r.status_code == 422


def test_feed_has_play_sessions_blocked_and_parent_actions(parent):
    for _ in range(3):
        parent.post("/api/device/heartbeat", headers=ADAM, json={"active": True, "running": "Minecraft"})
    parent.post("/api/device/events", headers=ADAM, json=[{"kind": "game_blocked", "detail": "Fortnite", "at": f"{TUE}T11:00:00"},
                                                          {"kind": "battlenet_products", "detail": "wow", "at": f"{TUE}T11:01:00"}])
    parent.post("/api/parent/kids/adam/grant", json={"minutes": 15})
    f = parent.get("/api/parent/feed?kid=adam").json()
    texts = [i["text"] for i in f["items"]]
    assert "Adam played Minecraft" in texts
    assert any("isn't approved: Fortnite" in t for t in texts)
    assert any("gave Adam 15 more minutes" in t for t in texts)
    assert not any("wow" in t for t in texts)
    assert f["counts"]["played"] == 1
    assert all(i["group"] == "blocked" for i in parent.get("/api/parent/feed?kid=adam&group=blocked").json()["items"])


def test_voice_call_names_the_friend_who_joined(parent):
    code = parent.post("/api/parent/invites", json={"kid_id": "adam", "friend_name": "Sam"}).json()["code"]
    parent.post("/api/device/heartbeat", headers=ADAM, json={"active": True, "voice": "friends"})
    parent.post(f"/api/voice/join/{code}")
    parent.post("/api/device/heartbeat", headers=ADAM, json={"active": True, "voice": "friends"})
    f = parent.get("/api/parent/feed?kid=adam&group=chats").json()["items"]
    assert any(i["text"] == "Voice call with Sam" for i in f)
    fr = parent.get("/api/parent/friends").json()
    assert fr[0]["friend_name"] == "Sam" and fr[0]["calls"] == 1


def test_add_a_kid_from_the_dashboard(parent):
    r = parent.post("/api/parent/kids", json={"name": "Ella Rose"})
    assert r.status_code == 201 and r.json()["id"] == "ellarose"
    assert parent.post("/api/parent/kids", json={"name": "ella rose"}).status_code == 409
    names = [k["name"] for k in parent.get("/api/parent/overview").json()["kids"]]
    assert names == ["Adam", "Barrett", "Ella Rose"]
    code = parent.post("/api/parent/kids/ellarose/setup-code").json()["code"]
    assert code
    assert parent.get("/api/parent/devices").json()["ellarose"] == {"devices": [], "added_here": True}
