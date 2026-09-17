"""Simple single-user session auth for the local demo."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException, Request
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware.sessions import SessionMiddleware


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    fyfteen_user: str = "fyfteen"
    fyfteen_password: str = ""
    fyfteen_session_secret: str = ""


def settings() -> AuthSettings:
    return AuthSettings()


def enabled() -> bool:
    return bool(settings().fyfteen_password)


def session_secret() -> str:
    cfg = settings()
    if cfg.fyfteen_session_secret:
        return cfg.fyfteen_session_secret
    if cfg.fyfteen_password:
        return hashlib.sha256(f"fyfteen:{cfg.fyfteen_password}".encode()).hexdigest()
    return "fyfteen-local-dev-secret"


def install(app) -> None:
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret(),
        session_cookie="fyfteen_session",
        same_site="lax",
        https_only=False,
        max_age=60 * 60 * 12,
    )


def _secret_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(f"fyfteen:{left}".encode()).digest(),
        hashlib.sha256(f"fyfteen:{right}".encode()).digest(),
    )


def verify_login(username: str, password: str) -> bool:
    cfg = settings()
    if not cfg.fyfteen_password:
        return True
    return _secret_eq(username, cfg.fyfteen_user) and _secret_eq(password, cfg.fyfteen_password)


def logged_in(request: Request) -> bool:
    if not enabled():
        return True
    return bool(request.session.get("user"))


def require_user(request: Request) -> None:
    if logged_in(request):
        return
    raise HTTPException(status_code=401, detail="Sign in required")


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def require_csrf(request: Request) -> None:
    if not enabled():
        return
    header = request.headers.get("x-csrf-token") or ""
    token = request.session.get("csrf") or ""
    if not token or not hmac.compare_digest(header, token):
        raise HTTPException(status_code=403, detail="CSRF check failed")


def login(request: Request, username: str) -> None:
    request.session["user"] = username
    request.session["csrf"] = secrets.token_urlsafe(24)


def logout(request: Request) -> None:
    request.session.clear()
