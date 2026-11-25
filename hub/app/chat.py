"""Text chat: the family chat, the kids' own chat, and one chat per approved friend.

Who can talk to whom
  family            Mom, Dad and every kid. Works while a PC is locked, so a kid can always reach a parent.
  kids              the brothers together, without the parents in it. Parents read it on the dashboard.
  friend-<hash>     one kid and one friend a parent invited (the same invite link as voice chat). The friend
                    writes from the invite page in any browser. Revoking the link ends the chat at once.

Parents read every conversation, every picture and every link, and can remove a message (the kids and the
friend then see "A parent removed this message"; parents still see what it was). Pictures are re-saved by
the hub, which drops hidden data such as where a phone photo was taken. Words from the parents' flag list
are marked for them, the same as search terms.

A friend conversation's id is a hash of the invite code, not the code: the kid's shell never learns the
code, so a kid can't pass his friend's link on to someone else.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import media, rules, screening
from .config import Kid, Settings, get_settings
from .db import DB
from .security import current_device, current_parent

router = APIRouter()

FAMILY, KIDS, FRIEND = "family", "kids", "friend-"
DEFAULTS = {"kids": True, "friends": True, "pictures": True, "videos": True, "links": True}
MAX_TEXT = 500
MAX_PICTURE_BYTES = 15 * 1024 * 1024   # what a browser or the shell may send, before the hub shrinks it (an iPhone photo is 2-6 MB)
# Picture formats the hub takes in. Everything is re-saved as PNG or JPEG, so the kids' PCs and every browser can show it.
# MPO is how iPhones and many cameras save an ordinary JPEG; HEIF/HEIC is the iPhone's own photo format.
PICTURE_FORMATS = ("PNG", "JPEG", "MPO", "GIF", "WEBP", "BMP", "TIFF", "HEIF", "AVIF")
MAX_VIDEO_BYTES = 100 * 1024 * 1024    # a 30-second phone clip in 4K is about this; the hub keeps a much smaller copy
MAX_VIDEO_SECONDS = 30
VIDEO_SIDE = 1280                       # longest side of the copy the hub keeps (720p)
VIDEO_BRANDS = (b"qt  ", b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"3gp4", b"3gp5", b"dash")
MAX_SIDE = 1600                        # longest side the hub keeps; plenty for a screenshot on a phone
REMOVED = "A parent removed this message."
HELD = "A picture is waiting for Mom or Dad to look at it."
LINK_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"']+")
# Per sender, in memory: a friend's page is on the internet, so a stuck key or a script can't fill the hub.
RATE = {"text": (30, 60), "picture": (10, 600), "video": (5, 600)}    # at most N in so many seconds
_sent: dict[tuple[str, str], list[float]] = {}


def get_db(request: Request) -> DB:
    return request.app.state.db


def store(request: Request) -> media.LocalStore:
    return request.app.state.media


def chat_settings(db: DB) -> dict:
    row = db.one("SELECT value FROM kv WHERE key='chat'")
    return {**DEFAULTS, **(json.loads(row["value"]) if row else {})}


def friend_conv(code: str) -> str:
    return FRIEND + hashlib.sha256(code.encode()).hexdigest()[:16]


def links_in(text: str) -> list[str]:
    return [u.rstrip(".,!?)") for u in LINK_RE.findall(text)]


def _locked(db: DB, settings: Settings, kid: Kid) -> bool:
    from .main import today_state   # main imports this module
    return today_state(db, settings, kid).locked


def _limit(who: str, kind: str) -> None:
    n, seconds = RATE[kind]
    now = time.time()
    recent = [t for t in _sent.get((who, kind), []) if now - t < seconds]
    if len(recent) >= n:
        raise HTTPException(429, "Slow down a little, then try again.")
    if len(_sent) > 5000:
        _sent.clear()
    _sent[(who, kind)] = recent + [now]


# --------------------------------------------------------------------------- conversations

def kid_convs(db: DB, settings: Settings, kid: Kid) -> list[dict]:
    """The conversations one kid's shell shows, in order: family, his brothers, then each friend."""
    cs = chat_settings(db)
    out = [{"id": FAMILY, "title": "Family", "kind": "family"}]
    sibs = [k.name for k in settings.kids if k.id != kid.id]
    if cs["kids"] and sibs:
        out.append({"id": KIDS, "title": " & ".join(sibs), "kind": "kids"})
    if cs["friends"]:
        for inv in db.q("SELECT code, friend_name FROM invites WHERE kid_id=? AND revoked=0 ORDER BY created_at", (kid.id,)):
            out.append({"id": friend_conv(inv["code"]), "title": inv["friend_name"], "kind": "friend"})
    return out


