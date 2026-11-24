"""Approving a game puts it on the kid's PC by itself, and the parents can see how far each install got."""
from .conftest import ADAM

BARRETT = {"Authorization": "Bearer tok-barrett"}


def add(parent, title="Brawlhalla", ref="291550", kids=("adam",), launcher="steam"):
    return parent.post("/api/parent/games", json={"title": title, "launcher": launcher, "launch_ref": ref, "approved_for": list(kids)}).json()["id"]


def beat(client, games, headers=ADAM, **extra):
    r = client.post("/api/device/heartbeat", headers=headers, json={"games": games, **extra})
    assert r.status_code == 200, r.text
    return r


def pc(parent, kid="adam"):
    return next(p for p in parent.get("/api/parent/installs").json()["pcs"] if p["id"] == kid)


def test_approving_a_game_puts_it_on_that_kids_install_list(parent):
    gid = add(parent)
    assert parent.get("/api/device/rules", headers=ADAM).json()["installs"] == [{"game_id": gid, "retry": 0}]
    assert parent.get("/api/device/rules", headers=BARRETT).json()["installs"] == []


def test_approving_from_the_games_list_and_from_a_request_installs_too(parent, client):
    gid = add(parent, kids=())
    parent.put(f"/api/parent/games/{gid}/kids/barrett", json={"approved": True})
    assert [i["game_id"] for i in parent.get("/api/device/rules", headers=BARRETT).json()["installs"]] == [gid]

    client.post("/api/device/requests", headers=ADAM, json={"kind": "game", "detail": "Portal 2"})
    rid = parent.get("/api/parent/requests").json()[0]["id"]
    other = add(parent, "Portal 2", "620", kids=())
    parent.post(f"/api/parent/requests/{rid}", json={"approve": True, "game_id": other})
    assert other in [i["game_id"] for i in parent.get("/api/device/rules", headers=ADAM).json()["installs"]]
    msg = [c for c in parent.get("/api/device/rules", headers=ADAM).json()["commands"] if c["kind"] == "message"][-1]
    assert "installing on your PC now" in msg["arg"]


def test_older_approvals_are_not_pushed_until_a_parent_says_so(parent, client):
    """Approvals from before this feature have auto_install 0: an upgrade must not start every big download at once."""
    from app.main import get_db
    gid = add(parent)
    get_db().x("UPDATE approvals SET auto_install=0")
    get_db().x("DELETE FROM game_installs")
    assert parent.get("/api/device/rules", headers=ADAM).json()["installs"] == []
    row = pc(parent)["games"][0]
    assert row["plain"]["fix"] == "install" and row["plain"]["label"] == "Not on the PC"
    parent.put(f"/api/parent/installs/adam/{gid}", json={"install": True})
    assert [i["game_id"] for i in parent.get("/api/device/rules", headers=ADAM).json()["installs"]] == [gid]


def test_progress_and_finish_are_shown_in_plain_words(parent, client):
    gid = add(parent)
    assert pc(parent)["games"][0]["plain"]["label"] == "Waiting for the PC"     # never heard from the PC
    beat(client, {str(gid): {"state": "installing", "done_bytes": 3 << 30, "total_bytes": 12 << 30}}, disk_free_gb=420.5)
    p = pc(parent)
    g = p["games"][0]
    assert p["online"] and p["disk_free_gb"] == 420.5
    assert g["plain"]["label"] == "Downloading 25%" and "3.0 GB of 12.0 GB" in g["plain"]["text"]
    assert "on the way" in parent.get("/api/parent/installs").json()["summary"]
    assert parent.get("/api/parent/overview").json()["kids"][0]["installs"] == {"busy": 1, "needs": 0}

    beat(client, {str(gid): {"state": "installed"}})
    assert pc(parent)["games"][0]["plain"]["label"] == "Installed"
    assert parent.get("/api/parent/installs").json()["summary"] == "Everything you approved is installed."
    kinds = [e["kind"] for e in parent.get("/api/parent/kids/adam/activity").json()["events"]]
    assert "game_installed" in kinds


def test_a_store_that_needs_signing_in_says_so(parent, client):
    gid = add(parent)
    beat(client, {str(gid): {"state": "needs_signin", "detail": ""}})
    g = pc(parent)["games"][0]
    assert g["plain"]["fix"] == "signin" and "Steam has to be signed in on Adam's PC once" in g["plain"]["text"]
    assert "need" in parent.get("/api/parent/installs").json()["summary"]


def test_try_again_starts_over_and_ignores_reports_from_before(parent, client):
    gid = add(parent)
    beat(client, {str(gid): {"state": "no_space", "detail": "Only 2 GB free."}})
    assert pc(parent)["games"][0]["plain"]["label"] == "PC is full"
    parent.post(f"/api/parent/installs/adam/{gid}/retry")
    assert parent.get("/api/device/rules", headers=ADAM).json()["installs"] == [{"game_id": gid, "retry": 1}]
    beat(client, {str(gid): {"state": "no_space", "retry": 0}})      # a heartbeat already on its way
    assert pc(parent)["games"][0]["state"] == "queued"
    beat(client, {str(gid): {"state": "installing", "retry": 1}})
    assert pc(parent)["games"][0]["state"] == "installing"


