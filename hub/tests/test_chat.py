"""Text chat: the kids' own chat, friend chat through the invite link, pictures, links, and what parents see."""
import base64
import io

from PIL import Image

from tests.conftest import ADAM

BARRETT = {"Authorization": "Bearer tok-barrett"}


def picture(fmt="PNG", size=(40, 30), exif=False) -> str:
    im = Image.new("RGB", size, "red")
    buf = io.BytesIO()
    kw = {}
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "PhoneMaker"   # Make
        ex[0x8825] = {2: (40.0, 26.0, 0.0)}   # GPS latitude
        kw["exif"] = ex
    im.save(buf, fmt, **kw)
    return base64.b64encode(buf.getvalue()).decode()


def invite(parent, kid="adam", name="Max") -> str:
    return parent.post("/api/parent/invites", json={"kid_id": kid, "friend_name": name}).json()["code"]


def test_kids_chat_between_brothers(parent):
    convs = parent.get("/api/device/chats", headers=ADAM).json()["convs"]
    assert [(c["id"], c["title"]) for c in convs] == [("family", "Family"), ("kids", "Barrett")]
    assert parent.post("/api/device/chat", headers=ADAM, json={"conv": "kids", "text": "wanna play? www.minecraft.net"}).status_code == 201
    got = parent.get("/api/device/chat?conv=kids", headers=BARRETT).json()
    assert got[0]["sender"] == "Adam" and not got[0]["mine"] and got[0]["links"] == ["www.minecraft.net"]
    # Not in the family chat, and parents can't post into the brothers' chat but can read it.
    assert parent.get("/api/parent/chat").json() == []
    assert parent.post("/api/parent/chat", json={"conv": "kids", "text": "hi"}).status_code == 403
    assert [m["text"] for m in parent.get("/api/parent/chats/kids").json()] == ["wanna play? www.minecraft.net"]
    unread = {c["id"]: c for c in parent.get("/api/device/chats", headers=BARRETT).json()["convs"]}
    assert unread["kids"]["last_other_id"] == got[0]["id"]
    # Parents can switch it off.
    parent.put("/api/parent/chat-settings", json={"kids": False})
    assert parent.get("/api/device/chat?conv=kids", headers=ADAM).status_code == 404
    assert [c["id"] for c in parent.get("/api/device/chats", headers=ADAM).json()["convs"]] == ["family"]


def test_friend_chat_through_the_invite_link(parent):
    code = invite(parent)
    convs = parent.get("/api/device/chats", headers=ADAM).json()["convs"]
    friend = next(c for c in convs if c["kind"] == "friend")
    assert friend["title"] == "Max" and code not in friend["id"]
    # The PC's rules carry the chat id, not the invite link.
    assert code not in parent.get("/api/device/rules", headers=ADAM).text
    # Barrett doesn't get Adam's friend.
    assert all(c["kind"] != "friend" for c in parent.get("/api/device/chats", headers=BARRETT).json()["convs"])
    assert parent.get(f"/api/device/chat?conv={friend['id']}", headers=BARRETT).status_code == 404

    assert parent.post(f"/join/{code}/chat", json={"text": "hey Adam"}).status_code == 201
    assert parent.post("/api/device/chat", headers=ADAM, json={"conv": friend["id"], "text": "hi Max"}).status_code == 201
    page = parent.get(f"/join/{code}/chat").json()
    assert page["on"] and page["kid_name"] == "Adam"
    assert [(m["sender"], m["mine"]) for m in page["messages"]] == [("Max", True), ("Adam", False)]
    kid = parent.get(f"/api/device/chat?conv={friend['id']}", headers=ADAM).json()
    assert kid[0]["friend"] and kid[0]["kid_id"] is None and kid[1]["mine"]

    chats = {c["id"]: c for c in parent.get("/api/parent/chats").json()["convs"]}
    assert chats[friend["id"]]["title"] == "Adam & Max" and chats[friend["id"]]["count"] == 2

    # Revoking the link ends the chat both ways; the parents keep the history.
    parent.delete(f"/api/parent/invites/{code}")
    assert parent.get(f"/join/{code}/chat").status_code == 404
    assert parent.post(f"/join/{code}/chat", json={"text": "still here?"}).status_code == 404
    assert parent.get(f"/api/device/chat?conv={friend['id']}", headers=ADAM).status_code == 404
    assert len(parent.get(f"/api/parent/chats/{friend['id']}").json()) == 2


