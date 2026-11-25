"""Skylight chore points: a parent links Skylight, the kids see their points and spend them on extra time."""
import pytest

from app import skylight

from .conftest import ADAM


class FakeSkylight:
    """Stands in for Skylight's API: one household, two kids on the chore chart, a points balance each."""
    def __init__(self):
        self.points = {"11": 25, "12": 4}
        self.calls = []

    async def call(self, db, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/frames":
            return {"data": [{"id": "900", "type": "frame", "attributes": {"name": "Powell home"}}]}
        if path.endswith("/categories"):
            return {"data": [{"id": "11", "attributes": {"id": 11, "label": "Adam ", "linked_to_profile": True}},
                             {"id": "12", "attributes": {"id": 12, "label": "Barrett", "linked_to_profile": True}},
                             {"id": "13", "attributes": {"label": "Groceries"}}]}
        if path.endswith("/reward_points") and method == "GET":
            return [{"category_id": int(k), "current_point_balance": v, "lifetime_points_earned": 100} for k, v in self.points.items()]
        if path.endswith("/reward_points") and method == "POST":
            for c in body["category_ids"]:
                self.points[c] += body["points"]
            return {}
        raise AssertionError(path)


@pytest.fixture
def sky(monkeypatch):
    fake = FakeSkylight()
    monkeypatch.setattr(skylight, "call", fake.call)
    skylight._balances.update(at=0.0, by_member={}, error="")
    return fake


def link(parent):
    db = parent.app.state.db
    skylight.save(db, {**skylight.load(db), "access_token": "a", "refresh_token": "r", "expires_at": 9e12})


def test_not_linked_means_no_points(parent, sky):
    assert parent.get("/api/device/rules", headers=ADAM).json()["points"] is None
    assert parent.post("/api/device/points/buy", headers=ADAM).status_code == 400
    assert parent.get("/api/parent/skylight").json()["linked"] is False


def test_link_matches_kids_and_buys_time(parent, sky):
    link(parent)
    d = parent.get("/api/parent/skylight").json()
    assert d["frame_id"] == "900" and d["members"] == {"adam": "11", "barrett": "12"}
    assert [p["name"] for p in d["people"]] == ["Adam ", "Barrett"] and d["balances"]["11"] == 25

    pts = parent.get("/api/device/rules", headers=ADAM).json()["points"]
    assert pts == {"balance": 25, "price_points": 10, "price_minutes": 15, "can_buy": True, "why": "", "left_today": 30}
    before = parent.get("/api/device/rules", headers=ADAM).json()["time"]["minutes_left"]

    r = parent.post("/api/device/points/buy", headers=ADAM)
    assert r.status_code == 200 and r.json() == {"minutes": 15, "points": 10, "balance": 15}
    assert sky.points["11"] == 15 and ("POST", "/frames/900/reward_points", {"category_ids": ["11"], "points": -10}) in sky.calls
    rules = parent.get("/api/device/rules", headers=ADAM).json()
    assert rules["time"]["minutes_left"] == before + 15 and rules["points"]["left_today"] == 15

    parent.post("/api/device/points/buy", headers=ADAM)            # 30 of 30 minutes bought
    r = parent.post("/api/device/points/buy", headers=ADAM)
    assert r.status_code == 400 and "all the extra time" in r.json()["detail"]
    assert sky.points["11"] == 5                                      # the refused one cost nothing


def test_not_enough_points_and_settings(parent, sky):
    link(parent)
    parent.get("/api/parent/skylight")
    parent.put("/api/parent/skylight", json={"price_points": 5, "price_minutes": 10, "daily_cap": 60})
    barrett = {"Authorization": "Bearer tok-barrett"}
    assert parent.get("/api/device/rules", headers=barrett).json()["points"]["why"] == "You need 5 points."
    assert parent.post("/api/device/points/buy", headers=barrett).status_code == 400 and sky.points["12"] == 4
    parent.put("/api/parent/skylight", json={"members": {"adam": "11", "barrett": ""}})
    assert parent.get("/api/device/rules", headers=barrett).json()["points"] is None
    parent.delete("/api/parent/skylight")
    assert parent.get("/api/parent/skylight").json()["linked"] is False


def test_balance_shapes():
    assert skylight._rows([{"category_id": 11, "current_point_balance": 3}])[0]["id"] == "11"
    assert skylight._balance_of({"current_point_balance": 7}) == 7 and skylight._balance_of({"x_balance": 2}) == 2


def test_members_saved_as_numbers_still_show_points(parent, sky):
    """Real Skylight puts a numeric id inside the attributes too; a hub that saved those numbers must still match."""
    link(parent)
    db = parent.app.state.db
    skylight.save(db, {**skylight.load(db), "frame_id": 900, "members": {"adam": 11, "barrett": 12}})
    parent.get("/api/parent/skylight")
    assert parent.get("/api/device/rules", headers=ADAM).json()["points"]["balance"] == 25
    assert all(isinstance(p["id"], str) for p in parent.get("/api/parent/skylight").json()["people"])
