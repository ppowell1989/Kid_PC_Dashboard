"""Buying a game for a kid: the hub opens the store's own checkout, watches for the game to arrive, and installs it."""
import pytest

from .conftest import ADAM
from .test_installs import add, beat, pc


@pytest.fixture
def steam(monkeypatch):
    """Steam as the hub sees it: a price for every app, and an owned-games list the test changes."""
    from app import main, steamlink
    owned = {"appids": []}

    async def fake_app(appid, db=None):
        return {"appid": appid, "name": f"Game {appid}", "free": appid == "291550", "price": "$19.99"}

    async def fake_owned(key, steamid):
        return {"appids": list(owned["appids"]), "count": len(owned["appids"])}
    monkeypatch.setattr(main, "steam_app", fake_app)
    monkeypatch.setattr(steamlink, "fetch_owned", fake_owned)
    return owned


def link_steam(parent, client):
    beat(client, {}, steam_account={"id": "76561198000000001", "name": "AdamPlays", "login": "adam_p"})
    assert parent.put("/api/parent/steam", json={"api_key": "a" * 32}).status_code == 200


def test_buy_shows_the_steam_page_price_and_steps_and_changes_nothing(parent, client, steam):
    gid = add(parent, "Moving Out", "996770", kids=())
    link_steam(parent, client)
    r = parent.get(f"/api/parent/buy/adam/{gid}")
    assert r.status_code == 200, r.text
    g = r.json()["guide"]
    assert g["kind"] == "buy" and g["price"] == "$19.99" and g["url"] == "https://store.steampowered.com/app/996770/"
    assert any("Steam Family" in s for s in g["steps"]) and any("AdamPlays" in s for s in g["steps"])
    assert "checks" in g["detects"]
    assert parent.get("/api/device/rules", headers=ADAM).json()["installs"] == []   # a look changes nothing


def test_buying_approves_waits_then_installs_when_steam_has_it(parent, client, steam):
    from app.main import sweep_purchases, get_db
    gid = add(parent, "Moving Out", "996770", kids=())
    link_steam(parent, client)
    r = parent.post("/api/parent/purchases", json={"kid_id": "adam", "game_id": gid})
    assert r.status_code == 201, r.text
    p = r.json()["purchase"]
    assert p["state"] == "started" and p["price"] == "$19.99" and p["started_by"] == "Patrick" and p["url"]
    # Approved and on his install list straight away, so it installs the moment it's his.
    assert [i["game_id"] for i in parent.get("/api/device/rules", headers=ADAM).json()["installs"]] == [gid]
    row = next(g for g in pc(parent)["games"] if g["game_id"] == gid)
    assert row["plain"]["label"] == "Being bought" and row["plain"]["fix"] == "bought"
    # Pressing Buy twice doesn't make two purchases.
    assert parent.post("/api/parent/purchases", json={"kid_id": "adam", "game_id": gid}).json()["purchase"]["id"] == p["id"]
    assert len(parent.get("/api/parent/purchases").json()["open"]) == 1

    # The gift arrives: Steam lists it on his account. The hub closes the purchase and tells the PC to go again.
    steam["appids"] = ["996770"]
    parent.put("/api/parent/steam", json={})   # re-reads every kid's games now (the background check does this every 90 s)
    sweep_purchases(get_db())
    d = parent.get("/api/parent/purchases").json()
    assert d["open"] == [] and d["done"][0]["state"] == "bought" and d["done"][0]["confirmed"] == "steam"
    assert "Steam shows it" in d["done"][0]["words"]
    item = parent.get("/api/device/rules", headers=ADAM).json()["installs"][0]
    assert item["owned"] is True and item["retry"] == 1
    feed = parent.get("/api/parent/feed?group=parents").json()["items"]
    assert any(i["text"] == "Bought for Adam: Moving Out ($19.99)" for i in feed)


def test_a_family_shared_game_counts_as_bought_once_it_really_downloads(parent, client, steam):
    gid = add(parent, "Moving Out", "996770", kids=())
    link_steam(parent, client)
    pid = parent.post("/api/parent/purchases", json={"kid_id": "adam", "game_id": gid}).json()["purchase"]["id"]
    beat(client, {str(gid): {"state": "installing", "stage": "Waiting", "done_bytes": 0, "total_bytes": 0}})
    assert parent.get("/api/parent/purchases").json()["open"][0]["id"] == pid   # 0 bytes proves nothing
    beat(client, {str(gid): {"state": "installing", "stage": "Downloading", "done_bytes": 5, "total_bytes": 10}})
    done = parent.get("/api/parent/purchases").json()["done"][0]
    assert done["id"] == pid and done["confirmed"] == "pc"


