"""Epic Games as a fourth store: approve = install, search (curated + each kid's Epic library), requests, shelves."""
import os
import sqlite3
import tempfile

import pytest

from app import gamesearch, main
from app.db import DB

from .conftest import ADAM

BARRETT = {"Authorization": "Bearer tok-barrett"}
FALL_GUYS = "0a2d9f6403244d12969e11da6713137b"


@pytest.fixture(autouse=True)
def no_stores(monkeypatch):
    """No store traffic: Steam finds nothing, Game Pass is unreachable."""
    async def none(*a, **k):
        return []
    async def no_gp(db):
        return None
    async def no_app(appid, db=None):
        return {}
    monkeypatch.setattr(main, "steam_store_search", none)
    monkeypatch.setattr(main, "steam_app", no_app)
    monkeypatch.setattr(gamesearch, "gamepass_catalogue", no_gp)


def test_approving_an_epic_game_gets_the_epic_app_installed(parent):
    r = parent.post("/api/parent/games/from-search", json={"ref": "epic:Fortnite", "approved_for": ["adam"]})
    assert r.status_code == 201, r.text
    rules = parent.get("/api/device/rules", headers=ADAM).json()
    assert rules["managed_apps"] == ["steam", "chrome", "epic"]
    g = next(g for g in rules["games"] if g["launcher"] == "epic")
    assert (g["title"], g["launch_ref"], g["rating"]) == ("Fortnite", "Fortnite", "T")
    assert [i["game_id"] for i in rules["installs"]] == [g["id"]]              # approve = install
    assert "epic" not in parent.get("/api/device/rules", headers=BARRETT).json()["managed_apps"]
    # The parent can push the launcher install again from the PCs page.
    assert parent.post("/api/parent/kids/adam/commands", json={"kind": "install_app", "arg": "epic"}).status_code == 201


def test_search_offers_epic_and_prefers_it_where_steam_no_longer_sells_the_game(parent):
    rows = parent.get("/api/parent/games/search?q=rocket league").json()["results"]
    rl = next(r for r in rows if r["title"] == "Rocket League")
    assert rl["platforms"][rl["default"]]["launcher"] == "epic" and rl["platforms"][rl["default"]]["launch_ref"] == "Sugar"
    # The same, with Steam knowing the game too: Epic still comes first.
    merged = gamesearch.merge("rocket league", [{"appid": "252950", "name": "Rocket League", "type": "game"}], [], [], False, {},
                              gamesearch.epic_search("rocket league"))
    assert len(merged) == 1 and [p["platform"] for p in merged[0]["platforms"]] == ["epic", "steam"]
    # A game on both where nothing says otherwise keeps Steam first.
    merged = gamesearch.merge("fortnite", [{"appid": "9", "name": "Fortnite", "type": "game"}], [], [], False, {}, gamesearch.epic_search("fortnite"))
    assert merged[0]["platforms"][0]["platform"] == "steam"


def test_the_pcs_epic_library_feeds_search_and_bad_entries_are_dropped(parent):
    lib = [{"app": "Anemone", "title": "Some Free Puzzle Game", "image_url": "https://cdn1.epicgames.com/a.jpg"},
           {"app": "bad app!", "title": "Nope"},                                  # not an app name
           {"app": "Wobbly", "title": "Wobbly Life", "image_url": "javascript:alert(1)"},
           "not even an object"]
    r = parent.post("/api/device/heartbeat", headers=ADAM, json={"epic_library": lib})
    assert r.status_code == 200, r.text                                     # one odd entry never fails the check-in
    rows = parent.get("/api/parent/games/search?q=puzzle").json()["results"]
    hit = next(r for r in rows if r["title"] == "Some Free Puzzle Game")
    p = hit["platforms"][0]
    assert (p["launcher"], p["launch_ref"], p["note"]) == ("epic", "Anemone", "On his Epic account")
    assert hit["image_url"] == "https://cdn1.epicgames.com/a.jpg"
    wob = next(r for r in parent.get("/api/parent/games/search?q=wobbly").json()["results"] if r["title"] == "Wobbly Life")
    assert wob["image_url"] == ""                                            # only https art
    assert not parent.get("/api/parent/games/search?q=nope").json()["results"]

    # Add it from search; an app name nobody has seen is refused.
    gid = parent.post("/api/parent/games/from-search", json={"ref": "epic:Anemone", "approved_for": ["adam"]}).json()["id"]
    g = next(g for g in parent.get("/api/parent/games").json() if g["id"] == gid)
    assert (g["title"], g["launcher"], g["image_url"]) == ("Some Free Puzzle Game", "epic", "https://cdn1.epicgames.com/a.jpg")
    assert parent.post("/api/parent/games/from-search", json={"ref": "epic:NeverHeardOf"}).status_code == 400
    # Barrett's PC search only sees his own library, which is empty, so Adam's game isn't offered to him.
    kid = parent.get("/api/device/games/search?q=puzzle", headers=BARRETT).json()
    assert kid == []
    kid = parent.get("/api/device/games/search?q=puzzle", headers=ADAM).json()
    assert kid[0]["ref"] == "epic:Anemone" and kid[0]["store"] == "Epic Games"