def kid_conv(db: DB, settings: Settings, kid: Kid, conv: str) -> dict:
    c = next((c for c in kid_convs(db, settings, kid) if c["id"] == conv), None)
    if not c:
        raise HTTPException(404, "That chat isn't turned on.")
    if c["kind"] != "family" and _locked(db, settings, kid):
        raise HTTPException(403, "This chat is off while the PC is locked. Family chat still works.")
    return c


def _invite_for(db: DB, conv: str) -> dict | None:
    if not conv.startswith(FRIEND):
        return None
    return next((i for i in db.q("SELECT * FROM invites") if friend_conv(i["code"]) == conv), None)


def live_friend(db: DB, code: str) -> dict:
    inv = db.one("SELECT * FROM invites WHERE code=? AND revoked=0", (code,))
    if not inv:
        raise HTTPException(404, "This invite link is no longer active.")
    return inv


# --------------------------------------------------------------------------- messages

COLS = "id, conv, sender, kid_id, friend, text, at, picture, video, links, flagged, removed_by, held"


def _rows(db: DB, conv: str, after: int, limit: int = 100) -> list[dict]:
    rows = db.q(f"SELECT {COLS} FROM chat WHERE conv=? AND id>? ORDER BY id DESC LIMIT ?", (conv, after, limit))
    return rows[::-1]


def _shown(m: dict, parent: bool) -> dict:
    """One message as a screen shows it. Kids and friends see a removed message as a short note; parents see it all."""
    out = {"id": m["id"], "conv": m["conv"], "sender": m["sender"], "kid_id": m["kid_id"], "friend": bool(m["friend"]),
           "text": m["text"], "at": m["at"], "picture": bool(m["picture"]), "video": bool(m.get("video")), "links": json.loads(m["links"] or "[]"),
           "removed": bool(m["removed_by"])}
    if parent:
        out["flagged"] = bool(m["flagged"])
        out["removed_by"] = m["removed_by"]
        out["held"] = m.get("held") or ""
    elif m["removed_by"]:
        out.update(text=REMOVED, picture=False, video=False, links=[])
    elif m.get("held"):
        out.update(text=HELD, picture=False, video=False, links=[])
    return out