def test_try_again_also_retries_a_launcher_that_failed(parent, client):
    gid = add(parent, "World of Warcraft", "WoW", launcher="battlenet")
    beat(client, {str(gid): {"state": "waiting"}}, apps={"battlenet": {"state": "failed", "detail": "Battle.net would not install by itself."}})
    g = pc(parent)["games"][0]
    assert g["plain"]["label"] == "Needs a hand" and "Battle.net wouldn't install by itself" in g["plain"]["text"]
    parent.post(f"/api/parent/installs/adam/{gid}/retry")
    cmds = parent.get("/api/device/rules", headers=ADAM).json()["commands"]
    assert [c["arg"] for c in cmds if c["kind"] == "install_app"] == ["battlenet"]


def test_unapproving_stops_the_push_and_reports_for_other_games_are_ignored(parent, client):
    gid = add(parent)
    other = add(parent, "Not For Adam", "999999", kids=())
    beat(client, {str(gid): {"state": "installed"}, str(other): {"state": "installed"}})
    parent.put(f"/api/parent/games/{gid}/kids/adam", json={"approved": False})
    assert parent.get("/api/device/rules", headers=ADAM).json()["installs"] == []
    assert pc(parent)["games"] == []


def test_switching_the_push_off_keeps_the_approval(parent):
    gid = add(parent)
    parent.put(f"/api/parent/installs/adam/{gid}", json={"install": False})
    assert parent.get("/api/device/rules", headers=ADAM).json()["installs"] == []
    assert [g["id"] for g in parent.get("/api/device/rules", headers=ADAM).json()["games"]] == [gid]


def test_installs_need_a_parent(client):
    assert client.get("/api/parent/installs").status_code == 401
    assert client.post("/api/parent/installs/adam/1/retry").status_code == 401


def test_world_of_warcraft_forever_is_its_own_game():
    """Parents approve the exact WoW version: Forever is a separate search result with its own code."""
    from app import gamesearch
    assert [g["code"] for g in gamesearch.battlenet_search("wow forever")] == ["WoWF"]
    assert {g["code"] for g in gamesearch.battlenet_search("wow")} >= {"WoW", "WoWF", "WoWC", "WoWCE", "WoWA"}
    assert [g["code"] for g in gamesearch.battlenet_search("burning crusade")] == ["WoWA"]
    assert [g["code"] for g in gamesearch.battlenet_search("season of discovery")] == ["WoWCE"]


def test_status_says_what_the_store_is_doing_how_fast_and_when_it_last_moved(parent, client):
    """Patrick: "on its way" made it hard to tell if Brawlhalla was really downloading."""
    from datetime import timedelta
    from app import installs
    from app.main import get_db
    gid = add(parent)
    beat(client, {str(gid): {"state": "waiting", "stage": "Waiting for Steam",
                             "detail": "Steam was restarted at 10:52 AM to pick it up. It hasn't started downloading yet."}})
    g = pc(parent)["games"][0]
    assert g["plain"]["label"] == "Waiting for Steam" and "restarted at 10:52" in g["plain"]["text"]
    assert g["plain"]["since"].startswith("Last change just now")

    # Two reports a minute apart, 600 MB apart: about 10 MB/s, and the time left from that.
    beat(client, {str(gid): {"state": "installing", "stage": "Downloading", "done_bytes": 1 << 30, "total_bytes": 4 << 30}})
    get_db().x("UPDATE game_installs SET at=?", ((installs.datetime.fromisoformat(get_db().one("SELECT at FROM game_installs")["at"]) - timedelta(seconds=60)).isoformat(),))
    beat(client, {str(gid): {"state": "installing", "stage": "Downloading", "done_bytes": (1 << 30) + (600 << 20), "total_bytes": 4 << 30}})
    t = pc(parent)["games"][0]["plain"]
    assert t["label"].startswith("Downloading ") and t["label"].endswith("%")
    assert "10.0 MB/s" in t["text"] and "min left" in t["text"], t["text"]

    beat(client, {str(gid): {"state": "installing", "stage": "Installing", "done_bytes": 4 << 30, "total_bytes": 4 << 30,
                             "detail": "Downloaded. Steam is unpacking it."}})
    assert pc(parent)["games"][0]["plain"]["label"] == "Installing"


def test_no_progress_since_the_last_check_is_said_plainly(parent, client):
    gid = add(parent)
    for _ in range(2):
        beat(client, {str(gid): {"state": "installing", "stage": "Downloading", "done_bytes": 1 << 30, "total_bytes": 4 << 30}})
    assert "hasn't moved" in pc(parent)["games"][0]["plain"]["text"]
