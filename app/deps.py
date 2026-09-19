"""FastAPI dependencies: DB session, JWT user, and gapi-key principal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import GapiError, forbidden, unauthorized
from app.models import ApiKey, User
from app.security import decode_access_token, hash_api_key

_jwt_bearer = HTTPBearer(auto_error=False)

# Panel sessions live in an HttpOnly cookie so injected JS can never read the
# JWT; SDK/API clients keep using Authorization: Bearer.
SESSION_COOKIE = "gapi_session"


# ── JWT (panel endpoints) ───────────────────────────────────────────────────

def _resolve_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
    *,
    check_fingerprint: bool,
) -> User:
    token = credentials.credentials if credentials else request.cookies.get(SESSION_COOKIE)
    if not token:
        raise unauthorized("Missing bearer token")
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise unauthorized("Invalid or expired token")
    user = db.get(User, int(payload.get("sub", 0)))
    if user is None:
        raise unauthorized("Unknown user")
    if not user.is_active:
        raise forbidden("Account is deactivated")
    if not check_fingerprint:
        return user
    # Browser/environment fingerprint binding. Once an account holds any
    # binding, every DATA request MUST carry one of the bound hashes: a stolen
    # JWT/cookie used without the password is refused even with the header
    # simply omitted. New environments join the set at *login* time
    # (password-verified), never here.
    from app.services import fingerprint as fp_service
    fingerprint = request.headers.get("x-fingerprint")
    if fp_service.has_bindings(db, user.id):
        if not fingerprint or not fp_service.matches(db, user.id, fingerprint):
            raise GapiError(403, "fingerprint_mismatch", "浏览器环境与账号绑定的不一致，请重新登录")
        db.commit()  # persist last_seen_at
    return user


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_jwt_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Strict variant for API data endpoints (fetch can inject X-Fingerprint)."""
    return _resolve_user(request, credentials, db, check_fingerprint=True)


def get_current_user_lenient(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_jwt_bearer),
    db: Session = Depends(get_db),
) -> User:
    """JWT check WITHOUT the fingerprint gate.

    For the panel shell and its JS assets: browser navigation and module
    imports can never attach custom headers, so gating them would lock every
    bound account out of the panel entirely. They ship only code and markup —
    every byte of user data still goes through the strict variant above.
    """
    return _resolve_user(request, credentials, db, check_fingerprint=False)


def get_admin_user(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise GapiError(403, "admin_required", "Administrator role required")
    return user


# ── gapi API key (proxy endpoints) ──────────────────────────────────────────

@dataclass
class KeyPrincipal:
    user: User
    api_key: ApiKey


def _extract_key(request: Request) -> str:
    """Same lookup order as upstream: Bearer → x-api-key → x-goog-api-key."""
    auth = request.headers.get("authorization")
    if auth:
        token = auth.strip()
        if token[:7].lower() == "bearer ":
            token = token[7:].strip()
        if token:
            return token
    for header in ("x-api-key", "x-goog-api-key"):
        token = request.headers.get(header)
        if token:
            return token.strip()
    return ""


def get_key_principal(
    request: Request,
    db: Session = Depends(get_db),
) -> KeyPrincipal:
    plaintext = _extract_key(request)
    if not plaintext:
        raise unauthorized("Missing API key")
    api_key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(plaintext)))
    if api_key is None or not api_key.is_active:
        raise unauthorized("Invalid API key")
    if api_key.expires_at is not None:
        expires = api_key.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            raise unauthorized("API key has expired")
    user = db.get(User, api_key.user_id)
    if user is None or not user.is_active:
        raise forbidden("Account is inactive")

    # Fingerprint binding applies to browser panel sessions (JWT) only. API
    # clients (SDKs, curl) never send X-Fingerprint, so checking it here would
    # either lock them all out or — as before — do nothing at all.

    # last_used_at is a display nicety, not a meter: writing it on every call
    # doubles the proxy's write load. Once a minute is plenty.
    now = datetime.now(timezone.utc)
    last = api_key.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is None or (now - last).total_seconds() > 60:
        api_key.last_used_at = now
        db.commit()
    return KeyPrincipal(user=user, api_key=api_key)
