"""User accounts. The first registered account becomes admin."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, Numeric, String, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    routing_strategy: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    gavincoin_balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), default=Decimal("0"), nullable=False
    )
    # Registration creates an EmailVerification row; the flag flips when the
    # link is clicked. Nothing gates on it yet, but it must tell the truth.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    concurrent_limit: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    concurrent_active: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_login_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_announcement_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    api_keys: Mapped[list["ApiKey"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
    token_packages: Mapped[list["TokenPackage"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
    usage_records: Mapped[list["UsageRecord"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
    credit_transactions: Mapped[list["CreditTransaction"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
    # email_verifications has no DB-level ON DELETE rule; without this cascade,
    # deleting a user who never clicked their verification link violates the FK.
    email_verifications: Mapped[list["EmailVerification"]] = relationship(  # noqa: F821
        cascade="all, delete-orphan"
    )
    # Bound browser fingerprints (see app/services/fingerprint.py).
    fingerprint_bindings: Mapped[list["UserFingerprint"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"