def _post(db: DB, settings: Settings, conv: str, sender: str, kid_id: str | None, friend: bool, text: str,
          picture: str = "", from_parent: bool = False, video: str = "", st: media.LocalStore | None = None) -> int:
    text = text.strip()
    if not text and not picture:
        raise HTTPException(422, "message is empty")
    links = links_in(text)
    if links and not from_parent and not chat_settings(db)["links"]:
        raise HTTPException(403, "Links are switched off in chat.")
    from .main import is_flagged
    now = rules.local_now(settings.timezone).isoformat(timespec="seconds")
    # The picture check (screening.py): a flagged picture or video is held until a parent lets it through.
    held = screening.check(st, picture, video) if st is not None and (picture or video) and not from_parent else ""
    return db.x("INSERT INTO chat(conv,sender,kid_id,friend,text,at,picture,video,links,flagged,held) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (conv, sender, kid_id, int(friend), text, now, picture, video, json.dumps(links), int(is_flagged(text, settings.flag_words)), held))


def _heif() -> None:
    """Teach Pillow to open iPhone HEIC photos (once). Without the package the hub still runs, minus HEIC."""
    global _heif_done
    if _heif_done:
        return
    _heif_done = True
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass


_heif_done = False


def save_picture(st: media.LocalStore, data: str) -> str:
    """Checks the picture really is one, shrinks it, and re-saves it without hidden data (camera, GPS)."""
    from PIL import Image, ImageOps
    _heif()
    try:
        raw = base64.b64decode(data.split(",", 1)[-1], validate=True)   # a browser's data: URL is fine too
    except (binascii.Error, ValueError):
        raise HTTPException(422, "That picture didn't come through. Try again.")
    if len(raw) > MAX_PICTURE_BYTES:
        raise HTTPException(413, "That picture is too big. Try a smaller one.")
    Image.MAX_IMAGE_PIXELS = 60_000_000
    try:
        im = Image.open(io.BytesIO(raw))
        fmt = im.format
        if fmt not in PICTURE_FORMATS:
            raise HTTPException(422, "That kind of picture can't be sent. Photos and screenshots work (JPEG, PNG, HEIC, GIF or WebP).")
        im.seek(0)                         # GIF, MPO, HEIC bursts: the first picture
        im = ImageOps.exif_transpose(im)   # keep a phone photo the right way up once its EXIF is gone
        im.thumbnail((MAX_SIDE, MAX_SIDE))
    except HTTPException:
        raise
    except Exception:
        if raw[4:8] == b"ftyp" and raw[8:12] in VIDEO_BRANDS:
            raise HTTPException(422, "That's a video. Use the video button, or send a photo or screenshot.")
        raise HTTPException(422, "That file isn't a picture the hub can open.")
    folder = st.ready()
    if fmt == "PNG":   # screenshots: keep them sharp
        name = st.new_key("png")
        if im.mode not in ("RGB", "RGBA", "L", "LA"):
            im = im.convert("RGBA")
        im.save(folder / name, "PNG", optimize=True)
    else:
        name = st.new_key("jpg")
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.save(folder / name, "JPEG", quality=85)
    return name


def save_video(st: media.LocalStore, raw: bytes) -> tuple[str, str]:
    """Checks a clip, keeps a small copy (720p H.264 MP4, at most 30 s, no location or camera data) and a still
    picture from its start for the chat bubble. Returns (video key, picture key)."""
    import json as _json
    import shutil
    import subprocess
    if not raw:
        raise HTTPException(422, "That video didn't come through. Try again.")
    if len(raw) > MAX_VIDEO_BYTES:
        raise HTTPException(413, "That video is too big. Try a shorter one.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise HTTPException(503, "Videos aren't set up on the home server yet.")
    src = st.scratch(".in")
    src.write_bytes(raw)
    out_key, pic_key = st.new_key("mp4"), st.new_key("jpg")
    out = st.ready() / out_key
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "format=duration:stream=codec_type",
                                "-of", "json", str(src)], capture_output=True, text=True, timeout=30)
        info = _json.loads(probe.stdout or "{}")
        if probe.returncode != 0 or not info.get("streams"):
            raise HTTPException(422, "That file isn't a video the hub can open.")
        seconds = float(info.get("format", {}).get("duration") or 0)
        if seconds > MAX_VIDEO_SECONDS + 0.5:
            raise HTTPException(422, f"Videos can be up to {MAX_VIDEO_SECONDS} seconds. This one is {round(seconds)} seconds.")
        side = f"scale='if(gt(iw,ih),min({VIDEO_SIDE},iw),-2)':'if(gt(iw,ih),-2,min({VIDEO_SIDE},ih))'"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-t", str(MAX_VIDEO_SECONDS), "-map", "0:v:0", "-map", "0:a:0?",
                            "-map_metadata", "-1", "-map_chapters", "-1", "-vf", side, "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(out)],
                           capture_output=True, text=True, timeout=240)
        if r.returncode != 0 or not out.exists():
            out.unlink(missing_ok=True)
            raise HTTPException(422, "That video couldn't be converted. Try another one.")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out), "-frames:v", "1", "-q:v", "4", str(st.ready() / pic_key)],
                       capture_output=True, timeout=60)
        if not (st.ready() / pic_key).exists():
            pic_key = ""
    except subprocess.TimeoutExpired:
        out.unlink(missing_ok=True)
        raise HTTPException(422, "That video took too long to convert. Try a shorter one.")
    finally:
        src.unlink(missing_ok=True)
    return out_key, pic_key


