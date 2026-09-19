"""GavinCoin billing: atomic pre-authorisation, then settlement.

Money moves in two steps, because the real cost is only known after the
response has been read:

1. ``reserve()`` — before forwarding, debit an upper bound derived from
   ``max_tokens``. This is the only place that can reject a request (402).
2. ``settle()`` — after the response, refund ``reserved - actual`` (or debit
   the excess if the estimate was too low) and write the usage row.

Both steps run inside a single SQLite write transaction opened with
``BEGIN IMMEDIATE``, so two concurrent requests from the same account cannot
both see a sufficient balance and both pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.errors import GapiError, payment_required
from app.models import CreditTransaction, Pricing, RedemptionCode, TokenPackage, UsageRecord, User

log = logging.getLogger("gapi.billing")

# Money is stored as NUMERIC(18, 6); every computed amount is quantised to the
# same scale so reserve/refund arithmetic is exact and always nets to zero.
CENT = Decimal("0.000001")

# Used when a request omits max_tokens, matching the OpenAI default ceiling we
# are willing to underwrite.
DEFAULT_MAX_TOKENS = 4096

# Fallback when a model has no row in `pricing`. Deliberately the gpt-4o rate:
# over-charging an unknown model is safer than serving it for free.
FALLBACK_INPUT = Decimal("0.005")
FALLBACK_OUTPUT = Decimal("0.015")

DEFAULT_PRICING: dict[str, tuple[str, str]] = {
    "gpt-4o": ("0.005", "0.015"),
    "gemini-2.5-flash": ("0.0001", "0.0004"),
    "claude-sonnet-4-5": ("0.003", "0.015"),
}

# Media endpoints produce no token stream; they bill per call (design D3).
# A pricing row's flat_fee_coin overrides these endpoint defaults. These base
# prices sit UNDER the global rate_multiplier (a multiplier of 100 puts a
# speech call at ◎2, not ◎200).
FLAT_FEE_ENDPOINTS: dict[str, Decimal] = {
    "/v1/images/generations": Decimal("0.05"),
    "/v1/videos/generations": Decimal("0.5"),
    "/v1/audio/speech": Decimal("0.02"),
    "/v1/audio/transcriptions": Decimal("0.02"),
}


def q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Rates:
    input_per_1k: Decimal
    output_per_1k: Decimal

    def cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        return q(
            Decimal(prompt_tokens) / 1000 * self.input_per_1k
            + Decimal(completion_tokens) / 1000 * self.output_per_1k
        )


# The pricing table is tiny and changes about never, but get_rates runs on
# every proxied request. Cache a whole-table snapshot; writes through the CLI
# or seed take effect within _PRICING_CACHE_TTL seconds.
_PRICING_CACHE_TTL = 60.0
# model -> (input_per_1k, output_per_1k, flat_fee_coin|None). Plain values,
# never ORM instances — those detach the moment their session closes.
_pricing_cache: tuple[float, dict[str, tuple[Decimal, Decimal, Decimal | None]]] | None = None


def invalidate_pricing() -> None:
    """Drop the pricing snapshot (tests, and after pricing writes)."""
    global _pricing_cache
    _pricing_cache = None


def _pricing_table(db: Session) -> dict[str, tuple[Decimal, Decimal, Decimal | None]]:
    global _pricing_cache
    import time as _time
    if _pricing_cache is not None and _time.monotonic() - _pricing_cache[0] < _PRICING_CACHE_TTL:
        return _pricing_cache[1]
    rows = {
        row.model: (row.input_per_1k, row.output_per_1k, row.flat_fee_coin)
        for row in db.scalars(select(Pricing)).all()
    }
    _pricing_cache = (_time.monotonic(), rows)
    return rows


def get_rates(db: Session, model: str) -> Rates:
    from app.services import coin
    m = coin.get_rate_multiplier(db)
    row = _pricing_table(db).get(model)
    if row is not None:
        return Rates(row[0] * m, row[1] * m)
    return Rates(FALLBACK_INPUT * m, FALLBACK_OUTPUT * m)


def get_flat_fee(db: Session, model: str, endpoint: str) -> Decimal | None:
    """Per-call price for a media endpoint, or None when tokens apply."""
    if endpoint not in FLAT_FEE_ENDPOINTS:
        return None
    from app.services import coin
    m = coin.get_rate_multiplier(db)
    row = _pricing_table(db).get(model)
    if row is not None and row[2] is not None:
        return row[2] * m
    return FLAT_FEE_ENDPOINTS[endpoint] * m


def seed_default_pricing(db: Session) -> int:
    """Insert the documented default rates for models not already priced."""
    added = 0
    for model, (inp, outp) in DEFAULT_PRICING.items():
        if db.scalar(select(Pricing.id).where(Pricing.model == model)) is None:
            db.add(Pricing(model=model, input_per_1k=Decimal(inp), output_per_1k=Decimal(outp)))
            added += 1
    if added:
        db.commit()
        invalidate_pricing()
    return added


# ── reservation ─────────────────────────────────────────────────────────────

@dataclass
class Reservation:
    """A held amount awaiting settlement.

    ``amount`` is only the GavinCoin hold for tokens the prepaid packages do
    not cover. ``package_holds`` records (package id, tokens) that were drawn
    out of the packages up front and must either be consumed or released at
    settlement.
    """

    user_id: int
    amount: Decimal
    request_id: str
    rates: Rates
    model: str
    package_holds: list[tuple[int, int]]
    prompt_estimate: int = 0
    max_tokens: int = 0
    # When set (media endpoints), this is the per-call price; token math and
    # package draws do not apply.
    flat_fee: Decimal | None = None


def _active_packages(db: Session, user_id: int) -> list[TokenPackage]:
    """Packages with a balance that have not expired, oldest first."""
    rows = db.scalars(
        select(TokenPackage)
        .where(TokenPackage.user_id == user_id, TokenPackage.remaining_tokens > 0)
        .order_by(TokenPackage.created_at, TokenPackage.id)
    ).all()
    return [p for p in rows if not p.is_expired]


def _draw_from_packages(
    packages: list[TokenPackage], tokens: int
) -> tuple[list[tuple[int, int]], int]:
    """Draw ``tokens`` across packages, mutating remaining_tokens in place.

    Returns the per-package draws ``[(package_id, taken), …]`` and the total
    number of tokens actually covered (clamped at the available balance).
    """
    draws: list[tuple[int, int]] = []
    remaining = max(0, tokens)
    for pkg in packages:
        if remaining <= 0:
            break
        take = min(pkg.remaining_tokens, remaining)
        if take <= 0:
            continue
        pkg.remaining_tokens -= take
        draws.append((pkg.id, take))
        remaining -= take
    return draws, tokens - remaining


def estimate_ceiling(rates: Rates, max_tokens: int, prompt_tokens: int = 0) -> Decimal:
    """Worst-case spend for a request that may emit ``max_tokens`` of output."""
    est = (
        Decimal(prompt_tokens) / 1000 * rates.input_per_1k
        + Decimal(max_tokens) / 1000 * rates.output_per_1k
    )
    # Round up: the reserve must never be smaller than the eventual charge for
    # the same token counts, or settlement would debit beyond what was checked.
    return est.quantize(CENT, rounding=ROUND_CEILING)


def reserve(
    db: Session,
    user: User,
    model: str,
    max_tokens: int | None,
    prompt_tokens: int,
    request_id: str,
    flat_fee: Decimal | None = None,
) -> Reservation:
    """Atomically hold the worst-case cost. Raises GapiError(402) if short.

    Prepaid token packages cover the estimate first (input side first, then
    output); only the uncovered tail needs a GavinCoin hold. Package tokens
    are drawn inside the same ``BEGIN IMMEDIATE`` transaction as the coin hold,
    so two concurrent requests cannot both spend the same prepaid balance.
    """
    rates = get_rates(db, model)
    output_limit = max_tokens or DEFAULT_MAX_TOKENS
    est_total = prompt_tokens + output_limit

    # BEGIN IMMEDIATE takes SQLite's write lock up front, so package draws, the
    # balance read in the WHERE clause and the write cannot interleave with
    # another request's trio.
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        if flat_fee is not None:
            # Media call: the whole per-call price comes out of GavinCoin;
            # prepaid *token* packages cannot cover a non-token product.
            holds = []
            uncovered = 0
            amount = q(flat_fee)
        else:
            holds, covered = _draw_from_packages(_active_packages(db, user.id), est_total)
            uncovered = est_total - covered
            # Packages cover input-first, so the uncovered tail is all output.
            amount = q(Decimal(uncovered) / 1000 * rates.output_per_1k) if uncovered else Decimal("0")

        if amount > 0:
            result = db.execute(
                update(User)
                .where(User.id == user.id, User.gavincoin_balance >= amount)
                .values(gavincoin_balance=User.gavincoin_balance - amount)
            )
            if result.rowcount == 0:
                current = db.scalar(select(User.gavincoin_balance).where(User.id == user.id))
                db.rollback()
                raise payment_required(float(amount), float(current or 0))
            balance_after = db.scalar(
                select(User.gavincoin_balance).where(User.id == user.id)
            )
            db.add(
                CreditTransaction(
                    user_id=user.id,
                    amount=-amount,
                    balance_after=balance_after,
                    tx_type="reserve",
                    reference_id=request_id,
                    note=f"hold for {model} (uncovered {uncovered} tok)",
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(user)
    return Reservation(
        user.id,
        amount,
        request_id,
        rates,
        model,
        package_holds=holds,
        prompt_estimate=prompt_tokens,
        max_tokens=output_limit,
        flat_fee=flat_fee,
    )


# ── settlement ──────────────────────────────────────────────────────────────

def settle(
    db: Session,
    reservation: Reservation,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    endpoint: str,
    api_key_id: int | None,
    duration_ms: int,
    status: str = "ok",
    charge_flat: bool = True,
    rates_override: "Rates | None" = None,
) -> UsageRecord:
    """Reconcile the hold against real usage and record the request.

    Always writes a usage row, including for failed requests (tokens may still
    have been produced before the stream broke).
    """
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        # 1. Release the package tokens held at reserve(). The hold sized the
        #    worst case (prompt + max_tokens); real usage is usually smaller.
        #    Work on the held instances themselves: sessions run with
        #    autoflush=False, so a fresh SELECT (remaining > 0) would not see
        #    the credit we just gave back.
        packages: list[TokenPackage] = []
        if reservation.package_holds:
            held_ids = [pkg_id for pkg_id, _ in reservation.package_holds]
            held = {
                p.id: p
                for p in db.scalars(select(TokenPackage).where(TokenPackage.id.in_(held_ids))).all()
            }
            for pkg_id, held_tokens in reservation.package_holds:
                if pkg := held.get(pkg_id):
                    pkg.remaining_tokens += held_tokens
                    if not pkg.is_expired:
                        packages.append(pkg)  # holds were drawn oldest-first
        else:
            packages = _active_packages(db, reservation.user_id)

        # 2. Redraw against actual usage, covering input tokens first and then
        #    output; only the uncovered remainder is billed in GavinCoin.
        _, covered_prompt = _draw_from_packages(packages, prompt_tokens)
        _, covered_completion = _draw_from_packages(packages, completion_tokens)
        covered = covered_prompt + covered_completion

        if reservation.flat_fee is not None:
            # Media call: charge the per-call price only when upstream actually
            # delivered (a 4xx/5xx or a connection failure refunds the hold).
            actual = q(reservation.flat_fee) if charge_flat else Decimal("0")
        else:
            # rates_override carries the X-Routed-Via model's rates when the
            # upstream routed the request to a different model than requested.
            rates = rates_override or reservation.rates
            actual = rates.cost(
                prompt_tokens - covered_prompt,
                completion_tokens - covered_completion,
            )
        delta = q(reservation.amount - actual)  # >0 refund, <0 extra debit

        if delta != 0:
            db.execute(
                update(User)
                .where(User.id == reservation.user_id)
                .values(gavincoin_balance=User.gavincoin_balance + delta)
            )
        balance_after = db.scalar(
            select(User.gavincoin_balance).where(User.id == reservation.user_id)
        )
        db.add(
            CreditTransaction(
                user_id=reservation.user_id,
                # The ledger records the *net* effect of this request: the
                # reserve row already debited the hold.
                amount=delta,
                balance_after=balance_after,
                tx_type="settle",
                reference_id=reservation.request_id,
                note=(
                    f"{reservation.model}: {prompt_tokens}+{completion_tokens} tok"
                    f" ({covered} via package)"
                ),
            )
        )
        record = UsageRecord(
            user_id=reservation.user_id,
            api_key_id=api_key_id,
            model=reservation.model,
            endpoint=endpoint,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            package_tokens=covered,
            gavincoin_cost=actual,
            duration_ms=duration_ms,
            status=status,
            request_id=reservation.request_id,
        )
        db.add(record)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(record)
    return record


# ── admin credit operations ─────────────────────────────────────────────────

def grant(db: Session, user: User, amount: Decimal, note: str | None = None) -> Decimal:
    """Credit (or debit, if negative) an account. Returns the new balance."""
    amount = q(amount)
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        db.execute(
            update(User)
            .where(User.id == user.id)
            .values(gavincoin_balance=User.gavincoin_balance + amount)
        )
        balance_after = db.scalar(select(User.gavincoin_balance).where(User.id == user.id))
        db.add(
            CreditTransaction(
                user_id=user.id,
                amount=amount,
                balance_after=balance_after,
                tx_type="grant" if amount >= 0 else "adjust",
                note=note,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(user)
    return user.gavincoin_balance


# ── redemption codes ────────────────────────────────────────────────────────

def redeem(db: Session, user: User, code_str: str) -> tuple[Decimal, Decimal]:
    """Redeem a prepaid code into the user's balance.

    Returns ``(new_balance, credited_amount)``.

    Raises GapiError(400/404/409) for unknown, disabled, exhausted or already
    (by this user) redeemed codes. All checks and the credit happen inside one
    BEGIN IMMEDIATE transaction, so two concurrent redeems of the same code
    cannot both succeed past max_uses.
    """
    normalized = code_str.strip().upper()
    if not normalized:
        raise GapiError(400, "invalid_code", "请输入兑换码")

    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        row = db.scalar(select(RedemptionCode).where(RedemptionCode.code == normalized))
        if row is None:
            raise GapiError(404, "code_not_found", "兑换码不存在")
        if not row.is_active:
            raise GapiError(400, "code_inactive", "兑换码已停用")
        # Per-user check first: "you already used this" is the more useful
        # answer than "the code is spent" for a returning redeemer.
        already = db.scalar(
            select(CreditTransaction).where(
                CreditTransaction.user_id == user.id,
                CreditTransaction.tx_type == "redeem",
                CreditTransaction.reference_id == normalized,
            )
        )
        if already is not None:
            raise GapiError(409, "code_already_redeemed", "你已兑换过该兑换码")
        if row.used_count >= row.max_uses:
            raise GapiError(409, "code_exhausted", "兑换码已被使用完")

        row.used_count += 1
        amount = q(row.amount)
        db.execute(
            update(User)
            .where(User.id == user.id)
            .values(gavincoin_balance=User.gavincoin_balance + amount)
        )
        balance_after = db.scalar(
            select(User.gavincoin_balance).where(User.id == user.id)
        )
        db.add(
            CreditTransaction(
                user_id=user.id,
                amount=amount,
                balance_after=balance_after,
                tx_type="redeem",
                reference_id=normalized,
                note=f"redeem code {normalized}",
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(user)
    return user.gavincoin_balance, amount


# ── token packages ──────────────────────────────────────────────────────────

def buy_package(
    db: Session,
    user: User,
    cost: Decimal,
    tokens_per_coin: int,
    valid_days: int | None = None,
) -> TokenPackage:
    """Buy a subscription-style quota plan: prepaid tokens for one period.

    The plan's tokens are drawn first while it is alive; anything left when
    ``valid_days`` elapses is abandoned (no rollover, no auto-renewal). Pass
    ``valid_days=None`` for a permanent (non-expiring) quota. Raises 402 if the
    GavinCoin balance is short.

    Same BEGIN IMMEDIATE discipline as reserve(): the balance check and the
    debit must not interleave with a concurrent request's pair.
    """
    from datetime import timedelta

    cost = q(cost)
    if cost <= 0:
        raise GapiError(400, "invalid_amount", "Purchase amount must be positive")
    tokens = int(cost * tokens_per_coin * 1000)
    if tokens <= 0:
        raise GapiError(400, "invalid_amount", "Purchase is too small to yield any tokens")

    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        result = db.execute(
            update(User)
            .where(User.id == user.id, User.gavincoin_balance >= cost)
            .values(gavincoin_balance=User.gavincoin_balance - cost)
        )
        if result.rowcount == 0:
            current = db.scalar(select(User.gavincoin_balance).where(User.id == user.id))
            db.rollback()
            raise payment_required(float(cost), float(current or 0))
        balance_after = db.scalar(select(User.gavincoin_balance).where(User.id == user.id))
        package = TokenPackage(
            user_id=user.id,
            total_tokens=tokens,
            remaining_tokens=tokens,
            cost_gavincoin=cost,
            expires_at=(
                datetime.now(timezone.utc) + timedelta(days=valid_days)
                if valid_days
                else None
            ),
        )
        db.add(package)
        db.add(
            CreditTransaction(
                user_id=user.id,
                amount=-cost,
                balance_after=balance_after,
                tx_type="package_purchase",
                note=f"{tokens:,} tokens",
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(package)
    db.refresh(user)
    return package
