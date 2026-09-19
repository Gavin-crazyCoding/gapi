"""Per-model token pricing table (editable via CLI)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.user import _utcnow


class Pricing(Base):
    __tablename__ = "pricing"

    id: Mapped[int] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    input_per_1k: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    output_per_1k: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    # Per-call flat fee for media endpoints (images/videos/audio), which produce
    # no token usage. NULL means "bill by tokens" for this model.
    flat_fee_coin: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
