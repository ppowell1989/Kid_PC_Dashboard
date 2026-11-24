"""Family: brothers see each other's game, join it when it's approved for both, and share one voice room."""
import jwt

from .conftest import ADAM

BARRETT = {"Authorization": "Bearer tok-barrett"}


def add(parent, title, ref, kids):
    return parent.post("/api/parent/games", json={"title": title, "launcher": "steam", "launch_ref": ref, "approved_for": kids}).json()["id"]


def sibling(client, headers):
    fam = client.get("/api/device/rules", headers=headers).json()["family"]
    assert len(fam) == 1
    return fam[0]


def test_family_shows_brother_offline_then_playing(parent):
    b = sibling(parent, ADAM)
    assert b["name"] == "Barrett" and not b["online"] and b["join_game"] is None

    both = add(parent, "Brawlhalla", "291550", ["adam", "barrett"])
    parent.post("/api/device/heartbeat", headers=BARRETT, json={"running": "Brawlhalla", "active": True, "game_id": both})
    b = sibling(parent, ADAM)
    assert b["online"] and b["running"] == "Brawlhalla" and b["game_title"] == "Brawlhalla"
    assert b["join_game"]["id"] == both and b["join_game"]["launch_ref"] == "291550"
    assert b["ask_ref"] == ""


def test_join_needs_the_game_approved_for_both(parent):
    only_barrett = add(parent, "Terraria", "105600", ["barrett"])
    parent.post("/api/device/heartbeat", headers=BARRETT, json={"running": "Terraria", "active": True, "game_id": only_barrett})
    b = sibling(parent, ADAM)
    assert b["game_title"] == "Terraria" and b["join_game"] is None
    assert b["ask_ref"] == "steam:105600"   # Adam can ask for it instead


def test_unknown_game_id_is_ignored(parent):
    parent.post("/api/device/heartbeat", headers=BARRETT, json={"running": "Something", "active": True, "game_id": 999})
    b = sibling(parent, ADAM)
    assert b["running"] == "Something" and b["game_title"] == "" and b["join_game"] is None


def test_family_voice_token_and_presence(parent):
    r = parent.get("/api/device/family-voice-token", headers=ADAM)
    assert r.status_code == 200
    claims = jwt.decode(r.json()["token"], options={"verify_signature": False})
    assert r.json()["room"] == "family-room" and claims["video"]["room"] == "family-room" and claims["sub"] == "kid-adam"
    parent.post("/api/device/heartbeat", headers=ADAM, json={"voice": "family"})
    assert sibling(parent, BARRETT)["in_family_voice"]
    fam = parent.get("/api/parent/family").json()
    assert fam["voice_count"] == 1 and fam["settings"] == {"see": True, "join": True, "voice": True}


def test_parent_switches(parent):
    both = add(parent, "Brawlhalla", "291550", ["adam", "barrett"])
    parent.post("/api/device/heartbeat", headers=BARRETT, json={"running": "Brawlhalla", "active": True, "game_id": both})
    parent.post("/api/device/heartbeat", headers=ADAM, json={"running": "Brawlhalla", "active": True, "game_id": both})
    assert parent.get("/api/parent/family").json()["playing_together"]

    assert parent.put("/api/parent/family", json={"join": False}).json() == {"see": True, "join": False, "voice": True}
    assert sibling(parent, ADAM)["join_game"] is None

    parent.put("/api/parent/family", json={"voice": False})
    assert parent.get("/api/device/family-voice-token", headers=ADAM).status_code == 403
    assert parent.get("/api/device/rules", headers=ADAM).json()["family_voice"] is False

    parent.put("/api/parent/family", json={"see": False})
    assert parent.get("/api/device/rules", headers=ADAM).json()["family"] == []


def test_family_needs_parent_login(client):
    assert client.get("/api/parent/family").status_code == 401
    assert client.put("/api/parent/family", json={"see": False}).status_code == 401
