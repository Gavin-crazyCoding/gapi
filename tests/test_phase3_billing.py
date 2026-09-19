"""Phase 3: billing arithmetic and the atomicity of the reserve."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.errors import GapiError
from app.models import CreditTransaction, Pricing, User
from app.services import billing


@pytest.fixture()
def funded(client, registered_admin):
    """The admin account, priced and topped up to a known balance."""
    db = SessionLocal()
    try:
        billing.seed_default_pricing(db)
        user = db.scalar(select(User).where(User.email == "admin@example.com"))
        # Normalise to exactly 100 regardless of the signup bonus.
        billing.grant(db, user, Decimal("100") - user.gavincoin_balance, "test setup")
        yield user.id
    finally:
        db.close()


def _balance(uid: int) -> Decimal:
    db = SessionLocal()
    try:
        return db.scalar(select(User.gavincoin_balance).where(User.id == uid))
    finally:
        db.close()


def test_rates_fall_back_when_model_unpriced(funded):
    db = SessionLocal()
    try:
        rates = billing.get_rates(db, "some-model-nobody-priced")
        assert rates.input_per_1k == billing.FALLBACK_INPUT
        assert rates.output_per_1k == billing.FALLBACK_OUTPUT
    finally:
        db.close()


def test_cost_matches_the_documented_formula(funded):
    db = SessionLocal()
    try:
        rates = billing.get_rates(db, "gemini-2.5-flash")
    finally:
        db.close()
    # 2000 in @ 0.0001/1k + 3000 out @ 0.0004/1k = 0.0002 + 0.0012
    assert rates.cost(2000, 3000) == Decimal("0.001400")


def test_reserve_rounds_up_so_settlement_never_exceeds_the_check(funded):
    """The hold must be >= the eventual charge for the same token counts."""
    rates = billing.Rates(Decimal("0.005"), Decimal("0.015"))
    for max_tokens in (1, 7, 33, 4096):
        for prompt in (0, 1, 999):
            hold = billing.estimate_ceiling(rates, max_tokens, prompt)
            actual = rates.cost(prompt, max_tokens)
            assert hold >= actual, (max_tokens, prompt, hold, actual)


def test_reserve_then_settle_nets_to_the_actual_cost(funded):
    uid = funded
    before = _balance(uid)
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        res = billing.reserve(db, user, "gpt-4o", 1000, 0, "req-net")
        # The hold is taken immediately.
        assert _balance(uid) == before - res.amount
        billing.settle(
            db,
            res,
            prompt_tokens=1000,
            completion_tokens=1000,
            endpoint="/v1/chat/completions",
            api_key_id=None,
            duration_ms=5,
        )
    finally:
        db.close()
    # 1000 @0.005/1k + 1000 @0.015/1k = 0.005 + 0.015 = 0.02
    assert before - _balance(uid) == Decimal("0.020000")


def test_settle_can_debit_more_than_reserved(funded):
    """An under-estimate is corrected at settlement, not silently absorbed."""
    uid = funded
    before = _balance(uid)
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        res = billing.reserve(db, user, "gpt-4o", 10, 0, "req-over")
        billing.settle(
            db,
            res,
            prompt_tokens=0,
            completion_tokens=100_000,  # far beyond the 10-token hold
            endpoint="/v1/chat/completions",
            api_key_id=None,
            duration_ms=5,
        )
    finally:
        db.close()
    assert before - _balance(uid) == Decimal("1.500000")  # 100k @0.015/1k


def test_reserve_rejects_when_balance_is_short(funded):
    uid = funded
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        billing.grant(db, user, -user.gavincoin_balance, "drain")
        with pytest.raises(GapiError) as exc:
            billing.reserve(db, user, "gpt-4o", 4096, 0, "req-broke")
        assert exc.value.status_code == 402
        assert exc.value.code == "insufficient_balance"
    finally:
        db.close()


def test_concurrent_reserves_cannot_overdraw(funded):
    """Two threads racing on the same account must not both succeed.

    The balance is set so exactly one of the two holds fits. Without
    BEGIN IMMEDIATE both would read a sufficient balance and both would pass,
    driving the account negative.
    """
    uid = funded
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        # One reserve of 4096 output tokens on gpt-4o costs 0.06144.
        target = Decimal("0.070000")
        billing.grant(db, user, target - user.gavincoin_balance, "tight budget")
    finally:
        db.close()

    results: list[str] = []
    barrier = threading.Barrier(2)

    def attempt(tag: str):
        session = SessionLocal()
        try:
            u = session.get(User, uid)
            barrier.wait(timeout=5)
            billing.reserve(session, u, "gpt-4o", 4096, 0, f"race-{tag}")
            results.append("ok")
        except GapiError as exc:
            results.append(f"rejected-{exc.status_code}")
        except Exception as exc:  # lock contention surfaces here, not as an overdraw
            results.append(f"error-{type(exc).__name__}")
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert results.count("ok") == 1, results
    # Whatever happened to the loser, the account must never go negative.
    assert _balance(uid) >= 0


def test_grant_writes_a_ledger_entry(funded):
    uid = funded
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        after = billing.grant(db, user, Decimal("12.5"), "manual top-up")
        assert after == _balance(uid)
        tx = db.scalars(
            select(CreditTransaction)
            .where(CreditTransaction.user_id == uid)
            .order_by(CreditTransaction.id.desc())
        ).first()
        assert tx.tx_type == "grant"
        assert tx.amount == Decimal("12.500000")
        assert tx.balance_after == after
        assert tx.note == "manual top-up"
    finally:
        db.close()


def test_buy_package_debits_and_credits_tokens(funded):
    """Spending GavinCoin yields cost * rate * 1000 prepaid tokens."""
    uid = funded
    before = _balance(uid)
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        pkg = billing.buy_package(db, user, Decimal("5"), tokens_per_coin=10)
    finally:
        db.close()
    assert pkg.total_tokens == 50_000
    assert pkg.remaining_tokens == 50_000
    assert pkg.expires_at is None
    assert before - _balance(uid) == Decimal("5.000000")


def test_buy_package_refuses_when_short(funded):
    uid = funded
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        with pytest.raises(GapiError) as exc:
            billing.buy_package(db, user, Decimal("99999"), tokens_per_coin=10)
        assert exc.value.status_code == 402
        # The failed purchase must not have moved the balance.
        assert _balance(uid) == Decimal("100.000000")
    finally:
        db.close()


def test_settled_usage_draws_down_a_package(funded):
    """Prepaid tokens are consumed oldest-first as requests settle."""
    uid = funded
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        pkg = billing.buy_package(db, user, Decimal("1"), tokens_per_coin=10)  # 10k tokens
        pkg_id = pkg.id
        res = billing.reserve(db, user, "gpt-4o", 1000, 0, "req-pkg")
        billing.settle(
            db,
            res,
            prompt_tokens=400,
            completion_tokens=600,
            endpoint="/v1/chat/completions",
            api_key_id=None,
            duration_ms=1,
        )
    finally:
        db.close()

    db = SessionLocal()
    try:
        from app.models import TokenPackage

        assert db.get(TokenPackage, pkg_id).remaining_tokens == 10_000 - 1000
    finally:
        db.close()


# ── prepaid token packages cover requests before GavinCoin ──────────────────

def test_package_covers_whole_request_no_coin_spent(funded):
    """A big enough package means zero GavinCoin hold and zero charge."""
    uid = funded
    before = _balance(uid)
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        billing.buy_package(db, user, Decimal("1"), tokens_per_coin=10)  # 10k tokens
        res = billing.reserve(db, user, "gpt-4o", 1000, 500, "req-cover")
        assert res.amount == 0  # package alone underwrites the 1500-token estimate
        billing.settle(
            db, res, prompt_tokens=500, completion_tokens=1000,
            endpoint="/v1/chat/completions", api_key_id=None, duration_ms=5,
        )
    finally:
        db.close()
    assert _balance(uid) == before - Decimal("1.000000")  # only the package purchase

    db = SessionLocal()
    try:
        from app.models import TokenPackage, UsageRecord
        pkg = db.scalar(select(TokenPackage).where(TokenPackage.user_id == uid))
        assert pkg.remaining_tokens == 10_000 - 1500
        row = db.scalar(select(UsageRecord).where(UsageRecord.request_id == "req-cover"))
        assert row.package_tokens == 1500
        assert row.gavincoin_cost == 0
    finally:
        db.close()


def test_package_partial_coverage_bills_only_overflow(funded):
    """Package covers input-first; the uncovered output tail is coin-billed."""
    uid = funded
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        # Package holds only 1000 tokens; request uses 400 in + 2000 out.
        billing.buy_package(db, user, Decimal("0.1"), tokens_per_coin=10)  # 1000 tok
        res = billing.reserve(db, user, "gpt-4o", 2000, 400, "req-partial")
        # Estimate = 2400 tokens; package covers 1000, all on the output tail,
        # so the hold is 1400 output tokens @0.015/1k = 0.021.
        assert res.amount == Decimal("0.021000")
        billing.settle(
            db, res, prompt_tokens=400, completion_tokens=2000,
            endpoint="/v1/chat/completions", api_key_id=None, duration_ms=5,
        )
    finally:
        db.close()

    db = SessionLocal()
    try:
        from app.models import UsageRecord
        row = db.scalar(select(UsageRecord).where(UsageRecord.request_id == "req-partial"))
        # 400 input + 600 of the output came from the package; 1400 output billed.
        assert row.package_tokens == 1000
        assert row.gavincoin_cost == Decimal("0.021000")  # 1400 * 0.015/1k
    finally:
        db.close()


def test_failed_request_releases_held_package_tokens(funded):
    """Settlement with zero real usage refunds both the coin hold and package."""
    uid = funded
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        pkg = billing.buy_package(db, user, Decimal("1"), tokens_per_coin=10)
        res = billing.reserve(db, user, "gpt-4o", 1000, 0, "req-fail")
        assert res.amount == 0
        billing.settle(
            db, res, prompt_tokens=0, completion_tokens=0,
            endpoint="/v1/chat/completions", api_key_id=None,
            duration_ms=5, status="upstream_error",
        )
        pkg_id = pkg.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        from app.models import TokenPackage
        assert db.get(TokenPackage, pkg_id).remaining_tokens == 10_000
    finally:
        db.close()


def test_concurrent_package_draws_cannot_overshoot(funded):
    """Two requests racing for a 1000-token package together cover at most 1000."""
    uid = funded
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        billing.buy_package(db, user, Decimal("0.1"), tokens_per_coin=10)
    finally:
        db.close()

    covered = []
    barrier = threading.Barrier(2)

    def attempt(tag):
        session = SessionLocal()
        try:
            u = session.get(User, uid)
            barrier.wait(timeout=5)
            res = billing.reserve(session, u, "gpt-4o", 800, 0, f"race-pkg-{tag}")
            covered.append(sum(h for _, h in res.package_holds))
            billing.settle(
                session, res, prompt_tokens=0, completion_tokens=800,
                endpoint="/v1/chat/completions", api_key_id=None, duration_ms=1,
            )
        except Exception:
            covered.append(0)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    db = SessionLocal()
    try:
        from app.models import TokenPackage
        pkg = db.scalar(select(TokenPackage).where(TokenPackage.user_id == uid))
        assert pkg.remaining_tokens >= 0
        assert sum(covered) <= 1000  # the package can never be overspent
    finally:
        db.close()


# ── subscription-style quota plans ──────────────────────────────────────────

def test_quota_plan_carries_expiry_and_status(funded):
    from datetime import timedelta
    from app.models import TokenPackage

    uid = funded
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        plan = billing.buy_package(
            db, user, Decimal("1"), tokens_per_coin=100, valid_days=30
        )  # 100k tokens, 30-day period
        assert plan.total_tokens == 100_000
        assert plan.status == "active"
        assert plan.expires_at is not None
        assert timedelta(days=29) < (plan.expires_at.replace(tzinfo=None) - plan.created_at.replace(tzinfo=None)) < timedelta(days=31)

        # Drain it: status flips to exhausted.
        res = billing.reserve(db, user, "gpt-4o", 50_000, 50_000, "req-drain")
        billing.settle(db, res, prompt_tokens=50_000, completion_tokens=50_000,
                       endpoint="/v1/chat/completions", api_key_id=None, duration_ms=1)
        db.refresh(plan)
        assert plan.remaining_tokens == 0
        assert plan.status == "exhausted"
        pid = plan.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        plan = db.get(TokenPackage, pid)
        assert plan.used_tokens == 100_000

        # An expired plan is skipped even with remaining tokens.
        from datetime import datetime, timezone, timedelta as td
        plan.expires_at = datetime.now(timezone.utc) - td(days=1)
        db.commit()
        assert plan.status == "expired"

        user = db.get(User, uid)
        billing.grant(db, user, Decimal("1"), "top up for expired-plan test")
        res = billing.reserve(db, user, "gpt-4o", 100, 0, "req-after-expiry")
        # The expired plan covered nothing → the full output estimate is held.
        assert res.amount == Decimal("100") * Decimal("0.015") / 1000
    finally:
        db.close()