def test_buy_from_a_request_approves_it_and_tells_the_kid(parent, client, steam):
    client.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "Moving Out", "ref": "steam:996770"})
    rid = parent.get("/api/parent/requests").json()[0]["id"]
    r = parent.post("/api/parent/purchases", json={"kid_id": "adam", "ref": "steam:996770", "request_id": rid})
    assert r.status_code == 201, r.text
    assert r.json()["purchase"]["request_id"] == rid
    assert parent.get("/api/parent/requests").json() == []
    msg = [c for c in parent.get("/api/device/rules", headers=ADAM).json()["commands"] if c["kind"] == "message"][-1]["arg"]
    assert "approved" in msg and "Patrick is getting it for you" in msg
    # The request can't be bought or decided a second time.
    assert parent.post("/api/parent/purchases", json={"kid_id": "adam", "ref": "steam:996770", "request_id": rid}).status_code == 409
    # Someone else's request isn't his.
    client.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "Other"})
    other = parent.get("/api/parent/requests").json()[0]["id"]
    assert parent.post("/api/parent/purchases", json={"kid_id": "barrett", "ref": "steam:996770", "request_id": other}).status_code == 404


def test_free_and_owned_games_need_no_purchase(parent, client, steam):
    free = add(parent, "Brawlhalla", "291550", kids=())
    owned = add(parent, "Portal 2", "620", kids=())
    steam["appids"] = ["620"]
    link_steam(parent, client)
    r = parent.post("/api/parent/purchases", json={"kid_id": "adam", "game_id": free}).json()
    assert r["guide"]["kind"] == "free" and r["purchase"] is None
    r = parent.post("/api/parent/purchases", json={"kid_id": "adam", "game_id": owned}).json()
    assert r["guide"]["kind"] == "owned" and r["purchase"] is None
    assert {i["game_id"] for i in parent.get("/api/device/rules", headers=ADAM).json()["installs"]} == {free, owned}


def test_battlenet_is_confirmed_by_the_parent_and_cancel_keeps_the_approval(parent, client):
    d4 = add(parent, "Diablo IV", "Fen", kids=(), launcher="battlenet")
    ow = add(parent, "Overwatch 2", "Pro", kids=(), launcher="battlenet")
    g = parent.get(f"/api/parent/buy/adam/{d4}").json()["guide"]
    assert g["kind"] == "buy" and g["url"].startswith("https://shop.battle.net/") and "I bought it" in g["detects"]
    assert parent.get(f"/api/parent/buy/adam/{ow}").json()["guide"]["kind"] == "free"

    pid = parent.post("/api/parent/purchases", json={"kid_id": "adam", "game_id": d4}).json()["purchase"]["id"]
    r = parent.post(f"/api/parent/purchases/{pid}/bought")
    assert r.status_code == 200 and r.json()["confirmed"] == "parent" and "by Patrick" in r.json()["words"]
    assert parent.post(f"/api/parent/purchases/{pid}/bought").status_code == 409

    wow = add(parent, "World of Warcraft", "WoW", kids=(), launcher="battlenet")
    r = parent.post("/api/parent/purchases", json={"kid_id": "barrett", "game_id": wow}).json()
    assert r["guide"]["kind"] == "subscription" and r["purchase"]["state"] == "started"
    c = parent.post(f"/api/parent/purchases/{r['purchase']['id']}/cancel").json()
    assert c["state"] == "cancelled" and c["words"] == "Not bought."
    assert wow in [i["game_id"] for i in parent.get("/api/device/rules", headers={"Authorization": "Bearer tok-barrett"}).json()["installs"]]


def test_epic_gift_shows_up_in_his_library(parent, client):
    gid = add(parent, "Hades", "Min", kids=(), launcher="epic")
    g = parent.get(f"/api/parent/buy/adam/{gid}").json()["guide"]
    assert g["kind"] == "buy" and g["url"].startswith("https://store.epicgames.com/") and "Hades" in g["url"]
    pid = parent.post("/api/parent/purchases", json={"kid_id": "adam", "game_id": gid}).json()["purchase"]["id"]
    beat(client, {}, epic_library=[{"app": "Min", "title": "Hades", "image_url": ""}])
    done = parent.get("/api/parent/purchases").json()["done"][0]
    assert done["id"] == pid and done["confirmed"] == "epic"


def test_the_hub_never_takes_payment_details(parent, steam):
    gid = add(parent, "Moving Out", "996770", kids=())
    r = parent.post("/api/parent/purchases", json={"kid_id": "adam", "game_id": gid, "card": "4111111111111111"})
    assert r.status_code == 201
    from app.main import get_db
    cols = {c["name"] for c in get_db().q("PRAGMA table_info(purchases)")}
    assert not cols & {"card", "card_number", "payment", "password", "receipt"}
    assert "4111" not in str(get_db().q("SELECT * FROM purchases"))
