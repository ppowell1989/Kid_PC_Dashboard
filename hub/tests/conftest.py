import os
import tempfile

import pytest
import yaml

from app.security import hash_password

CONFIG = {
    "timezone": "UTC",
    "parents": [{"name": "Patrick", "password_hash": hash_password("pw")}],
    "kids": [
        {"id": "adam", "name": "Adam", "device_token": "tok-adam", "extension_token": "ext-adam", "adguard_client": "adam-pc",
         "daily_minutes": {"weekday": 60, "weekend": 60}, "bedtime": {"weekday": "21:00", "weekend": "21:00"}},
        {"id": "barrett", "name": "Barrett", "device_token": "tok-barrett",
         "daily_minutes": {"weekday": 60, "weekend": 60}, "bedtime": {"weekday": "21:00", "weekend": "21:00"}},
    ],
    "decisions": {"web_mode": "filtered", "game_pass_enabled": False, "friend_verification": "none", "remote_access": "lan_only"},
    "allowed_sites": ["wikipedia.org"],
    "session_secret": "test-session-secret-test-session-secret",
    "adguard": {"enabled": False},
    "livekit": {"url": "ws://lk", "api_url": "http://127.0.0.1:9", "api_key": "k", "api_secret": "s" * 32, "public_join_base": "http://hub"},
}


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    d = tempfile.mkdtemp()
    cfg = os.path.join(d, "config.yaml")
    with open(cfg, "w") as f:
        yaml.safe_dump(CONFIG, f)
    monkeypatch.setenv("HUB_CONFIG", cfg)
    monkeypatch.setenv("HUB_DB", os.path.join(d, "hub.db"))
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import config, rules
    # Freeze "now" at Tuesday noon so bedtime never interferes with the tests.
    monkeypatch.setattr(rules, "local_now", lambda tz: datetime(2026, 11, 24, 12, 0, tzinfo=ZoneInfo(tz)))
    config.get_settings.cache_clear()
    from app.main import app
    with TestClient(app, client=("192.168.1.20", 5000)) as c:
        yield c
    config.get_settings.cache_clear()


@pytest.fixture
def parent(client):
    r = client.post("/api/parent/login", json={"name": "patrick", "password": "pw"})
    assert r.status_code == 200
    return client


ADAM = {"Authorization": "Bearer tok-adam"}
