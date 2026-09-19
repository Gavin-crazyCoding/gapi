"""User panel: balance, token packages, ledger, usage, dashboard.

All JWT-protected (the panel is a browser surface; gapi keys are for /v1).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import Announcement, CreditTransaction, Pricing, TokenPackage, UsageRecord, User
from app.services import billing
from app.services.catalog import get_catalog
from app.schemas.key import (
    BalanceOut,
    DashboardOut,
    ModelListOut,
    ModelOut,
    PackageOut,
    PackagePurchase,
    PasswordChange,
    RedeemIn,
    RedeemOut,
    TransactionOut,
    UsageDayOut,
    UsageModelOut,
    UsageRecentOut,
    RoutingStrategyOut,
)

router = APIRouter(prefix="/user", tags=["user"])

_ZERO = Decimal("0")


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


@router.get("/routing", response_model=RoutingStrategyOut)
async def get_routing(user: User = Depends(get_current_user)) -> RoutingStrategyOut:
    return RoutingStrategyOut(strategy=user.routing_strategy)

@router.post("/routing", response_model=RoutingStrategyOut)
async def set_routing(body: RoutingStrategyOut, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> RoutingStrategyOut:
    user.routing_strategy = body.strategy
    db.add(user)
    db.commit()
    db.refresh(user)
    return RoutingStrategyOut(strategy=user.routing_strategy)

@router.post("/checkin")
def daily_checkin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Daily check-in: credits the daily bonus once per rolling 24h window.

    The panel calls this on load — no need to log out and back in to collect.
    """
    from app.services import coin
    granted = coin.award_daily_bonus(db, user)
    return {
        "checked_in": granted is not None,
        "bonus": str(granted) if granted is not None else None,
        "balance": str(user.gavincoin_balance),
    }


