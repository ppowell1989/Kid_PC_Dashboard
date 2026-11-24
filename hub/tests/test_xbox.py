"""Xbox play time (Microsoft Family Safety) shares the kid's daily budget, and his Xbox is blocked while he is locked."""
from datetime import datetime

import pytest

from app import xbox
from app.xbox import MemberSnap
from tests.conftest import ADAM


@pytest.fixture
def ms(monkeypatch):
    """A fake Microsoft family: Patrick (organizer) and Adam's Xbox account. Each call rotates the refresh token."""
    class Fake:
        members = [MemberSnap("m-patrick", "Patrick Powell", "Admin", 0), MemberSnap("m-adam", "Adam Powell", "User", 0)]
        calls: list = []
        tokens_seen: list = []
        n = 0

    fake = Fake()
    fake.calls, fake.tokens_seen = [], []

    def rotate(token):
        fake.tokens_seen.append(token)
        fake.n += 1
        return f"RT{fake.n}"

    async def finish(link):
        return "RT0", "Microsoft family"

    async def snapshot(token, start, end):
        assert start.hour == 0 and end.day == start.day
        return rotate(token), fake.members

    async def set_block(token, member_id, until):
        fake.calls.append((member_id, until))
        for m in fake.members:
            if m.member_id == member_id:
                m.blocked = until is not None
        return rotate(token)

    monkeypatch.setattr(xbox, "finish_login", finish)
    monkeypatch.setattr(xbox, "snapshot", snapshot)
    monkeypatch.setattr(xbox, "set_block", set_block)
    return fake


LINK = "https://login.live.com/oauth20_desktop.srf?code=M.C123&lc=1033"


def link(parent):
    assert "login.live.com" in parent.post("/api/parent/consoles/xbox/start").json()["login_url"]
    r = parent.post("/api/parent/consoles/xbox/finish", json={"link": LINK})
    assert r.status_code == 200, r.text
    return r.json()


def minutes_left(client):
    return client.get("/api/device/rules", headers=ADAM).json()["time"]["minutes_left"]


def match_adam(parent, enforce=True):
    r = parent.put("/api/parent/consoles/xbox/members/m-adam", json={"kid_id": "adam", "enforce": enforce})
    assert r.status_code == 200, r.text
    return r.json()


def test_wrong_address_is_a_plain_error(parent, ms):
    r = parent.post("/api/parent/consoles/xbox/finish", json={"link": "https://login.live.com/oauth20_authorize.srf?x"})
    assert r.status_code == 400 and "code=" in r.json()["detail"]


def test_link_lists_the_family(parent, ms):
    r = link(parent)["xbox"]
    assert "token" not in r
    assert [m["nickname"] for m in r["members"]] == ["Adam Powell", "Patrick Powell"]   # kids first


def test_refresh_token_is_kept_as_microsoft_rotates_it(parent, ms):
    link(parent)
    parent.post("/api/parent/consoles/sync")
    parent.post("/api/parent/consoles/sync")
    assert ms.tokens_seen == ["RT0", "RT1", "RT2"]     # each call uses the token the previous one returned


def test_xbox_minutes_count_once_matched(parent, client, ms):
    link(parent)
    ms.members[1].minutes, ms.members[1].games = 15, [{"name": "Minecraft", "minutes": 15}]
    parent.post("/api/parent/consoles/sync")
    assert minutes_left(client) == 60
    match_adam(parent, enforce=False)
    assert minutes_left(client) == 45
    now = parent.get("/api/parent/overview").json()["kids"][0]
    assert now["consoles"] == [{"device": "Xbox", "minutes": 15, "games": [{"name": "Minecraft", "minutes": 15}]}]


def test_xbox_blocked_when_time_is_up_and_unblocked_after_extra_time(parent, client, ms):
    link(parent)
    match_adam(parent)
    assert ms.calls == []                               # time left: nothing to do
    ms.members[1].minutes = 60
    parent.post("/api/parent/consoles/sync")
    member, until = ms.calls[-1]
    assert member == "m-adam" and datetime.fromisoformat(until.isoformat()) > datetime.fromisoformat("2026-11-24T12:00:00+00:00")
    assert parent.get("/api/parent/consoles").json()["xbox"]["members"][0]["blocked"] is True

    parent.post("/api/parent/consoles/sync")
    assert len(ms.calls) == 1                           # already blocked: no repeat

    parent.post("/api/parent/kids/adam/grant", json={"minutes": 15})
    parent.post("/api/parent/consoles/sync")
    assert ms.calls[-1] == ("m-adam", None)             # extra time lifts the block


def test_parents_own_block_is_never_lifted(parent, ms):
    link(parent)
    ms.members[1].blocked = True                        # set in Microsoft's app, not by the hub
    match_adam(parent)                                  # he has time left
    assert ms.calls == []


def test_turning_enforcement_off_lifts_the_hubs_block(parent, ms):
    link(parent)
    ms.members[1].minutes = 60
    match_adam(parent)
    assert ms.calls[-1][1] is not None
    match_adam(parent, enforce=False)
    assert ms.calls[-1] == ("m-adam", None)


def test_unlink_lifts_hub_blocks_and_forgets(parent, client, ms):
    link(parent)
    ms.members[1].minutes = 60
    match_adam(parent)
    assert parent.delete("/api/parent/consoles/xbox").status_code == 204
    assert ms.calls[-1] == ("m-adam", None)
    assert parent.get("/api/parent/consoles").json()["xbox"] is None
    assert minutes_left(client) == 60


def test_microsoft_errors_show_on_the_dashboard(parent, ms, monkeypatch):
    link(parent)

    class Unauthorized(Exception):
        pass

    async def broken(token, start, end):
        raise Unauthorized()

    monkeypatch.setattr(xbox, "snapshot", broken)
    assert "Connect the account again" in parent.post("/api/parent/consoles/sync").json()["xbox"]["error"]


def test_block_until_next_play_time_or_morning():
    from zoneinfo import ZoneInfo

    from app.rules import TimeState
    now = datetime(2026, 11, 24, 19, 0, tzinfo=ZoneInfo("America/New_York"))
    st = TimeState(allowance=60, granted=0, used=60, minutes_left=0, minutes_to_bedtime=90, locked=True,
                   lock_reason="time", warn=False, minutes_to_stop=90, next_play="")
    assert xbox.block_until(st, now) == datetime(2026, 11, 25, 6, 0, tzinfo=ZoneInfo("America/New_York"))
    st.next_play = "2026-11-25T15:30:00-05:00"
    assert xbox.block_until(st, now).hour == 15
