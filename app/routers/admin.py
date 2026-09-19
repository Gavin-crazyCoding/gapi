"""Admin-only routes: system settings and announcements.

User management lives in app/routers/users.py (the /users surface) — there is
deliberately exactly one admin user-management API, not two.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.database import get_db
from app.deps import User, get_admin_user
from app.errors import GapiError
from app.models import (
    Announcement,
    CreditTransaction,
    RedemptionCode,
    SystemSettings,
    UsageRecord,
)
from app.models import User as UserModel
from app.services.coin import DEFAULTS, set_setting

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Settings ────────────────────────────────────────────────────────────────


class SettingPut(BaseModel):
    key: str = Field(max_length=64)
    value: str = Field(min_length=0, max_length=4000)


class SettingsOut(BaseModel):
    coin_rate: str
    daily_bonus: str
    max_coin_per_user: str
    registration_bonus: str
    registration_open: str
    rate_multiplier: str
    token_package_rate: str = Field(..., alias="tokenPackageRate")
    defaults: dict[str, str]

    model_config = {"populate_by_name": True}


def _merged_settings(db: Session) -> dict[str, str]:
    stored = {row.key: row.value for row in db.scalars(select(SystemSettings)).all()}
    return {**DEFAULTS, **stored}


@router.get("/settings", response_model=SettingsOut)
def list_settings(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> SettingsOut:
    merged = _merged_settings(db)
    return SettingsOut(
        coin_rate=merged["coin_rate"],
        daily_bonus=merged["daily_bonus"],
        max_coin_per_user=merged["max_coin_per_user"],
        registration_bonus=merged["registration_bonus"],
        registration_open=merged["registration_open"],
        rate_multiplier=merged["rate_multiplier"],
        tokenPackageRate=str(app_settings.token_package_rate),
        defaults=DEFAULTS,
    )


@router.put("/settings", response_model=Any)
def update_settings(
    body: list[SettingPut],
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    # Validate the whole batch first, then write it as one commit: an unknown
    # key must not leave earlier keys half-applied behind a 422.
    for item in body:
        if item.key not in DEFAULTS:
            raise GapiError(422, "invalid_setting_key", f"Unknown setting: {item.key}")
    for item in body:
        set_setting(db, item.key, item.value, commit=False)
    db.commit()
    return {"ok": True, "count": len(body)}


# ── Announcements ───────────────────────────────────────────────────────────


class AnnouncementIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=0, max_length=10000)
    as_html: bool = False
    is_active: bool = True
    start_at: datetime | None = None
    end_at: datetime | None = None
    priority: int = Field(default=0, ge=-100, le=100)

    @field_validator("start_at", "end_at", mode="before")
    @classmethod
    def _naive_is_utc(cls, v):
        # The panel edits times without a zone suffix; they mean UTC.
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        if isinstance(v, str) and v and "+" not in v and "Z" not in v.upper():
            try:
                return datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
            except ValueError:
                return v  # let pydantic's own error fire
        return v


class AnnouncementPatch(BaseModel):
    """Partial update (the panel toggle flips exactly one field)."""

    is_active: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=0, max_length=10000)
    as_html: bool | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    priority: int | None = Field(default=None, ge=-100, le=100)


class AnnouncementOut(BaseModel):
    id: int
    title: str
    content: str
    as_html: bool
    is_active: bool  # the stored on/off switch
    live: bool  # switch AND schedule window, i.e. visible to users right now
    start_at: datetime | None = None
    end_at: datetime | None = None
    priority: int
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def of(cls, a: Announcement) -> "AnnouncementOut":
        return cls(
            id=a.id,
            title=a.title,
            content=a.content,
            as_html=a.as_html,
            is_active=a.is_active,
            live=a.active,
            start_at=a.start_at,
            end_at=a.end_at,
            priority=a.priority,
            created_at=a.created_at,
        )


def _active_filter(now: datetime):
    return (
        Announcement.is_active.is_(True),
        (Announcement.start_at.is_(None) | (Announcement.start_at <= now)),
        (Announcement.end_at.is_(None) | (Announcement.end_at >= now)),
    )


@router.get("/announcements", response_model=list[AnnouncementOut])
def list_announcements(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(Announcement).order_by(Announcement.priority.desc(), Announcement.created_at.desc())
    ).all()
    return [AnnouncementOut.of(a) for a in rows]


@router.get("/announcements/active", response_model=list[AnnouncementOut])
def active_announcements(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    rows = db.scalars(
        select(Announcement)
        .where(*_active_filter(now))
        .order_by(Announcement.priority.desc(), Announcement.created_at.desc())
    ).all()
    return [AnnouncementOut.of(a) for a in rows]


@router.get("/announcements/{aid}", response_model=AnnouncementOut)
def get_announcement(
    aid: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    rec = db.get(Announcement, aid)
    if rec is None:
        raise GapiError(404, "announcement_not_found", "Announcement not found")
    return AnnouncementOut.of(rec)


@router.post("/announcements", response_model=AnnouncementOut, status_code=201)
def create_announcement(
    body: AnnouncementIn,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    rec = Announcement(
        title=body.title,
        content=body.content,
        as_html=body.as_html,
        is_active=body.is_active,
        start_at=body.start_at,
        end_at=body.end_at,
        priority=body.priority,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return AnnouncementOut.of(rec)


@router.put("/announcements/{aid}", response_model=AnnouncementOut)
def replace_announcement(
    aid: int,
    body: AnnouncementIn,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    rec = db.get(Announcement, aid)
    if rec is None:
        raise GapiError(404, "announcement_not_found", "Announcement not found")
    rec.title = body.title
    rec.content = body.content
    rec.as_html = body.as_html
    rec.is_active = body.is_active
    rec.priority = body.priority
    rec.start_at = body.start_at
    rec.end_at = body.end_at
    db.commit()
    db.refresh(rec)
    return AnnouncementOut.of(rec)


@router.patch("/announcements/{aid}", response_model=AnnouncementOut)
def patch_announcement(
    aid: int,
    body: AnnouncementPatch,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    rec = db.get(Announcement, aid)
    if rec is None:
        raise GapiError(404, "announcement_not_found", "Announcement not found")
    for field_name, value in body.model_dump(exclude_unset=True).items():
        setattr(rec, field_name, value)
    db.commit()
    db.refresh(rec)
    return AnnouncementOut.of(rec)


@router.delete("/announcements/{aid}", status_code=204)
def delete_announcement(
    aid: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    rec = db.get(Announcement, aid)
    if rec is None:
        raise GapiError(404, "announcement_not_found", "Announcement not found")
    db.delete(rec)
    db.commit()


# ── Global stats ────────────────────────────────────────────────────────────


class StatsOut(BaseModel):
    total_users: int
    active_users: int
    requests_30d: int
    tokens_30d: int
    spend_30d: str
    signups_7d: int
    redeems_30d: int
    daily: list[dict]  # [{date, requests, tokens, cost}] last 7 days
    top_models: list[dict]  # [{model, requests, tokens, cost}]
    top_users: list[dict]  # [{email, requests, cost}]


@router.get("/stats", response_model=StatsOut)
def global_stats(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> StatsOut:
    now = datetime.now(timezone.utc)
    since_30 = now - timedelta(days=30)
    since_7 = now - timedelta(days=7)

    total_users = db.scalar(select(func.count()).select_from(UserModel)) or 0
    active_users = db.scalar(
        select(func.count()).select_from(UserModel).where(UserModel.is_active.is_(True))
    ) or 0
    signups_7d = db.scalar(
        select(func.count()).select_from(UserModel).where(UserModel.created_at >= since_7)
    ) or 0

    req, tok, spend = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(UsageRecord.total_tokens), 0),
            func.coalesce(func.sum(UsageRecord.gavincoin_cost), 0),
        ).where(UsageRecord.created_at >= since_30)
    ).one()

    redeems_30d = db.scalar(
        select(func.count())
        .select_from(CreditTransaction)
        .where(CreditTransaction.tx_type == "redeem", CreditTransaction.created_at >= since_30)
    ) or 0

    daily = [
        {"date": str(day), "requests": int(c or 0), "tokens": int(t or 0), "cost": str(cost or 0)}
        for day, c, t, cost in db.execute(
            select(
                func.date(UsageRecord.created_at),
                func.count(),
                func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                func.coalesce(func.sum(UsageRecord.gavincoin_cost), 0),
            )
            .where(UsageRecord.created_at >= since_7)
            .group_by(func.date(UsageRecord.created_at))
            .order_by(func.date(UsageRecord.created_at))
        ).all()
    ]

    top_models = [
        {"model": m, "requests": int(c or 0), "tokens": int(t or 0), "cost": str(cost or 0)}
        for m, c, t, cost in db.execute(
            select(
                UsageRecord.model,
                func.count(),
                func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                func.coalesce(func.sum(UsageRecord.gavincoin_cost), 0),
            )
            .where(UsageRecord.created_at >= since_30)
            .group_by(UsageRecord.model)
            .order_by(func.sum(UsageRecord.gavincoin_cost).desc())
            .limit(5)
        ).all()
    ]

    top_users = [
        {"email": e, "requests": int(c or 0), "cost": str(cost or 0)}
        for e, c, cost in db.execute(
            select(
                UserModel.email,
                func.count(),
                func.coalesce(func.sum(UsageRecord.gavincoin_cost), 0),
            )
            .join(UsageRecord, UsageRecord.user_id == UserModel.id)
            .where(UsageRecord.created_at >= since_30)
            .group_by(UserModel.id)
            .order_by(func.sum(UsageRecord.gavincoin_cost).desc())
            .limit(5)
        ).all()
    ]

    return StatsOut(
        total_users=total_users,
        active_users=active_users,
        requests_30d=int(req or 0),
        tokens_30d=int(tok or 0),
        spend_30d=str(spend or 0),
        signups_7d=signups_7d,
        redeems_30d=redeems_30d,
        daily=daily,
        top_models=top_models,
        top_users=top_users,
    )
