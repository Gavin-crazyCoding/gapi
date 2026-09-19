"""Password hashing, JWT issuance, and gapi API-key generation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings

# ── Passwords (bcrypt) ──────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# ── JWT (dashboard/session auth) ────────────────────────────────────────────

def create_access_token(subject: str, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": now + timedelta(days=jwt_expire_days()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Raises jwt.PyJWTError on invalid/expired tokens."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def jwt_expire_days() -> int:
    return settings.jwt_expire_days


# ── gapi API keys (high-entropy random; SHA-256 at rest) ───────────────────

GAPI_KEY_PREFIX = "gapi-"


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(plaintext, key_hash, key_prefix)``.

    plaintext is shown to the user exactly once; only its SHA-256 hex digest
    and a short display prefix are persisted.
    """
    plaintext = GAPI_KEY_PREFIX + secrets.token_hex(24)
    return plaintext, hash_api_key(plaintext), api_key_prefix(plaintext)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.strip().encode()).hexdigest()


def api_key_prefix(plaintext: str) -> str:
    return plaintext[:12]


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
