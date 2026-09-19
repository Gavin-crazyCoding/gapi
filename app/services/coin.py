"""System settings + coin configuration helpers.

Values that an operator might change between deploys live in the
``system_settings`` table (read through :func:`get_settings`).  The
environment is still the source of truth when the table row is absent.

Reads are cached process-wide for a few seconds: every registration, login,
purchase and /config call needs these values, and they change about once a
week. Writes through :func:`set_setting` invalidate the cache immediately.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.config import settings as env_settings
from app.database import SessionLocal
from app.models.credit_transaction import CreditTransaction
from app.models.system_settings import SystemSettings
from app.models.user import User

# Precedence is DB row > .env > the literal fallback below. The env-derived
# values keep GAPI_TOKEN_PACKAGE_RATE / GAPI_REGISTRATION_BONUS meaningful:
# without them the .env file was silently ignored once the DB table existed.
DEFAULTS: dict[str, str] = {
    "coin_rate": str(env_settings.token_package_rate),  # GAPI_TOKEN_PACKAGE_RATE
    "daily_bonus": "1",           # GavinCoin awarded per daily login
    "max_coin_per_user": "1000000",
    "registration_bonus": str(env_settings.registration_bonus),  # GAPI_REGISTRATION_BONUS
    "registration_open": "true",  # "false" closes public signups (invite-only ops)
    # Global multiplier on metered billing (playground + /v1 calls). This is
    # the knob people reach for when they say "汇率": 2 = double all prices.
    "rate_multiplier": "1",
}

_CACHE_TTL = 60.0
_cache: tuple[float, dict[str, str]] | None = None


def invalidate_settings() -> None:
    """Drop the cached settings (tests, and writes from other processes)."""
    global _cache
    _cache = None


def _read_all(db: Session) -> dict[str, str]:
    stored = {row.key: row.value for row in db.scalars(select(SystemSettings)).all()}
    return {**DEFAULTS, **stored}


def get_settings(db: Session | None = None) -> dict[str, str]:
    """Return merged settings: DB overrides env defaults (cached ~60s)."""
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < _CACHE_TTL:
        return _cache[1]
    if db is None:
        with SessionLocal() as own:
            merged = _read_all(own)
    else:
        merged = _read_all(db)
    _cache = (time.monotonic(), merged)
    return merged


def get_coin_rate(db: Session | None = None) -> Decimal:
    v = get_settings(db).get("coin_rate", "100")
    return Decimal(v)


def get_daily_bonus(db: Session | None = None) -> Decimal:
    v = get_settings(db).get("daily_bonus", "1")
    return Decimal(v)


def get_max_coin_per_user(db: Session | None = None) -> Decimal:
    v = get_settings(db).get("max_coin_per_user", "1000000")
    return Decimal(v)


def get_registration_bonus(db: Session | None = None) -> Decimal:
    v = get_settings(db).get("registration_bonus", "20")
    return Decimal(v)


def get_rate_multiplier(db: Session | None = None) -> Decimal:
    v = get_settings(db).get("rate_multiplier", "1")
    try:
        m = Decimal(v)
    except Exception:
        return Decimal("1")
    return m if m > 0 else Decimal("1")


# ── daily check-in ──────────────────────────────────────────────────────────

_CHECKIN_WINDOW = timedelta(hours=24)


def award_daily_bonus(db: Session, user: User) -> Decimal | None:
    """Credit the daily login bonus if the user has not checked in for 24h.

    Returns the granted amount (0 when the balance cap ate it), or None when
    the user already checked in within the window. The claim itself is one
    guarded UPDATE inside BEGIN IMMEDIATE, so two racing requests (double
    click, two tabs) can never both win the same day's bonus.
    """
    # SQLite stores datetimes as naive strings; comparing an aware UTC value
    # against a freshly-loaded naive column raises TypeError. Keep the whole
    # comparison naive-UTC so the guarded UPDATE works regardless of which
    # session the caller's `user` object came from.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - _CHECKIN_WINDOW

    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        claimed = db.execute(
            update(User)
            .where(
                User.id == user.id,
                (User.last_login_date.is_(None)) | (User.last_login_date < cutoff),
            )
            .values(last_login_date=now)
        )
        if claimed.rowcount == 0:
            db.rollback()
            return None

        old_balance = db.scalar(
            select(User.gavincoin_balance).where(User.id == user.id)
        )
        new_balance = min(old_balance + get_daily_bonus(db), get_max_coin_per_user(db))
        granted = new_balance - old_balance
        db.execute(
            update(User)
            .where(User.id == user.id)
            .values(gavincoin_balance=new_balance)
        )
        if granted > 0:
            db.add(
                CreditTransaction(
                    user_id=user.id,
                    amount=granted,
                    balance_after=new_balance,
                    tx_type="daily_login",
                    note="daily check-in reward",
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(user)
    return granted


def registration_open(db: Session | None = None) -> bool:
    return get_settings(db).get("registration_open", "true").lower() == "true"


def set_setting(db: Session, key: str, value: str, *, commit: bool = True) -> SystemSettings:
    """Upsert a single setting. Returns the row (flushed, or persisted).

    Pass ``commit=False`` to batch several upserts into one surrounding
    transaction (the admin settings endpoint validates then commits once).
    """
    row = db.scalar(select(SystemSettings).where(SystemSettings.key == key))
    if row is None:
        row = SystemSettings(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    invalidate_settings()
    return row
