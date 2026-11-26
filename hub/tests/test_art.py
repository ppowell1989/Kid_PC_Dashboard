"""Every curated game has cover art (checked on the real internet by art_audit.py on CI)."""
from app import discover, gamesearch
from app.main import with_art


def test_every_curated_game_has_art_except_the_arcade_collection():
    for b in gamesearch.BATTLENET:
        url = gamesearch.art_for("battlenet", b["code"])
        assert url.startswith("https://") or b["code"] == "RTRO", b["title"]
    for e in gamesearch.EPIC:
        assert gamesearch.art_for("epic", e["app"]).startswith("https://"), e["title"]
    for i in discover.all_ideas():
        assert gamesearch.art_for(i["launcher"], i["launch_ref"]) or i["launch_ref"] == "RTRO", i["title"]


def test_art_lookup_ignores_case_and_prefers_a_parents_picture():
    assert gamesearch.art_for("battlenet", "wtcg") == gamesearch.art_for("battlenet", "WTCG") != ""
    assert gamesearch.art_for("battlenet", "Pro").endswith("/2357570/header.jpg")   # Overwatch 2: its Steam twin
    assert gamesearch.art_for("epic", "Sugar").endswith("/252950/header.jpg")      # Rocket League
    assert gamesearch.art_for("steam", "620").endswith("/620/header.jpg")
    assert gamesearch.art_for("epic", "SomeLibraryGame") == "" and gamesearch.art_for("direct", "C:\\x.exe") == ""
    assert with_art({"launcher": "battlenet", "launch_ref": "S2", "image_url": ""})["image_url"].startswith("https://")
    assert with_art({"launcher": "battlenet", "launch_ref": "S2", "image_url": "https://mine/x.png"})["image_url"] == "https://mine/x.png"


def test_search_rows_for_battlenet_and_epic_games_carry_art():
    rows = gamesearch.merge("hearthstone", [], gamesearch.battlenet_search("hearthstone"), [], False, {})
    assert rows[0]["image_url"].startswith("https://")
    rows = gamesearch.merge("fortnite", [], [], [], False, {}, epic=gamesearch.epic_search("fortnite"))
    assert rows[0]["image_url"].startswith("https://")