def test_kid_request_names_an_epic_game_with_a_long_app_id(parent):
    hits = parent.get("/api/device/games/search?q=fall guys", headers=ADAM).json()
    assert hits[0]["ref"] == f"epic:{FALL_GUYS}"
    r = parent.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "Fall Guys", "ref": f"epic:{FALL_GUYS}"})
    assert r.status_code == 201
    assert parent.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "x", "ref": "epic:a/b"}).status_code == 422


def test_manual_epic_game_and_its_install_status_words(parent):
    assert parent.post("/api/parent/games", json={"title": "X", "launcher": "epic", "launch_ref": "has space"}).status_code == 400
    gid = parent.post("/api/parent/games", json={"title": "Fortnite", "launcher": "epic", "launch_ref": "fortnite", "approved_for": ["adam"]}).json()["id"]
    assert next(g for g in parent.get("/api/parent/games").json() if g["id"] == gid)["launch_ref"] == "Fortnite"   # canonical case
    parent.post("/api/device/heartbeat", headers=ADAM, json={"games": {str(gid): {"state": "needs_signin", "detail": "sign in", "retry": 0, "stage": "Needs Epic sign-in"}}})
    pc = next(k for k in parent.get("/api/parent/installs").json()["pcs"] if k["id"] == "adam")
    row = next(g for g in pc["games"] if g["game_id"] == gid)
    assert row["state"] == "needs_signin" and row["plain"]["fix"] == "signin" and "the Epic Games app" in row["plain"]["text"] + row["plain"]["label"]
    parent.post("/api/device/heartbeat", headers=ADAM, json={"games": {str(gid): {"state": "installed", "retry": 0}}})
    pc = next(k for k in parent.get("/api/parent/installs").json()["pcs"] if k["id"] == "adam")
    assert next(g for g in pc["games"] if g["game_id"] == gid)["plain"]["can_remove"]


def test_epic_shelf_in_get_more_games(parent):
    parent.put("/api/parent/kids/adam/ideas", json={"rating": "T"})
    shelves = {s["title"]: s["games"] for s in parent.get("/api/device/games/discover", headers=ADAM).json()["shelves"]}
    epic = shelves["On Epic Games"]
    assert {g["ref"] for g in epic} == {"epic:Sugar", f"epic:{FALL_GUYS}", "epic:Fortnite"} and epic[0]["store"] == "Epic Games"


def test_an_older_hub_database_learns_the_epic_launcher():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "hub.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE games (id INTEGER PRIMARY KEY, title TEXT NOT NULL,
            launcher TEXT NOT NULL CHECK (launcher IN ('steam','battlenet','gamepass','direct')),
            launch_ref TEXT NOT NULL, rating TEXT DEFAULT '', image_url TEXT DEFAULT '');
        CREATE TABLE approvals (kid_id TEXT NOT NULL, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE, PRIMARY KEY (kid_id, game_id));
        INSERT INTO games(title,launcher,launch_ref) VALUES('Portal 2','steam','620');
        INSERT INTO approvals VALUES('adam', 1);
    """)
    old.commit()
    old.close()
    db = DB(path)
    db.x("INSERT INTO games(title,launcher,launch_ref) VALUES('Fortnite','epic','Fortnite')")
    assert [g["launcher"] for g in db.q("SELECT launcher FROM games ORDER BY id")] == ["steam", "epic"]
    assert db.q("SELECT * FROM approvals")[0]["game_id"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.x("INSERT INTO games(title,launcher,launch_ref) VALUES('x','origin','y')")   # the check still holds
    DB(path)   # opening again is a no-op