@router.post("/redeem", response_model=RedeemOut)
def redeem_code(
    body: RedeemIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedeemOut:
    """Redeem a prepaid code into GavinCoin."""
    new_balance, amount = billing.redeem(db, user, body.code)
    return RedeemOut(redeemed=amount, balance=new_balance)


@router.post("/password", status_code=200)
def change_password(
    body: PasswordChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the account password (requires the current one)."""
    from app.errors import GapiError
    from app.security import hash_password, verify_password
    if not verify_password(body.old_password, user.password_hash):
        raise GapiError(401, "invalid_credentials", "当前密码不正确")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"detail": "密码已更新"}


@router.get("/balance", response_model=BalanceOut)
def balance(user: User = Depends(get_current_user)) -> BalanceOut:
    return BalanceOut(balance=user.gavincoin_balance, currency="GavinCoin")


@router.get("/packages", response_model=list[PackageOut])
def packages(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(TokenPackage)
        .where(TokenPackage.user_id == user.id)
        .order_by(TokenPackage.created_at.desc())
    ).all()


@router.get("/models", response_model=ModelListOut)
async def models(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    force: bool = Query(False),
):
    """Models the caller can actually call right now, with gapi's price each.

    The list comes from upstream (`/v1/models?execution_status=ready`), not
    from a table — key health changes it minute to minute. Prices come from
    gapi's own `pricing` table, so the two are joined here rather than stored
    together.
    """
    catalog = await get_catalog(force=force)
    priced = {
        row.model: (row.input_per_1k, row.output_per_1k)
        for row in db.scalars(select(Pricing)).all()
    }

    out: list[ModelOut] = []
    for entry in catalog.ready():
        model_id = entry.get("id")
        if not model_id:
            continue
        rates = priced.get(model_id)
        params = entry.get("supported_parameters") or []
        out.append(
            ModelOut(
                id=model_id,
                name=entry.get("name"),
                context_window=entry.get("context_window") or entry.get("context_length"),
                input_per_1k=rates[0] if rates else billing.FALLBACK_INPUT,
                output_per_1k=rates[1] if rates else billing.FALLBACK_OUTPUT,
                priced=rates is not None,
                is_router=entry.get("execution_status") is None,
                supports_tools="tools" in params,
                supports_streaming="stream" in params,
            )
        )
    out.sort(key=lambda m: (not m.is_router, m.id))
    return ModelListOut(models=out, cached_age_seconds=int(catalog.age))


@router.post("/packages", response_model=PackageOut, status_code=201)
def buy_package(
    body: PackagePurchase,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Exchange GavinCoin for prepaid tokens at the configured coin rate."""
    from app.services import coin
    rate = int(coin.get_coin_rate(db))
    return billing.buy_package(db, user, body.cost, rate, body.valid_days)


@router.get("/transactions", response_model=list[TransactionOut])
def transactions(
    limit: int = Query(20, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(CreditTransaction)
        .where(CreditTransaction.user_id == user.id)
        .order_by(CreditTransaction.created_at.desc(), CreditTransaction.id.desc())
        .limit(limit)
    ).all()


def _usage_by_day(db: Session, user_id: int, days: int) -> list[UsageDayOut]:
    rows = db.execute(
        select(
            func.date(UsageRecord.created_at).label("day"),
            func.sum(UsageRecord.prompt_tokens),
            func.sum(UsageRecord.completion_tokens),
            func.sum(UsageRecord.total_tokens),
            func.sum(UsageRecord.gavincoin_cost),
        )
        .where(UsageRecord.user_id == user_id, UsageRecord.created_at >= _since(days))
        .group_by(func.date(UsageRecord.created_at))
        .order_by(func.date(UsageRecord.created_at))
    ).all()
    return [
        UsageDayOut(
            date=str(day),
            prompt_tokens=int(p or 0),
            completion_tokens=int(c or 0),
            total_tokens=int(t or 0),
            cost=Decimal(str(cost or 0)),
        )
        for day, p, c, t, cost in rows
    ]


def _usage_by_model(db: Session, user_id: int, days: int) -> list[UsageModelOut]:
    rows = db.execute(
        select(
            UsageRecord.model,
            func.sum(UsageRecord.prompt_tokens),
            func.sum(UsageRecord.completion_tokens),
            func.sum(UsageRecord.total_tokens),
            func.sum(UsageRecord.gavincoin_cost),
        )
        .where(UsageRecord.user_id == user_id, UsageRecord.created_at >= _since(days))
        .group_by(UsageRecord.model)
        .order_by(func.sum(UsageRecord.total_tokens).desc())
    ).all()
    return [
        UsageModelOut(
            model=model,
            prompt_tokens=int(p or 0),
            completion_tokens=int(c or 0),
            total_tokens=int(t or 0),
            cost=Decimal(str(cost or 0)),
        )
        for model, p, c, t, cost in rows
    ]


@router.get("/usage", response_model=list[UsageDayOut])
def usage_by_day(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _usage_by_day(db, user.id, days)


@router.get("/usage/models", response_model=list[UsageModelOut])
def usage_by_model(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _usage_by_model(db, user.id, days)


@router.get("/usage/recent", response_model=list[UsageRecentOut])
def usage_recent(
    limit: int = Query(20, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(UsageRecord)
        .where(UsageRecord.user_id == user.id)
        .order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc())
        .limit(limit)
    ).all()


@router.get("/announcements", response_model=list[Any])
def my_announcements(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Active site-wide announcements for the current user (any role).

    Returns the newest unread announcement first so the panel can pop one modal.
    A `read` marker is stored per-user so an announcement shows once.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    rows = db.scalars(
        select(Announcement)
        .where(
            Announcement.is_active.is_(True),
            (Announcement.start_at.is_(None) | (Announcement.start_at <= now)),
            (Announcement.end_at.is_(None) | (Announcement.end_at >= now)),
        )
        .order_by(Announcement.priority.desc(), Announcement.created_at.desc())
    ).all()
    # The user's last_announcement_at is the newest announcement id they've seen.
    last_seen = user.last_announcement_at
    fresh = []
    for a in rows:
        # Treat the marker as a monotonic clock: anything created after the
        # marker is unread. If the marker is null everything is unread.
        if last_seen is None or (a.created_at.replace(tzinfo=timezone.utc) if a.created_at.tzinfo is None else a.created_at) > last_seen:
            fresh.append(a)
    # Return the newest unread one (or nothing if all read).
    return [
        {
            "id": a.id,
            "title": a.title,
            "content": a.content,
            "as_html": a.as_html,
            "priority": a.priority,
            "created_at": a.created_at.isoformat(),
        }
        for a in fresh
    ]


@router.post("/announcements/read", status_code=200)
def mark_announcements_read(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark all announcements seen: clear the user's unread marker."""
    from datetime import datetime, timezone
    user.last_announcement_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True}


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    by_day = _usage_by_day(db, user.id, 30)
    by_model = _usage_by_model(db, user.id, 30)
    recent = db.scalars(
        select(UsageRecord)
        .where(UsageRecord.user_id == user.id)
        .order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc())
        .limit(10)
    ).all()
    totals = db.execute(
        select(func.count(), func.sum(UsageRecord.total_tokens), func.sum(UsageRecord.gavincoin_cost))
        .where(UsageRecord.user_id == user.id, UsageRecord.created_at >= _since(30))
    ).one()
    count, tokens, spend = totals
    return DashboardOut(
        balance=user.gavincoin_balance,
        currency="GavinCoin",
        total_tokens_30d=int(tokens or 0),
        total_spend_30d=Decimal(str(spend or 0)),
        request_count_30d=int(count or 0),
        usage_by_day=by_day,
        usage_by_model=by_model,
        recent=[UsageRecentOut.model_validate(r) for r in recent],
    )
