"""Picture check: a nudity detector that runs on the family's own server, for pictures and videos sent in chat.

Patrick's choice (2026-11-25): "Auto-check only". Everything arrives straight away, except a picture or video the
detector flags: that one is held, the kids and the friend see "waiting for a parent", and it shows in Requests
for a parent to let through or remove. Nothing is sent anywhere: the model (NudeNet, MIT licence, about 12 MB,
bundled with the package) runs on the hub's CPU in a few tens of milliseconds per picture.

Pictures from a parent are not checked. If the detector isn't installed or fails, nothing is held (the hub logs
it and the Connections page says the check is off), so chat keeps working.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Body parts that hold a picture. Covered parts, faces, feet and bellies never do.
FLAG = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}
MIN_SCORE = 0.45            # GUESS: NudeNet's own examples use about 0.5; a little lower errs toward holding
VIDEO_FRAMES = 6            # frames checked across a clip (clips are at most 30 s)

_detector = None
_failed = ""


def available() -> bool:
    return _load() is not None


def status() -> str:
    return "on" if available() else f"off: {_failed}"


def _load():
    global _detector, _failed
    if _detector is None and not _failed:
        try:
            from nudenet import NudeDetector
            _detector = NudeDetector()
        except Exception as e:   # not installed, or the model wouldn't load
            _failed = str(e)[:200] or type(e).__name__
            log.warning("picture check is off: %s", _failed)
    return _detector


def check_file(path: Path) -> str:
    """'' if the picture is fine, else a short reason for the parents."""
    d = _load()
    if d is None or not path.exists():
        return ""
    try:
        hits = [h for h in d.detect(str(path)) if h.get("class") in FLAG and h.get("score", 0) >= MIN_SCORE]
    except Exception as e:
        log.warning("picture check failed on %s: %s", path.name, e)
        return ""
    if not hits:
        return ""
    return "The picture check found what looks like nudity"


def check_video(path: Path) -> str:
    """Checks a few frames spread across a clip."""
    if _load() is None or not path.exists() or not shutil.which("ffmpeg"):
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            step = max(float(dur or 0) / VIDEO_FRAMES, 0.5)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps=1/{step:.2f}", "-frames:v", str(VIDEO_FRAMES),
                            "-q:v", "4", str(Path(tmp) / "f%02d.jpg")], capture_output=True, timeout=120)
        except (subprocess.TimeoutExpired, ValueError):
            return ""
        for f in sorted(Path(tmp).glob("f*.jpg")):
            why = check_file(f)
            if why:
                return why.replace("picture check", "picture check, in this video,")
    return ""


def check(store, picture: str, video: str) -> str:
    """For a chat message's media keys (see media.py). '' = let it through."""
    if video:
        why = check_video(store.path(video))
        if why:
            return why
    return check_file(store.path(picture)) if picture else ""
