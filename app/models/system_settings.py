"""Persisted system settings, editable from the admin panel.

gapi's behaviour is normally driven by environment variables, but the things
an operator tweaks between deploys (coin exchange rate, daily login bonus,
max coin per account) are exactly the things a web UI should change without
a restart. This table is the source of truth for those values; the env var
is the fallback when the row is absent.

A single row per key keeps the schema trivially updatable: ``PUT`` replaces
the value, ``GET`` returns the merged view with env defaults.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.user import _utcnow


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )