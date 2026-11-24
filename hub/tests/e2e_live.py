"""End-to-end walk through the whole hub over real HTTP, as a kid PC, a Chrome extension, both parents and an
invited friend would use it. Runs against a hub that is already up (a local uvicorn, or a test hub):

    python tests/e2e_live.py http://127.0.0.1:8099 --parent patrick:pw --adam tok-adam --barrett tok-barrett \
        --ext ext-adam
    python tests/e2e_live.py local      # starts its own throwaway hub on a free port (what CI runs)

It changes data (games, invites, chat, grants), so never point it at the family's real hub. Each step prints
PASS or FAIL and the script exits 1 if any failed.
"""
import argparse
import base64
import io
import sys
from datetime import datetime, timezone

import httpx

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    return bool(ok)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def png() -> str:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 30, 30)).save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


def local_hub():
    """A throwaway hub (temp config and database) on a free port, as uvicorn in a child process."""
    import os, socket, subprocess, tempfile, time
    import yaml
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tests.conftest import CONFIG
    d = tempfile.mkdtemp()
    # No bedtime, so the walk-through passes at any hour of the day it runs.
    kids = [dict(k, bedtime={"weekday": "", "weekend": ""}, daily_minutes={"weekday": 120, "weekend": 120}) for k in CONFIG["kids"]]
    cfg = dict(CONFIG, kids=kids)
    with open(os.path.join(d, "config.yaml"), "w") as f:
        yaml.safe_dump(cfg, f)
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    env = dict(os.environ, HUB_CONFIG=os.path.join(d, "config.yaml"), HUB_DB=os.path.join(d, "hub.db"))
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"], env=env,
                            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                return url, proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.kill()
    raise SystemExit("local hub did not start")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hub")
    ap.add_argument("--parent", default="patrick:pw")
    ap.add_argument("--adam", default="tok-adam")
    ap.add_argument("--barrett", default="tok-barrett")
    ap.add_argument("--ext", default="ext-adam")
    a = ap.parse_args()
    if a.hub == "local":
        url, proc = local_hub()
        try:
            a.hub = url
            return run(a)
        finally:
            proc.terminate()
    return run(a)