def test_pictures_are_cleaned_and_shown_to_the_right_people(parent):
    from app.main import app
    code = invite(parent)
    conv = next(c["id"] for c in parent.get("/api/device/chats", headers=ADAM).json()["convs"] if c["kind"] == "friend")
    # A phone photo from the friend: the hub keeps the picture but not the camera or GPS data.
    r = parent.post(f"/join/{code}/chat/picture", json={"data": "data:image/jpeg;base64," + picture("JPEG", exif=True)})
    assert r.status_code == 201
    mid = r.json()["id"]
    row = app.state.db.one("SELECT picture FROM chat WHERE id=?", (mid,))
    saved = Image.open(app.state.chat_dir / row["picture"])
    assert not saved.getexif()
    # A big screenshot from Adam is shrunk and stays PNG.
    r = parent.post("/api/device/chat/picture", headers=ADAM, json={"conv": conv, "data": picture(size=(3000, 1500)), "text": "look"})
    assert r.status_code == 201
    shot = r.json()["id"]
    assert Image.open(io.BytesIO(parent.get(f"/api/device/chat/picture/{shot}", headers=ADAM).content)).size == (1600, 800)

    assert parent.get(f"/join/{code}/chat/picture/{shot}").status_code == 200
    assert parent.get(f"/api/parent/chat/picture/{mid}").status_code == 200
    assert parent.get(f"/api/device/chat/picture/{mid}", headers=BARRETT).status_code == 404   # not his chat
    assert [m["id"] for m in parent.get("/api/parent/chat-shared").json()] == [shot, mid]

    # Not a picture, or pictures switched off.
    bad = base64.b64encode(b"MZ\x90\x00 not an image").decode()
    assert parent.post(f"/join/{code}/chat/picture", json={"data": bad}).status_code == 422
    parent.put("/api/parent/chat-settings", json={"pictures": False})
    assert parent.post(f"/join/{code}/chat/picture", json={"data": picture()}).status_code == 403


def test_parent_removes_a_message_and_flag_words_are_marked(parent):
    parent.post("/api/device/chat", headers=ADAM, json={"conv": "kids", "text": "send nudes lol"})
    m = parent.get("/api/parent/chats/kids").json()[0]
    assert m["flagged"]
    assert parent.get("/api/parent/chats").json()["convs"][1]["flagged"] == 1
    assert parent.delete(f"/api/parent/chat/{m['id']}").status_code == 200
    kid = parent.get("/api/device/chat?conv=kids", headers=BARRETT).json()[0]
    assert kid["removed"] and kid["text"] == "A parent removed this message."
    assert parent.get("/api/parent/chats/kids").json()[0]["text"] == "send nudes lol"


def test_only_family_chat_while_locked(parent, monkeypatch):
    from app import chat
    monkeypatch.setattr(chat, "_locked", lambda db, s, k: True)
    convs = {c["id"]: c for c in parent.get("/api/device/chats", headers=ADAM).json()["convs"]}
    assert convs["family"]["can_send"] and not convs["kids"]["can_send"]
    assert parent.post("/api/device/chat", headers=ADAM, json={"conv": "kids", "text": "hi"}).status_code == 403
    assert parent.post("/api/device/chat", headers=ADAM, json={"text": "can I have 10 more minutes"}).status_code == 201


def test_links_can_be_switched_off_and_friends_are_rate_limited(parent):
    code = invite(parent)
    parent.put("/api/parent/chat-settings", json={"links": False})
    assert parent.post(f"/join/{code}/chat", json={"text": "go to https://example.com"}).status_code == 403
    assert parent.post("/api/parent/chat", json={"text": "https://example.com is fine"}).status_code == 201   # parents always can
    codes = [parent.post(f"/join/{code}/chat", json={"text": f"spam {i}"}).status_code for i in range(32)]
    assert codes.count(201) == 29 and codes[-1] == 429   # the refused link counted as one of the 30


def test_old_pictures_are_deleted_with_their_messages(parent):
    from app import retention
    from app.main import app
    db = app.state.db
    r = parent.post("/api/device/chat/picture", headers=ADAM, json={"conv": "kids", "data": picture()})
    name = db.one("SELECT picture FROM chat WHERE id=?", (r.json()["id"],))["picture"]
    assert (app.state.chat_dir / name).exists()
    db.x("UPDATE chat SET at='2026-01-01T10:00:00'")
    retention.prune(db, __import__("datetime").datetime(2026, 11, 24, tzinfo=__import__("datetime").timezone.utc))
    assert not (app.state.chat_dir / name).exists()


def test_iphone_photos_and_other_formats_are_taken(parent):
    from pillow_heif import register_heif_opener
    from app.main import app
    register_heif_opener()
    code = invite(parent)
    for fmt in ("HEIF", "MPO", "BMP", "TIFF", "WEBP"):
        im = Image.new("RGB", (2000, 1500), "blue")
        buf = io.BytesIO()
        im.save(buf, fmt, **({"quality": 60} if fmt == "HEIF" else {}))
        r = parent.post(f"/join/{code}/chat/picture", json={"data": base64.b64encode(buf.getvalue()).decode()})
        assert r.status_code == 201, (fmt, r.text)
        name = app.state.db.one("SELECT picture FROM chat WHERE id=?", (r.json()["id"],))["picture"]
        assert name.endswith(".jpg") and Image.open(app.state.chat_dir / name).size == (1600, 1200)


