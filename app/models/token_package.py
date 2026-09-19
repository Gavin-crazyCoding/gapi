"""Token packages purchased with GavinCoin."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import _utcnow


class TokenPackage(Base):
    __tablename__ = "token_packages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_gavincoin: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="token_packages")  # noqa: F821

    @property
    def used_tokens(self) -> int:
        return max(0, self.total_tokens - self.remaining_tokens)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        # SQLite stores naive datetimes; normalise to UTC before comparing.
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > exp

    @property
    def status(self) -> str:
        """Subscription-style lifecycle state."""
        if self.is_expired:
            return "expired"
        if self.remaining_tokens <= 0:
            return "exhausted"
        return "active"