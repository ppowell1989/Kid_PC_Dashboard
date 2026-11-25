"""Nintendo Switch play time shares the kid's daily budget, and the hub keeps the Switch's own limit in step."""
import pytest

from app import consoles
from app.consoles import DeviceSnap, PlayerSnap
from tests.conftest import ADAM


@pytest.fixture
def nintendo(monkeypatch):
    """A fake Nintendo account with one Switch and two players. Tests change `fake.devices` to move time on."""
    class Fake:
        devices = [DeviceSnap("sw1", "Living room Switch", "Switch 2", limit=None, players=[
            PlayerSnap("p-adam", "Adam", 0), PlayerSnap("p-barrett", "Barrett", 0)])]
        writes: list = []

    fake = Fake()
    fake.writes = []

    async def start():
        return "https://accounts.nintendo.com/connect/1.0.0/authorize?fake"

    async def finish(link):
        if "session_token_code" not in link:
            raise RuntimeError("bad link")
        return "SESSION-TOKEN", "Patrick"

    async def snapshot(token, tz):
        assert token == "SESSION-TOKEN"
        return fake.devices

    async def set_limit(token, tz, device_id, minutes, stop_game):
        fake.writes.append((device_id, minutes, stop_game))
        for d in fake.devices:
            if d.device_id == device_id:
                d.limit = minutes

    monkeypatch.setattr(consoles, "nintendo_start_login", start)
    monkeypatch.setattr(consoles, "nintendo_finish_login", finish)
    monkeypatch.setattr(consoles, "nintendo_snapshot", snapshot)
    monkeypatch.setattr(consoles, "nintendo_set_limit", set_limit)
    return fake


def link(parent):
    assert parent.post("/api/parent/consoles/nintendo/start").json()["login_url"].startswith("https://")
    r = parent.post("/api/parent/consoles/nintendo/finish", json={"link": "npf54789befb391a838://auth#session_token_code=abc"})
    assert r.status_code == 200, r.text
    return r.json()


def play(fake, adam=0, barrett=0):
    fake.devices[0].players[0].minutes = adam
    fake.devices[0].players[1].minutes = barrett


def minutes_left(client):
    return client.get("/api/device/rules", headers=ADAM).json()["time"]["minutes_left"]


def test_nothing_until_linked(parent, nintendo):
    r = parent.get("/api/parent/consoles").json()
    assert r["nintendo"] is None and r["devices"] == []
    assert parent.post("/api/parent/consoles/sync").json()["nintendo"] is None


def test_bad_link_is_a_plain_error(parent, nintendo):
    parent.post("/api/parent/consoles/nintendo/start")
    r = parent.post("/api/parent/consoles/nintendo/finish", json={"link": "something else entirely"})
    assert r.status_code == 400 and "Nintendo" in r.json()["detail"]


def test_link_lists_the_switch_and_its_players(parent, nintendo):
    r = link(parent)
    assert r["nintendo"]["label"] == "Patrick" and "token" not in r["nintendo"]
    (d,) = r["devices"]
    assert d["name"] == "Living room Switch" and [p["nickname"] for p in d["players"]] == ["Adam", "Barrett"]


def test_switch_minutes_count_once_a_player_is_matched(parent, client, nintendo):
    link(parent)
    play(nintendo, adam=20, barrett=15)
    parent.post("/api/parent/consoles/sync")
    assert minutes_left(client) == 60                   # nobody matched yet: Switch play isn't counted
    parent.put("/api/parent/consoles/nintendo/devices/sw1/players/p-adam", json={"kid_id": "adam"})
    assert minutes_left(client) == 40                   # 60-minute day, 20 on the Switch
    play(nintendo, adam=25, barrett=15)                 # Nintendo's running total replaces the old figure
    parent.post("/api/parent/consoles/sync")
    assert minutes_left(client) == 35
    now = parent.get("/api/parent/overview").json()["kids"][0]
    assert now["time"]["used"] == 25 and now["consoles"][0]["minutes"] == 25


def test_switch_limit_follows_the_kids_time_left(parent, client, nintendo):
    link(parent)
    parent.put("/api/parent/consoles/nintendo/devices/sw1/players/p-adam", json={"kid_id": "adam"})
    play(nintendo, adam=10)
    parent.put("/api/parent/consoles/nintendo/devices/sw1", json={"limit_kid": "adam", "stop_game": True})
    assert nintendo.writes[-1] == ("sw1", 60, True)     # 10 played + 50 left

    # 20 minutes on the PC: the Switch now allows 30 more.
    client.post("/api/device/heartbeat", headers=ADAM, json={"active": False, "offline_minutes": 20})
    parent.post("/api/parent/consoles/sync")
    assert nintendo.writes[-1] == ("sw1", 40, True)

    # Small changes aren't written (no rewrite every minute while he plays on the PC).
    n = len(nintendo.writes)
    client.post("/api/device/heartbeat", headers=ADAM, json={"active": False, "offline_minutes": 2})
    parent.post("/api/parent/consoles/sync")
    assert len(nintendo.writes) == n

    # A parent's extra time raises the Switch's limit too.
    parent.post("/api/parent/kids/adam/grant", json={"minutes": 30})
    parent.post("/api/parent/consoles/sync")
    assert nintendo.writes[-1] == ("sw1", 68, True)     # 10 + (60 + 30 - 32)


