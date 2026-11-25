"""Get more games: shelves to browse (brother's games, then ideas up to a rating cap), per-game play history, "New"."""
import pytest

from app import discover, main

from .conftest import ADAM

BARRETT = {"Authorization": "Bearer tok-barrett"}


@pytest.fixture(autouse=True)
def no_steam(monkeypatch):
    """No store traffic in tests: Steam "knows" one game's real rating, the rest use the written-in fallback."""
    async def fake(appid, db=None):
        return {"name": "Portal 2", "esrb": "E10+", "image_url": "art/620", "description": "Portals.", "adult": False} if appid == "620" else {}
    monkeypatch.setattr(main, "steam_app", fake)


def add(parent, title, ref, kids):
    return parent.post("/api/parent/games", json={"title": title, "launcher": "steam", "launch_ref": ref,
                                                  "image_url": "x", "approved_for": kids}).json()["id"]


def shelves(client, headers=ADAM):
    return {s["title"]: s["games"] for s in client.get("/api/device/games/discover", headers=headers).json()["shelves"]}


def test_rating_cap():
    assert discover.rating_ok("E", "E10+") and discover.rating_ok("E10+", "E10+")
    assert not discover.rating_ok("T", "E10+") and not discover.rating_ok("E", "off")
    assert not discover.rating_ok("", "T") and discover.rating_ok("", "M")


def test_ideas_respect_cap_and_leave_out_his_games(parent):
    add(parent, "Stardew Valley", "413150", ["adam"])
    s = shelves(parent)
    names = [g["name"] for games in s.values() for g in games]
    assert "Stardew Valley" not in names                       # already his
    assert "Portal 2" in names and "Kerbal Space Program" in names
    assert "Terraria" not in names and "Overwatch 2" not in names   # T is above the default E10+
    portal = next(g for g in s["Puzzles and adventures"] if g["name"] == "Portal 2")
    assert portal["ref"] == "steam:620" and portal["store"] == "Steam" and portal["description"] == "Portals." and not portal["asked"]

    assert parent.put("/api/parent/kids/adam/ideas", json={"rating": "T"}).status_code == 200
    names = [g["name"] for games in shelves(parent).values() for g in games]
    assert "Terraria" in names and "Overwatch 2" in names
    parent.put("/api/parent/kids/adam/ideas", json={"rating": "off"})
    assert shelves(parent) == {}
    assert parent.put("/api/parent/kids/adam/ideas", json={"rating": "AO"}).status_code == 422


def test_brother_shelf_and_asked(parent):
    add(parent, "Brawlhalla", "291550", ["barrett"])
    s = shelves(parent)
    assert [g["name"] for g in s["Barrett has these"]] == ["Brawlhalla"]
    assert all(g["name"] != "Brawlhalla" for t, games in s.items() if t != "Barrett has these" for g in games)   # shown once
    parent.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "Portal 2", "ref": "steam:620"})
    portal = next(g for g in shelves(parent)["Puzzles and adventures"] if g["name"] == "Portal 2")
    assert portal["asked"]


def test_play_history_and_new(parent):
    gid = add(parent, "Brawlhalla", "291550", ["adam"])
    g = parent.get("/api/device/rules", headers=ADAM).json()["games"][0]
    assert g["new"] and g["last_played"] == ""
    parent.post("/api/device/heartbeat", headers=ADAM, json={"running": "Brawlhalla", "active": True, "game_id": gid})
    g = parent.get("/api/device/rules", headers=ADAM).json()["games"][0]
    assert not g["new"] and g["last_played"]
    row = next(x for x in parent.get("/api/parent/games").json() if x["id"] == gid)
    assert row["kids"]["adam"]["minutes"] == 1 and row["kids"]["adam"]["last_played"]


def test_parent_ideas(parent):
    add(parent, "Portal 2", "620", ["barrett"])
    d = parent.get("/api/parent/games/ideas").json()
    assert d["caps"] == {"adam": "E10+", "barrett": "E10+"} and "off" in d["choices"]
    portal = next(i for i in d["ideas"] if i["ref"] == "steam:620")
    assert portal["game_id"] and portal["approved_for"] == ["barrett"] and portal["shelf"] == "Puzzles and adventures"
    assert any(i["ref"] == "battlenet:Pro" for i in d["ideas"])
