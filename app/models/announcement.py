"""Site-wide announcements shown to users on login."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.user import _utcnow


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    as_html: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Manual on/off switch, independent of the scheduled window. The panel's
    # enable/disable toggle flips this column.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        # SQLite reads datetimes back naive; they are UTC by convention.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    @property
    def active(self) -> bool:
        """True when the announcement is switched on AND inside its window."""
        if not self.is_active:
            return False
        now = datetime.now(timezone.utc)
        if self.start_at and now < self._as_utc(self.start_at):
            return False
        if self.end_at and now > self._as_utc(self.end_at):
            return False
        return True