def run(a) -> int:
    hub = a.hub.rstrip("/")
    name, pw = a.parent.split(":", 1)
    P = httpx.Client(base_url=hub, timeout=20)
    A = httpx.Client(base_url=hub, timeout=20, headers={"Authorization": f"Bearer {a.adam}"})
    B = httpx.Client(base_url=hub, timeout=20, headers={"Authorization": f"Bearer {a.barrett}"})
    X = httpx.Client(base_url=hub, timeout=20, headers={"Authorization": f"Bearer {a.ext}"})
    F = httpx.Client(base_url=hub, timeout=20)

    # Basics ---------------------------------------------------------------------------------------
    r = F.get("/healthz")
    check("hub answers /healthz", r.status_code == 200 and r.json().get("ok"), r.text)
    r = F.get("/")
    check("dashboard page loads", r.status_code == 200 and "Kids PC" in r.text)
    for asset in ("/static/app.js", "/static/app.css"):
        check(f"{asset} served", F.get(asset).status_code == 200)
    check("device API refuses no token", F.get("/api/device/rules").status_code == 401)
    check("parent API refuses no sign-in", F.get("/api/parent/overview").status_code == 401)
    check("wrong parent password refused", P.post("/api/parent/login", json={"name": name, "password": pw + "x"}).status_code == 401)
    r = P.post("/api/parent/login", json={"name": name, "password": pw})
    if not check("parent signs in", r.status_code == 200, r.text):
        return report()
    r = P.get("/api/parent/overview")
    check("overview lists both kids", r.status_code == 200 and {k["id"] for k in r.json()["kids"]} >= {"adam", "barrett"}, r.text[:300])

    # Kid PC starts up -----------------------------------------------------------------------------
    r = A.get("/api/device/rules")
    ok = check("Adam's PC pulls its rules", r.status_code == 200, r.text[:200])
    rules = r.json() if ok else {}
    check("rules carry time, web, timezone, family", all(k in rules for k in ("time", "web", "timezone", "family", "installs")))
    check("rules carry a DNS filter", bool(rules.get("web", {}).get("dns")))
    r = A.post("/api/device/heartbeat", json={"running": "", "active": False, "apps": {"steam": {"state": "installed"}, "chrome": {"state": "installed"}},
                                             "disk_free_gb": 120.5, "games": {}})
    check("heartbeat accepted", r.status_code == 200 and "time" in r.json(), r.text[:200])
    r = B.post("/api/device/heartbeat", json={"running": "", "active": False})
    check("Barrett's heartbeat accepted", r.status_code == 200)

    # Games: request -> approve = install --------------------------------------------------------------
    r = P.post("/api/parent/games", json={"title": "Brawlhalla", "launcher": "steam", "launch_ref": "291550", "approved_for": []})
    check("parent adds a Steam game to the catalogue", r.status_code == 201, r.text)
    brawl = r.json().get("id")
    r = A.post("/api/device/requests", json={"kind": "game", "detail": "Brawlhalla", "ref": "steam:291550"})
    check("kid asks for a game", r.status_code == 201, r.text)
    rid = r.json().get("id")
    r = A.post("/api/device/requests", json={"kind": "game", "detail": "Brawlhalla", "ref": "steam:291550"})
    check("asking twice doesn't make a second request", r.json().get("duplicate") is True)
    reqs = P.get("/api/parent/requests").json()
    check("parent sees the request", any(q["id"] == rid for q in reqs), str(reqs)[:300])
    r = P.post(f"/api/parent/requests/{rid}", json={"approve": True, "game_id": brawl})
    check("parent approves it", r.status_code == 200, r.text)
    rules = A.get("/api/device/rules").json()
    check("game is on Adam's PC list", any(g["id"] == brawl for g in rules["games"]))
    check("approved game is pushed for install", any(i["game_id"] == brawl for i in rules["installs"]), str(rules["installs"]))
    check("Barrett does not get Adam's game", not any(g["id"] == brawl for g in B.get("/api/device/rules").json()["games"]))
    for stage, state, done in (("Waiting for Steam", "waiting", 0), ("Downloading", "installing", 200_000_000), ("", "installed", 400_000_000)):
        r = A.post("/api/device/heartbeat", json={"games": {str(brawl): {"state": state, "stage": stage, "done_bytes": done, "total_bytes": 400_000_000}}})
        if r.status_code != 200:
            break
    check("install progress reports accepted", r.status_code == 200, r.text[:300])
    inst = P.get("/api/parent/installs")
    check("parent PCs page shows the install", inst.status_code == 200 and "Brawlhalla" in inst.text, inst.text[:300])
    acts = P.get("/api/parent/kids/adam/activity").json()
    check("activity says the game was installed", any(e.get("kind") == "game_installed" for e in acts.get("events", acts) if isinstance(e, dict)), str(acts)[:300])

    # Battle.net: WoW Forever gets Battle.net managed ------------------------------------------------------
    r = P.post("/api/parent/games", json={"title": "", "launcher": "battlenet", "launch_ref": "WoWF", "approved_for": ["adam"]})
    check("parent adds WoW Forever for Adam", r.status_code == 201, r.text)
    rules = A.get("/api/device/rules").json()
    check("Battle.net becomes a managed app", "battlenet" in rules["managed_apps"], str(rules["managed_apps"]))

    # Time: warning, lock, ask for more, grant, offline ---------------------------------------------------
    r = P.put("/api/parent/playtimes/adam", json={"days": [0, 1, 2, 3, 4, 5, 6], "daily_minutes": 15, "bedtime": "", "windows": []})
    check("parent sets a 15-minute day", r.status_code == 200, r.text[:200])
    r = A.post("/api/device/heartbeat", json={"running": "Brawlhalla", "active": True, "game_id": brawl, "offline_minutes": 6})
    t = r.json()["time"]
    check("offline minutes count against the day", t["used"] >= 6, str(t))
    check("ten-minute warning shows", t["warn"] and not t["locked"], str(t))
    ov = P.get("/api/parent/overview").json()
    adam = next(k for k in ov["kids"] if k["id"] == "adam")
    check("parent sees what Adam is playing", "Brawlhalla" in str(adam), str(adam)[:300])
    r = A.post("/api/device/heartbeat", json={"running": "Brawlhalla", "active": True, "offline_minutes": 10})
    t = r.json()["time"]
    check("out of time locks the PC", t["locked"] and t["lock_reason"] == "time", str(t))
    r = A.get("/api/device/voice-token")
    check("voice refused while locked", r.status_code in (403, 423), r.text[:200])
    r = A.post("/api/device/requests", json={"kind": "time", "detail": "20", "note": "finishing a match"})
    check("kid asks for 20 more minutes", r.status_code == 201, r.text)
    tid = r.json()["id"]
    r = P.post(f"/api/parent/requests/{tid}", json={"approve": True, "minutes": 15})
    check("parent grants 15", r.status_code == 200, r.text)
    t = A.get("/api/device/rules").json()["time"]
    check("PC unlocks with the extra time", not t["locked"] and t["minutes_left"] > 0, str(t))
    r = P.post("/api/parent/kids/adam/pause", json={"minutes": 0})
    check("pause", r.status_code == 200 and A.get("/api/device/rules").json()["time"]["lock_reason"] == "paused", r.text)
    r = P.delete("/api/parent/kids/adam/pause")
    check("resume", r.status_code == 200 and not A.get("/api/device/rules").json()["time"]["locked"])
    r = P.post("/api/parent/kids/adam/grant", json={"minutes": 30})
    check("parent adds time directly", r.status_code == 200)

    # Remote commands (message, restart, install now) ----------------------------------------------------
    r = P.post("/api/parent/kids/adam/commands", json={"kind": "message", "arg": "Dinner in 5"})
    check("parent sends a message to the PC", r.status_code in (200, 201), r.text)
    cmds = A.get("/api/device/rules").json()["commands"]
    check("PC receives the command", any(c["kind"] == "message" for c in cmds), str(cmds))
    if cmds:
        r = A.post(f"/api/device/commands/{cmds[0]['id']}", json={"ok": True, "result": "shown"})
        check("PC reports the command done", r.status_code == 200)

    # Web: extension visits, helper history, flagged searches, site request --------------------------------
    r = X.post("/api/device/visits", json=[{"url": "https://www.wikipedia.org/wiki/Cat", "title": "Cat", "at": now()}])
    check("Chrome extension reports a visit", r.status_code == 204, r.text)
    check("extension token can't read rules", X.get("/api/device/rules").status_code == 401)
    r = A.post("/api/device/history", json={"visits": [{"url": "https://khanacademy.org/math", "title": "Math", "at": now()}],
                                           "searches": [{"term": "minecraft seeds", "at": now()}, {"term": "sexy pictures", "at": now()}]})
    check("helper uploads Chrome history", r.status_code == 204, r.text)
    v = P.get("/api/parent/visits/adam").json()
    check("parent sees visited sites", "wikipedia.org" in str(v) and "khanacademy.org" in str(v), str(v)[:300])
    s = P.get("/api/parent/searches/adam").json()
    check("bad search is flagged", any(x.get("flagged") for x in (s if isinstance(s, list) else s.get("searches", []))), str(s)[:300])
    r = A.post("/api/device/requests", json={"kind": "site", "detail": "scratch.mit.edu"})
    sid = r.json()["id"]
    P.post(f"/api/parent/requests/{sid}", json={"approve": True})
    check("approved site joins the allow list", "scratch.mit.edu" in A.get("/api/device/rules").json()["web"]["allowed_sites"])

    # Family: see each other, family chat, family voice -----------------------------------------------------
    fam = B.get("/api/device/rules").json()["family"]
    check("Barrett sees Adam in the family list", "adam" in str(fam).lower(), str(fam)[:300])
    r = A.post("/api/device/chat", json={"text": "want to play?", "conv": "kids"})
    check("Adam texts Barrett", r.status_code in (200, 201), r.text)
    r = B.get("/api/device/chat", params={"conv": "kids"})
    check("Barrett reads it", "want to play?" in r.text, r.text[:300])
    r = A.post("/api/device/chat/picture", json={"data": png(), "text": "my base", "conv": "kids"})
    check("Adam sends a screenshot", r.status_code in (200, 201), r.text[:200])
    r = B.get("/api/device/family-voice-token")
    check("family voice token", r.status_code == 200 and r.json().get("token"), r.text[:200])

    # Friends: invite link, voice join, friend chat, revoke -------------------------------------------------
    r = P.post("/api/parent/invites", json={"kid_id": "adam", "friend_name": "Sam"})
    check("parent makes an invite link", r.status_code == 201, r.text)
    code = r.json()["code"]
    check("friend opens the join page", F.get(f"/join/{code}").status_code == 200)
    r = F.post(f"/api/voice/join/{code}")
    check("friend gets a voice token", r.status_code == 200 and r.json().get("token"), r.text[:200])
    r = F.post(f"/join/{code}/chat", json={"text": "hi Adam, look https://example.com"})
    check("friend sends a chat message with a link", r.status_code == 201, r.text)
    convs = A.get("/api/device/chats").json()
    fconv = next((c["id"] for c in (convs if isinstance(convs, list) else convs.get("convs", [])) if str(c.get("id", "")).startswith("friend-")), None)
    check("Adam has the friend conversation", fconv is not None, str(convs)[:300])
    if fconv:
        check("Adam reads Sam's message", "hi Adam" in A.get("/api/device/chat", params={"conv": fconv}).text)
    pc = P.get("/api/parent/chats")
    check("parent can read every conversation", pc.status_code == 200 and "friend-" in pc.text, pc.text[:300])
    check("parent sees shared pictures and links", "example.com" in P.get("/api/parent/chat-shared").text)
    r = P.delete(f"/api/parent/invites/{code}")
    check("parent revokes the link", r.status_code == 200)
    check("revoked link stops the join page", F.get(f"/join/{code}").status_code == 404)
    check("revoked link stops friend chat", F.post(f"/join/{code}/chat", json={"text": "still here?"}).status_code in (403, 404))

    # Retention + history clearing -------------------------------------------------------------------------
    r = P.get("/api/parent/retention")
    check("retention settings readable", r.status_code == 200, r.text[:200])
    r = P.put("/api/parent/retention", json=r.json())
    check("retention settings save", r.status_code == 200, r.text[:200])

    # New PC setup -------------------------------------------------------------------------------------------
    r = P.post("/api/parent/kids/barrett/setup-code")
    check("setup code for a new PC", r.status_code == 201 and "irm" in r.json().get("command", ""), r.text)
    sc = r.json().get("code", "")
    s = F.get(f"/setup/{sc}")
    check("setup script served with the code baked in", s.status_code == 200 and sc in s.text and "__HUB__" not in s.text)
    s = F.get("/setup/NOTACODE")
    check("a bad code gives a plain message", "not valid" in s.text)

    # Consoles (no accounts linked: must say so, not crash) ---------------------------------------------------
    r = P.get("/api/parent/consoles")
    check("consoles page answers without accounts", r.status_code in (200, 404), r.text[:200])

    # Updates --------------------------------------------------------------------------------------------------
    r = A.get("/api/device/update", params={"arch": "arm64"})
    check("update check answers (build or 'none yet')", r.status_code in (200, 404), r.text[:200])
    r = P.put("/api/parent/kids/adam/auto-update", json={"on": True})
    check("right-away updates switch", r.status_code == 200 and A.get("/api/device/rules").json()["auto_update"] is True, r.text)

    # Sign out -------------------------------------------------------------------------------------------------
    P.post("/api/parent/logout")
    check("signed-out parent is refused", P.get("/api/parent/overview").status_code == 401)
    return report()


def report() -> int:
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)} of {len(RESULTS)} passed")
    for n, _, d in bad:
        print(f"  FAIL {n}: {d}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