async def read_upload(request: Request, limit: int) -> bytes:
    """The raw request body, refused as soon as it passes the limit."""
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, "That video is too big. Try a shorter one.")
    return bytes(buf)


def _media_link(request: Request, db: DB, mid: int, col: str, conv_ok) -> RedirectResponse:
    """Access is checked here; the file itself is reached through a short-lived signed link (see media.py)."""
    m = db.one(f"SELECT conv, {col} AS key, removed_by, held FROM chat WHERE id=?", (mid,))
    if not m or not m["key"] or not conv_ok(m) or not store(request).path(m["key"]).exists():
        raise HTTPException(404)
    return RedirectResponse(media.signed_link(get_settings().session_secret, m["key"]), status_code=307,
                            headers={"Cache-Control": "private, max-age=600"})


def _picture_file(request: Request, db: DB, mid: int, conv_ok) -> RedirectResponse:
    return _media_link(request, db, mid, "picture", conv_ok)


def _video_file(request: Request, db: DB, mid: int, conv_ok) -> RedirectResponse:
    return _media_link(request, db, mid, "video", conv_ok)


@router.get("/media/{key}")
def media_file(key: str, exp: int, sig: str, request: Request):
    """A photo or clip, for whoever holds a signed link. Reachable from outside the house, like /join."""
    if not media.KEY_RE.match(key) or not media.link_ok(get_settings().session_secret, key, exp, sig):
        raise HTTPException(404)
    path = store(request).path(key)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type=media.TYPES[path.suffix], headers={"Cache-Control": "private, max-age=3600"})


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    conv: str = Field(default=FAMILY, max_length=40)


class PictureIn(BaseModel):
    data: str = Field(min_length=8, max_length=21_000_000)    # base64 of at most MAX_PICTURE_BYTES, or a data: URL
    text: str = Field(default="", max_length=MAX_TEXT)
    conv: str = Field(default=FAMILY, max_length=40)


# --------------------------------------------------------------------------- kid's PC (through the helper)

