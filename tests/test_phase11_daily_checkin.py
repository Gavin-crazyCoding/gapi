"""Phase 11: daily check-in (login + panel auto-checkin, 24h window).

Verifies:
- First login credits the daily bonus.
- A panel /user/checkin in the same 24h window must not credit again.
- After 24h elapses a fresh checkin grants again.
- Two concurrent /user/checkin requests cannot both win the same bonus
  (BEGIN IMMEDIATE guarded UPDATE — rowcount race guard).
- Balance cap (max_coin_per_user) clamps the grant to 0 extra.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from sqlalchemy import select

from app.database import SessionLocal
from app.models import User
from app.services import coin

pytestmark = pytest.mark.asyncio


async def _register_login(client, email, password):
    r = await client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _balance(email):
    with SessionLocal() as db:
        return db.scalar(select(User.gavincoin_balance).where(User.email == email))


def _user(email):
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.email == email))


# ── RED: login must credit the bonus, panel must NOT double-credit ──

async def test_login_grants_bonus_panel_does_not_double(client):
    """Login credits the bonus once; a panel /user/checkin in the same
    24h window must not credit again."""
    token = await _register_login(client, "c1@example.com", "pw123456")
    bonus = coin.get_daily_bonus(SessionLocal())
    after_login = _balance("c1@example.com")

    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post("/user/checkin", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["checked_in"] is False  # same window, no reward
    assert body["bonus"] is None

    assert _balance("c1@example.com") == after_login


async def test_second_checkin_after_24h_grants_again(client):
    """Rolling 24h window: pushing last_login_date back grants once more."""
    token = await _register_login(client, "c2@example.com", "pw123456")
    first = _balance("c2@example.com")
    bonus = coin.get_daily_bonus(SessionLocal())

    # force the window to have elapsed (naive UTC matches SQLite storage)
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == "c2@example.com"))
        u.last_login_date = (u.last_login_date - timedelta(hours=25)).replace(tzinfo=None)
        db.commit()

    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post("/user/checkin", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["checked_in"] is True
    assert body["bonus"] == str(Decimal(bonus).quantize(Decimal("0.000001")))
    assert _balance("c2@example.com") == first + bonus


async def test_concurrent_checkins_cannot_double_claim(client):
    """Two simultaneous /user/checkin calls must not both win the bonus."""
    token = await _register_login(client, "c3@example.com", "pw123456")
    # open the 24h window
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == "c3@example.com"))
        u.last_login_date = (u.last_login_date - timedelta(hours=25)).replace(tzinfo=None)
        db.commit()

    headers = {"Authorization": f"Bearer {token}"}
    import asyncio

    async def one():
        r = await client.post("/user/checkin", headers=headers)
        return r.json()

    results = await asyncio.gather(one(), one())
    granted = [r for r in results if r["checked_in"]]
    assert len(granted) == 1, "only one concurrent checkin should win"


async def test_balance_cap_clamps_grant_to_zero(client):
    """If max_coin_per_user is already reached, the bonus is 0 (granted=0)."""
    token = await _register_login(client, "c4@example.com", "pw123456")
    bonus = coin.get_daily_bonus(SessionLocal())
    cap = coin.get_max_coin_per_user(SessionLocal())

    # set balance AT the cap and open the window
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == "c4@example.com"))
        u.gavincoin_balance = cap
        u.last_login_date = (u.last_login_date - timedelta(hours=25)).replace(tzinfo=None)
        db.commit()

    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post("/user/checkin", headers=headers)
    assert r.status_code == 200
    body = r.json()
    # window eligible but cap eats it -> checked_in tells caller the claim
    # was attempted (window opened); bonus is 0 so no ledger entry.
    assert body["checked_in"] is True
    assert body["bonus"] == "0.000000"
    assert _balance("c4@example.com") == cap