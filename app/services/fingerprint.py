"""Fingerprint binding: several trusted environments per account.

Threat model: the binding is a second line of defence against a *stolen
token/cookie* used without the password. It is NOT a login barrier — a
correct password is the stronger proof, so a successful login from a new
environment registers that environment instead of locking the user out.

Rules:
- Login/register with a correct password: unknown fingerprint → bind it.
- Panel API calls (JWT, no password in play): the fingerprint header must
  match one of the account's bindings; missing/mismatched → 403.
- An account holds at most MAX_BINDINGS fingerprints; the least recently
  seen one is evicted past that.
- Admin escape hatch: clear all bindings (CLI or /users/{id}/fingerprints).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.user_fingerprint import MAX_BINDINGS, UserFingerprint


def _now() -> datetime:
    return datetime.now(timezone.utc)


def has_bindings(db: Session, user_id: int) -> bool:
    return db.scalar(
        select(func.count())
        .select_from(UserFingerprint)
        .where(UserFingerprint.user_id == user_id)
    ) > 0


def bind(db: Session, user_id: int, fp_hash: str | None) -> bool:
    """Register fp_hash for the account (idempotent). Returns True if new."""
    if not fp_hash:
        return False
    existing = db.scalar(
        select(UserFingerprint).where(
            UserFingerprint.user_id == user_id,
            UserFingerprint.fp_hash == fp_hash,
        )
    )
    if existing is not None:
        existing.last_seen_at = _now()
        return False

    # Evict the least-recently-seen binding when at the cap.
    rows = db.scalars(
        select(UserFingerprint)
        .where(UserFingerprint.user_id == user_id)
        .order_by(UserFingerprint.last_seen_at)
    ).all()
    while len(rows) >= MAX_BINDINGS:
        db.delete(rows[0])
        rows = rows[1:]
    db.add(UserFingerprint(user_id=user_id, fp_hash=fp_hash))
    return True


def matches(db: Session, user_id: int, fp_hash: str) -> bool:
    row = db.scalar(
        select(UserFingerprint).where(
            UserFingerprint.user_id == user_id,
            UserFingerprint.fp_hash == fp_hash,
        )
    )
    if row is None:
        return False
    row.last_seen_at = _now()
    return True


def clear(db: Session, user_id: int) -> int:
    """Remove every binding for the account. Returns how many."""
    result = db.execute(
        delete(UserFingerprint).where(UserFingerprint.user_id == user_id)
    )
    return result.rowcount or 0