def test_time_up_stops_the_switch_now(parent, client, nintendo):
    link(parent)
    parent.put("/api/parent/consoles/nintendo/devices/sw1/players/p-adam", json={"kid_id": "adam"})
    play(nintendo, adam=15)
    parent.put("/api/parent/consoles/nintendo/devices/sw1", json={"limit_kid": "adam"})
    parent.post("/api/parent/kids/adam/pause", json={"minutes": 30})
    parent.post("/api/parent/consoles/sync")
    assert nintendo.writes[-1] == ("sw1", 15, True)     # limit = what he already played: the game stops


def test_bonus_time_from_nintendos_app_is_left_alone(parent, nintendo):
    link(parent)
    nintendo.devices[0].extra_time_active = True
    parent.put("/api/parent/consoles/nintendo/devices/sw1", json={"limit_kid": "adam"})
    assert nintendo.writes == []


def test_nintendo_errors_show_on_the_dashboard(parent, nintendo, monkeypatch):
    link(parent)

    async def broken(token, tz):
        raise RuntimeError("HTTP 401 invalid_grant")

    monkeypatch.setattr(consoles, "nintendo_snapshot", broken)
    r = parent.post("/api/parent/consoles/sync").json()
    assert "Connect the account again" in r["nintendo"]["error"]


def test_unlink_forgets_everything(parent, client, nintendo):
    link(parent)
    parent.put("/api/parent/consoles/nintendo/devices/sw1/players/p-adam", json={"kid_id": "adam"})
    play(nintendo, adam=20)
    parent.post("/api/parent/consoles/sync")
    assert parent.delete("/api/parent/consoles/nintendo").status_code == 204
    assert minutes_left(client) == 60
    assert parent.get("/api/parent/consoles").json()["nintendo"] is None


def test_target_limit_rules():
    from app.rules import TimeState
    st = TimeState(allowance=60, granted=0, used=20, minutes_left=40, minutes_to_bedtime=300, locked=False,
                   lock_reason="", warn=False, minutes_to_stop=300, next_play="")
    assert consoles.target_limit(20, st) == 60
    assert consoles.target_limit(350, st) == 360        # the Switch's maximum
    st.locked = True
    assert consoles.target_limit(20, st) == 20
    assert consoles.needs_write(None, 30, 0)
    assert not consoles.needs_write(32, 30, 0)
    assert consoles.needs_write(25, 20, 20)             # time's up must be exact


# The real sign-in steps, with Nintendo's server faked -----------------------------------------------------------

class FakeAuth:
    made: list = []

    def __init__(self, *a, **k):
        import secrets as s
        self.state = s.token_hex(8)
        self.login_url = f"https://accounts.nintendo.com/connect/1.0.0/authorize?client_id=x&state={self.state}"
        self.session_token, self.account, self.got = None, {"nickname": "havitz"}, None
        self.closed = False
        FakeAuth.made.append(self)

        class S:
            async def close(_):
                self.closed = True
        self.client_session = S()

    async def async_complete_login(self, link):
        self.got = link
        if "bad" in link:
            raise RuntimeError("HTTP 400 invalid_grant")
        self.session_token = f"TOKEN-{self.state}"


def nlink(state, code="eyJcode", amp="&"):
    return f"npf54789befb391a838://auth#session_token_code={code}{amp}state={state}{amp}session_state=abc"


@pytest.fixture
def fake_auth(monkeypatch):
    import asyncio

    import pynintendoparental
    FakeAuth.made = []
    consoles._pending_login.clear()
    monkeypatch.setattr(pynintendoparental, "Authenticator", FakeAuth)
    return asyncio.run


def test_link_from_an_earlier_connect_tap_still_works(fake_auth):
    run = fake_auth
    run(consoles.nintendo_start_login())
    run(consoles.nintendo_start_login())          # Connect tapped twice; he finishes in the first page
    first = FakeAuth.made[0]
    token, label = run(consoles.nintendo_finish_login(nlink(first.state)))
    assert token == f"TOKEN-{first.state}" and label == "havitz" and first.closed


def test_html_escaped_link_is_cleaned(fake_auth):
    run = fake_auth
    run(consoles.nintendo_start_login())
    a = FakeAuth.made[0]
    run(consoles.nintendo_finish_login("  " + nlink(a.state, amp="&amp;") + " "))
    assert "&amp;" not in a.got and f"state={a.state}" in a.got


def test_wrong_text_and_old_links_get_plain_answers(fake_auth):
    run = fake_auth
    with pytest.raises(consoles.LoginRefused, match="Copy Link Address"):
        run(consoles.nintendo_finish_login("https://accounts.nintendo.com/whatever"))
    run(consoles.nintendo_start_login())
    run(consoles.nintendo_start_login())
    with pytest.raises(consoles.LoginExpired, match="older Connect tap"):
        run(consoles.nintendo_finish_login(nlink("not-a-state-we-made")))
    with pytest.raises(consoles.LoginRefused, match="expired or was already used"):
        run(consoles.nintendo_finish_login(nlink(FakeAuth.made[0].state, code="bad")))
