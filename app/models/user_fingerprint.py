"""Bound browser/environment fingerprints, several per account.

A single hash column was too brittle: the same browser drifts (window size,
zoom level, UA version bump) and produced a different hash, locking the
legitimate owner out. An account may now hold up to MAX_BINDINGS hashes;
any of them satisfies a panel request.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import _utcnow

MAX_BINDINGS = 5


class UserFingerprint(Base):
    __tablename__ = "user_fingerprints"
    __table_args__ = (
        UniqueConstraint("user_id", "fp_hash", name="uq_user_fingerprint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    fp_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="fingerprint_bindings")  # noqa: F821
