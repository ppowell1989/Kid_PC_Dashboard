"""Password hashing, parent sessions, device tokens, network-origin checks, LiveKit tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import time

import jwt
from fastapi import Depends, HTTPException, Request

from .config import Kid, Settings, get_settings

PBKDF2_ROUNDS = 310_000
SESSION_COOKIE = "hub_session"
SESSION_SECONDS = 30 * 24 * 3600


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, rounds, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except ValueError:
        return False


def make_session(settings: Settings, parent: str) -> str:
    return jwt.encode({"p": parent, "exp": int(time.time()) + SESSION_SECONDS}, settings.session_secret, "HS256")


def current_parent(request: Request, settings: Settings = Depends(get_settings)) -> str:
    tok = request.cookies.get(SESSION_COOKIE)
    if not tok:
        raise HTTPException(401, "login required")
    try:
        return jwt.decode(tok, settings.session_secret, ["HS256"])["p"]
    except jwt.PyJWTError:
        raise HTTPException(401, "login required")


def _bearer(request: Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ").strip()


def current_device(request: Request, settings: Settings = Depends(get_settings)) -> Kid:
    token = _bearer(request)
    kid = settings.kid_by_token(token) if token else None
    if not kid:
        raise HTTPException(401, "unknown device")
    return kid


def current_extension(request: Request, settings: Settings = Depends(get_settings)) -> Kid:
    """The Chrome extension's token sits in Chrome policy, which the kid's account can read,
    so it is only good for reporting visits."""
    kid = settings.kid_by_extension_token(_bearer(request))
    if not kid:
        raise HTTPException(401, "unknown extension")
    return kid


TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def origin_allowed(settings: Settings, client_ip: str, path: str) -> bool:
    """Enforces the remote_access decision. Friend /join pages and the voice token
    endpoint are always reachable, because friends join from outside the house."""
    if path.startswith("/join") or path.startswith("/api/voice/") or path == "/static/app.css" or path.startswith("/static/fonts/"):   # the join page's stylesheet and its font
        return True
    mode = settings.decisions.remote_access
    if mode == "public":
        return True
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    nets = [ipaddress.ip_network(c) for c in settings.lan_cidrs]
    if mode == "tailscale":
        nets.append(TAILSCALE)
    return any(ip in n for n in nets)


def livekit_token(settings: Settings, identity: str, name: str, room: str, ttl: int = 4 * 3600) -> str:
    """LiveKit access token (a JWT signed with the API secret). No LiveKit SDK needed."""
    now = int(time.time())
    claims = {
        "iss": settings.livekit.api_key,
        "sub": identity,
        "name": name,
        "nbf": now,
        "exp": now + ttl,
        "video": {"room": room, "roomJoin": True, "canPublish": True, "canSubscribe": True},
    }
    return jwt.encode(claims, settings.livekit.api_secret, "HS256")


def livekit_admin_token(settings: Settings, room: str) -> str:
    now = int(time.time())
    return jwt.encode({"iss": settings.livekit.api_key, "nbf": now, "exp": now + 60,
                       "video": {"room": room, "roomAdmin": True}}, settings.livekit.api_secret, "HS256")