@router.get("/api/device/chats")
def device_chats(kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """The kid's conversations, with the newest message id in each and the newest one not from him (for unread dots)."""
    locked = _locked(db, settings, kid)
    cs = chat_settings(db)
    out = []
    for c in kid_convs(db, settings, kid):
        last = db.one("SELECT MAX(id) n FROM chat WHERE conv=?", (c["id"],))["n"] or 0
        other = db.one("SELECT MAX(id) n FROM chat WHERE conv=? AND (kid_id IS NULL OR kid_id!=?)", (c["id"], kid.id))["n"] or 0
        off = locked and c["kind"] != "family"
        out.append({**c, "last_id": last, "last_other_id": other, "can_send": not off,
                    "note": "Off while the PC is locked" if off else ""})
    return {"convs": out, "pictures": cs["pictures"], "videos": cs["videos"], "links": cs["links"]}


@router.get("/api/device/chat")
def device_chat(after: int = 0, conv: str = FAMILY, kid: Kid = Depends(current_device), db: DB = Depends(get_db),
                settings: Settings = Depends(get_settings)):
    kid_conv(db, settings, kid, conv)
    return [{**_shown(m, False), "mine": m["kid_id"] == kid.id} for m in _rows(db, conv, after)]


@router.post("/api/device/chat", status_code=201)
def device_chat_send(m: ChatIn, kid: Kid = Depends(current_device), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    kid_conv(db, settings, kid, m.conv)
    _limit(f"kid-{kid.id}", "text")
    return {"id": _post(db, settings, m.conv, kid.name, kid.id, False, m.text)}


@router.post("/api/device/chat/picture", status_code=201)
def device_chat_picture(p: PictureIn, request: Request, kid: Kid = Depends(current_device), db: DB = Depends(get_db),
                        settings: Settings = Depends(get_settings)):
    kid_conv(db, settings, kid, p.conv)
    if not chat_settings(db)["pictures"]:
        raise HTTPException(403, "Pictures are switched off in chat.")
    _limit(f"kid-{kid.id}", "picture")
    name = save_picture(store(request), p.data)
    return {"id": _post(db, settings, p.conv, kid.name, kid.id, False, p.text, picture=name, st=store(request))}


@router.get("/api/device/chat/picture/{mid}")
def device_chat_picture_file(mid: int, request: Request, kid: Kid = Depends(current_device), db: DB = Depends(get_db),
                             settings: Settings = Depends(get_settings)):
    mine = {c["id"] for c in kid_convs(db, settings, kid)}
    return _picture_file(request, db, mid, lambda m: m["conv"] in mine and not m["removed_by"] and not m["held"])


def _videos_ok(db: DB) -> None:
    if not chat_settings(db)["videos"]:
        raise HTTPException(403, "Videos are switched off in chat.")


@router.post("/api/device/chat/video", status_code=201)
async def device_chat_video(request: Request, conv: str = FAMILY, text: str = "", kid: Kid = Depends(current_device),
                            db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Body: the clip's bytes. Words with it go in ?text=."""
    kid_conv(db, settings, kid, conv)
    _videos_ok(db)
    _limit(f"kid-{kid.id}", "video")
    raw = await read_upload(request, MAX_VIDEO_BYTES)
    vid, pic = await run_in_threadpool(save_video, store(request), raw)
    return {"id": _post(db, settings, conv, kid.name, kid.id, False, text[:MAX_TEXT], picture=pic, video=vid, st=store(request))}


@router.get("/api/device/chat/video/{mid}")
def device_chat_video_file(mid: int, request: Request, kid: Kid = Depends(current_device), db: DB = Depends(get_db),
                           settings: Settings = Depends(get_settings)):
    mine = {c["id"] for c in kid_convs(db, settings, kid)}
    return _video_file(request, db, mid, lambda m: m["conv"] in mine and not m["removed_by"] and not m["held"])


# --------------------------------------------------------------------------- friend's browser (invite link)

def _friend_ok(db: DB) -> None:
    if not chat_settings(db)["friends"]:
        raise HTTPException(403, "Text chat is switched off right now.")


@router.get("/join/{code}/chat")
def friend_chat(code: str, after: int = 0, db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    inv = live_friend(db, code)
    cs = chat_settings(db)
    kid = next((k for k in settings.kids if k.id == inv["kid_id"]), None)
    if not cs["friends"] or not kid:
        return {"on": False, "kid_name": kid.name if kid else "", "friend_name": inv["friend_name"], "messages": []}
    conv = friend_conv(code)
    msgs = [{**_shown(m, False), "mine": bool(m["friend"])} for m in _rows(db, conv, after)]
    for m in msgs:
        m.pop("kid_id")
    return {"on": True, "kid_name": kid.name, "friend_name": inv["friend_name"], "pictures": cs["pictures"], "videos": cs["videos"],
            "links": cs["links"], "messages": msgs}


class FriendIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)


@router.post("/join/{code}/chat", status_code=201)
def friend_chat_send(code: str, m: FriendIn, db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    inv = live_friend(db, code)
    _friend_ok(db)
    _limit(f"friend-{code}", "text")
    return {"id": _post(db, settings, friend_conv(code), inv["friend_name"], None, True, m.text)}


@router.post("/join/{code}/chat/picture", status_code=201)
def friend_chat_picture(code: str, p: PictureIn, request: Request, db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    inv = live_friend(db, code)
    _friend_ok(db)
    if not chat_settings(db)["pictures"]:
        raise HTTPException(403, "Pictures are switched off in chat.")
    _limit(f"friend-{code}", "picture")
    name = save_picture(store(request), p.data)
    return {"id": _post(db, settings, friend_conv(code), inv["friend_name"], None, True, p.text, picture=name, st=store(request))}


@router.get("/join/{code}/chat/picture/{mid}")
def friend_chat_picture_file(code: str, mid: int, request: Request, db: DB = Depends(get_db)):
    live_friend(db, code)
    conv = friend_conv(code)
    return _picture_file(request, db, mid, lambda m: m["conv"] == conv and not m["removed_by"] and not m["held"])


@router.post("/join/{code}/chat/video", status_code=201)
async def friend_chat_video(code: str, request: Request, text: str = "", db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    inv = live_friend(db, code)
    _friend_ok(db)
    _videos_ok(db)
    _limit(f"friend-{code}", "video")
    raw = await read_upload(request, MAX_VIDEO_BYTES)
    vid, pic = await run_in_threadpool(save_video, store(request), raw)
    return {"id": _post(db, settings, friend_conv(code), inv["friend_name"], None, True, text[:MAX_TEXT], picture=pic, video=vid, st=store(request))}


@router.get("/join/{code}/chat/video/{mid}")
def friend_chat_video_file(code: str, mid: int, request: Request, db: DB = Depends(get_db)):
    live_friend(db, code)
    conv = friend_conv(code)
    return _video_file(request, db, mid, lambda m: m["conv"] == conv and not m["removed_by"] and not m["held"])


# --------------------------------------------------------------------------- parents' dashboard

@router.get("/api/parent/chat")
def parent_chat(after: int = 0, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    """The family chat, which the parents write in."""
    return [{**_shown(m, True), "mine": m["kid_id"] is None and not m["friend"] and m["sender"] == parent}
            for m in _rows(db, FAMILY, after)]


@router.post("/api/parent/chat", status_code=201)
def parent_chat_send(m: ChatIn, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    if m.conv != FAMILY:
        raise HTTPException(403, "Parents write in the family chat. The other chats are for reading.")
    return {"id": _post(db, settings, FAMILY, parent, None, False, m.text, from_parent=True)}


@router.post("/api/parent/chat/picture", status_code=201)
def parent_chat_picture(p: PictureIn, request: Request, parent: str = Depends(current_parent), db: DB = Depends(get_db),
                        settings: Settings = Depends(get_settings)):
    if p.conv != FAMILY:
        raise HTTPException(403, "Parents write in the family chat. The other chats are for reading.")
    name = save_picture(store(request), p.data)
    return {"id": _post(db, settings, FAMILY, parent, None, False, p.text, picture=name, from_parent=True)}


def _stats(db: DB, conv: str) -> dict:
    s = db.one("""SELECT COUNT(*) n, MAX(at) last_at, SUM(picture!='') pictures, SUM(links!='[]' AND links!='') links,
                  SUM(flagged) flagged FROM chat WHERE conv=?""", (conv,))
    last = db.one("SELECT sender, text, picture FROM chat WHERE conv=? ORDER BY id DESC LIMIT 1", (conv,))
    return {"count": s["n"], "last_at": s["last_at"] or "", "pictures": s["pictures"] or 0, "links": s["links"] or 0,
            "flagged": s["flagged"] or 0, "last_id": db.one("SELECT MAX(id) n FROM chat WHERE conv=?", (conv,))["n"] or 0,
            "last": ({"sender": last["sender"], "text": last["text"] or ("Sent a picture" if last["picture"] else "")} if last else None)}


@router.get("/api/parent/chats")
def parent_chats(parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Every conversation the kids have, for reading: the family chat, the brothers' chat, and each friend's
    (revoked friends too while their messages are kept)."""
    names = {k.id: k.name for k in settings.kids}
    out = [{"id": FAMILY, "title": "Family chat", "kind": "family", "who": "Everyone", "can_send": True, "active": True}]
    if len(settings.kids) > 1:
        out.append({"id": KIDS, "title": " & ".join(names.values()), "kind": "kids", "who": "Just the kids",
                    "can_send": False, "active": chat_settings(db)["kids"]})
    seen = set()
    for inv in db.q("SELECT code, kid_id, friend_name, revoked FROM invites ORDER BY created_at"):
        conv = friend_conv(inv["code"])
        seen.add(conv)
        out.append({"id": conv, "title": f"{names.get(inv['kid_id'], inv['kid_id'])} & {inv['friend_name']}", "kind": "friend",
                    "who": "Friend" + (" (link revoked)" if inv["revoked"] else ""), "can_send": False, "active": not inv["revoked"],
                    "kid_id": inv["kid_id"]})
    # Messages from a friend whose revoked link has since been cleaned up are still the parents' to read.
    for r in db.q(f"SELECT DISTINCT conv FROM chat WHERE conv LIKE '{FRIEND}%'"):
        if r["conv"] not in seen:
            who = db.one("SELECT sender FROM chat WHERE conv=? AND friend=1 LIMIT 1", (r["conv"],))
            out.append({"id": r["conv"], "title": who["sender"] if who else "A friend", "kind": "friend",
                        "who": "Friend (link removed)", "can_send": False, "active": False})
    for c in out:
        c.update(_stats(db, c["id"]))
    return {"convs": out, "settings": chat_settings(db)}


@router.get("/api/parent/chats/{conv}")
def parent_conv(conv: str, after: int = 0, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    return [{**_shown(m, True), "mine": m["kid_id"] is None and not m["friend"] and m["sender"] == parent}
            for m in _rows(db, conv, after, limit=300)]


@router.get("/api/parent/chat-shared")
def parent_shared(days: int = 0, parent: str = Depends(current_parent), db: DB = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Every picture and link sent in any chat, newest first."""
    from .main import since
    rows = db.q(f"SELECT {COLS} FROM chat WHERE (picture!='' OR (links!='[]' AND links!='')) AND at >= ? ORDER BY id DESC LIMIT 300",
                (since(settings, days),))
    return [_shown(m, True) for m in rows]


@router.get("/api/parent/chat/picture/{mid}")
def parent_chat_picture_file(mid: int, request: Request, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    return _picture_file(request, db, mid, lambda m: True)


@router.post("/api/parent/chat/video", status_code=201)
async def parent_chat_video(request: Request, text: str = "", parent: str = Depends(current_parent), db: DB = Depends(get_db),
                            settings: Settings = Depends(get_settings)):
    raw = await read_upload(request, MAX_VIDEO_BYTES)
    vid, pic = await run_in_threadpool(save_video, store(request), raw)
    return {"id": _post(db, settings, FAMILY, parent, None, False, text[:MAX_TEXT], picture=pic, video=vid, from_parent=True)}


@router.get("/api/parent/chat/video/{mid}")
def parent_chat_video_file(mid: int, request: Request, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    return _video_file(request, db, mid, lambda m: True)


@router.delete("/api/parent/chat/{mid}")
def parent_remove(mid: int, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    """Hide a message from the kids and the friend. The parents keep seeing it, marked removed."""
    if not db.one("SELECT id FROM chat WHERE id=?", (mid,)):
        raise HTTPException(404)
    db.x("UPDATE chat SET removed_by=? WHERE id=? AND removed_by=''", (parent, mid))
    return {}


class ChatSettings(BaseModel):
    kids: bool | None = None       # the brothers' own chat
    friends: bool | None = None    # chat with invited friends
    pictures: bool | None = None   # photos and screenshots
    videos: bool | None = None     # clips up to 30 seconds
    links: bool | None = None      # web links in messages


@router.put("/api/parent/chat-settings")
def parent_chat_settings(c: ChatSettings, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    new = {**chat_settings(db), **{k: v for k, v in c.model_dump().items() if v is not None}}
    db.x("INSERT INTO kv(key,value) VALUES('chat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(new),))
    return new


@router.get("/api/parent/chat-held")
def parent_chat_held(parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    """Pictures and videos the picture check held, waiting for a parent (shown in Requests)."""
    return {"check": screening.status(),
            "held": [_shown(m, True) for m in db.q(f"SELECT {COLS} FROM chat WHERE held!='' AND removed_by='' ORDER BY id DESC LIMIT 100")]}


@router.post("/api/parent/chat/{mid}/release")
def parent_chat_release(mid: int, parent: str = Depends(current_parent), db: DB = Depends(get_db)):
    """The picture check was wrong: let the kids (and the friend) see it."""
    if not db.one("SELECT 1 FROM chat WHERE id=? AND held!=''", (mid,)):
        raise HTTPException(404, "Nothing is held with that number.")
    db.x("UPDATE chat SET held='' WHERE id=?", (mid,))
    return {"ok": True}
