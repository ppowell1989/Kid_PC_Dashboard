"""Loads config.yaml into typed settings."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class DayValues(BaseModel):
    weekday: int
    weekend: int


class DayTimes(BaseModel):
    weekday: str
    weekend: str


class Kid(BaseModel):
    id: str
    name: str
    device_token: str            # admin helper only (kept where the kid cannot read it)
    extension_token: str = ""    # Chrome extension only; may post visits and nothing else
    adguard_client: str = ""
    daily_minutes: DayValues = DayValues(weekday=90, weekend=180)
    bedtime: DayTimes = DayTimes(weekday="20:30", weekend="21:30")


class Parent(BaseModel):
    name: str
    password_hash: str


class Decisions(BaseModel):
    web_mode: Literal["allowlist", "filtered"] = "filtered"
    game_pass_enabled: bool = False
    friend_verification: Literal["none", "discord_oauth"] = "none"
    remote_access: Literal["lan_only", "tailscale", "public"] = "lan_only"


class AdGuard(BaseModel):
    enabled: bool = False
    url: str = "http://adguard:3000"
    username: str = "admin"
    password: str = ""
    poll_seconds: int = 120


class LiveKit(BaseModel):
    url: str = "ws://localhost:7880"
    api_url: str = "http://livekit:7880"   # server-side API, used to kick revoked friends
    api_key: str = "devkey"
    api_secret: str = "devsecret-devsecret-devsecret-devsecret"
    public_join_base: str = "http://localhost:8080"


class DiscordOAuth(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""


class Settings(BaseModel):
    timezone: str = "America/New_York"
    parents: list[Parent] = Field(default_factory=list)
    kids: list[Kid] = Field(default_factory=list)
    decisions: Decisions = Decisions()
    lan_cidrs: list[str] = Field(default_factory=lambda: ["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "127.0.0.0/8"])
    allowed_sites: list[str] = Field(default_factory=list)
    # Apps the helper on each PC installs (silently, as SYSTEM) if they are missing.
    managed_apps: list[Literal["steam", "chrome", "battlenet"]] = Field(default_factory=lambda: ["steam", "chrome"])
    session_secret: str = "dev-secret"
    adguard: AdGuard = AdGuard()
    livekit: LiveKit = LiveKit()
    discord_oauth: DiscordOAuth = DiscordOAuth()

    def kid(self, kid_id: str) -> Kid | None:
        return next((k for k in self.kids if k.id == kid_id), None)

    def kid_by_token(self, token: str) -> Kid | None:
        return next((k for k in self.kids if k.device_token == token), None)

    def kid_by_extension_token(self, token: str) -> Kid | None:
        return next((k for k in self.kids if token and token in (k.extension_token, k.device_token)), None)


def load_settings(path: str | None = None) -> Settings:
    path = path or os.environ.get("HUB_CONFIG", "config.yaml")
    with open(path, encoding="utf-8") as f:
        return Settings.model_validate(yaml.safe_load(f) or {})


@lru_cache
def get_settings() -> Settings:
    return load_settings()