def test_a_video_gets_a_plain_answer(parent):
    code = invite(parent)
    mov = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  " + b"\x00" * 200
    r = parent.post(f"/join/{code}/chat/picture", json={"data": base64.b64encode(mov).decode()})
    assert r.status_code == 422 and "video" in r.json()["detail"]


def clip(seconds: float, size="640x360") -> bytes:
    import subprocess
    import tempfile
    out = tempfile.mktemp(suffix=".mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=15:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=duration={seconds}", "-metadata", "location=+40.7128-074.0060/",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out], check=True)
    return open(out, "rb").read()


def test_short_videos_are_converted_and_linked(parent):
    import subprocess
    from app.main import app
    code = invite(parent)
    conv = next(c["id"] for c in parent.get("/api/device/chats", headers=ADAM).json()["convs"] if c["kind"] == "friend")
    r = parent.post(f"/join/{code}/chat/video?text=watch this", content=clip(3, "1920x1080"))
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    row = app.state.db.one("SELECT picture, video FROM chat WHERE id=?", (mid,))
    path = app.state.chat_dir / row["video"]
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format_tags", "-of", "json", str(path)],
                           capture_output=True, text=True).stdout
    assert '"width": 1280' in probe and "location" not in probe   # 720p copy, no location
    assert row["picture"].endswith(".jpg")

    kid = parent.get(f"/api/device/chat?conv={conv}", headers=ADAM).json()[0]
    assert kid["video"] and kid["picture"] and kid["text"] == "watch this"
    # The file is reached through a signed link; the link alone works (from outside too), a made-up one doesn't.
    link = parent.get(f"/api/device/chat/video/{mid}", headers=ADAM, follow_redirects=False)
    assert link.status_code == 307 and link.headers["location"].startswith(f"/media/{row['video']}?exp=")
    assert parent.get(link.headers["location"]).content[4:8] == b"ftyp"
    assert parent.get(f"/media/{row['video']}?exp=9999999999&sig=0000").status_code == 404
    assert parent.get(f"/api/device/chat/video/{mid}", headers=BARRETT).status_code == 404
    assert parent.get(f"/api/parent/chat/video/{mid}").status_code == 200
    assert parent.get(f"/join/{code}/chat/video/{mid}").status_code == 200
    # Removed: the kid can't fetch it any more; parents still can.
    parent.delete(f"/api/parent/chat/{mid}")
    assert parent.get(f"/api/device/chat/video/{mid}", headers=ADAM).status_code == 404
    assert parent.get(f"/api/parent/chat/video/{mid}").status_code == 200


def test_long_or_fake_videos_are_refused(parent):
    code = invite(parent)
    r = parent.post(f"/join/{code}/chat/video", content=clip(32))
    assert r.status_code == 422 and "up to 30 seconds" in r.json()["detail"]
    assert parent.post(f"/join/{code}/chat/video", content=b"not a video at all").status_code == 422
    parent.put("/api/parent/chat-settings", json={"videos": False})
    assert parent.post(f"/join/{code}/chat/video", content=clip(1)).status_code == 403


def test_picture_check_holds_a_flagged_picture_until_a_parent_lets_it_through(parent, monkeypatch):
    """Patrick's choice: everything arrives straight away except what the local picture check flags."""
    from app import screening
    monkeypatch.setattr(screening, "check_file", lambda path: "The picture check found what looks like nudity")
    code = invite(parent)
    conv = next(c["id"] for c in parent.get("/api/device/chats", headers=ADAM).json()["convs"] if c["kind"] == "friend")
    mid = parent.post(f"/join/{code}/chat/picture", json={"data": picture("JPEG"), "text": "lol"}).json()["id"]
    mine = parent.get(f"/api/device/chat?conv={conv}", headers=ADAM).json()
    m = next(x for x in (mine if isinstance(mine, list) else mine["messages"]) if x["id"] == mid)
    assert m["text"] == screening_text() and not m["picture"]
    assert parent.get(f"/api/device/chat/picture/{mid}", headers=ADAM).status_code == 404
    held = parent.get("/api/parent/chat-held").json()["held"]
    assert [h["id"] for h in held] == [mid] and held[0]["held"]
    assert parent.get(f"/api/parent/chat/picture/{mid}").status_code == 200       # parents can look
    # A parent's own picture is never checked.
    assert not parent.get("/api/parent/chat-held").json()["held"][1:]
    assert parent.post(f"/api/parent/chat/{mid}/release").status_code == 200
    assert parent.get(f"/api/device/chat/picture/{mid}", headers=ADAM).status_code == 200
    assert parent.get("/api/parent/chat-held").json()["held"] == []


def screening_text():
    from app.chat import HELD
    return HELD


def test_picture_check_lets_an_ordinary_picture_through(tmp_path):
    import pytest
    from app import screening
    if not screening.available():
        pytest.skip("nudenet not installed")
    Image.new("RGB", (640, 480), (90, 140, 200)).save(tmp_path / "sky.jpg")
    assert screening.check_file(tmp_path / "sky.jpg") == ""
