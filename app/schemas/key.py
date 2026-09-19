"""API key + user-panel schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_serializer

# ── API keys ────────────────────────────────────────────────────────────

class ApiKeyCreate(BaseModel):
    name: str = Field(default="default", max_length=100)
    quota: int | None = Field(default=None, ge=0)
    expires_at: datetime | None = None


class ApiKeyOut(BaseModel):
    id: int
    key_prefix: str
    name: str
    is_active: bool
    quota: int | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    # 30-day rolling usage for this key (0-filled in the router).
    requests_30d: int = 0
    tokens_30d: int = 0

    model_config = {"from_attributes": True}


class ApiKeyCreated(ApiKeyOut):
    # Plaintext key, returned exactly once at creation time.
    key: str


# ── balance / packages / ledger ─────────────────────────────────────────

class BalanceOut(BaseModel):
    balance: Decimal
    currency: str

    @field_serializer("balance")
    def _ser(self, v: Decimal) -> str:
        return str(v)


class RoutingStrategyOut(BaseModel):
    """User's preferred routing strategy for model selection."""
    strategy: Literal["auto", "fastest", "smart", "balanced", "stable"]

    @field_serializer("strategy")
    def _ser(self, v: str) -> str:
        return v


class ModelOut(BaseModel):
    """A model the caller can reach, with gapi's price for it."""

    id: str
    name: str | None = None
    context_window: int | None = None
    input_per_1k: Decimal
    output_per_1k: Decimal
    priced: bool  # False when falling back to the default rate
    is_router: bool
    supports_tools: bool
    supports_streaming: bool

    model_config = {"protected_namespaces": ()}

    @field_serializer("input_per_1k", "output_per_1k")
    def _ser(self, v: Decimal) -> str:
        return str(v)


class ModelListOut(BaseModel):
    models: list[ModelOut]
    cached_age_seconds: int


class PackagePurchase(BaseModel):
    """Buy prepaid tokens with GavinCoin."""

    cost: Decimal = Field(gt=0, description="GavinCoin to spend")
    valid_days: int | None = Field(default=None, ge=1, le=3650)


class PackageOut(BaseModel):
    id: int
    total_tokens: int
    remaining_tokens: int
    used_tokens: int
    status: str  # active | exhausted | expired
    cost_gavincoin: Decimal
    expires_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("cost_gavincoin")
    def _ser(self, v: Decimal) -> str:
        return str(v)


class TransactionOut(BaseModel):
    id: int
    amount: Decimal
    balance_after: Decimal
    tx_type: str
    reference_id: str | None = None
    note: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("amount", "balance_after")
    def _ser(self, v: Decimal) -> str:
        return str(v)


# ── redemption / password ─────────────────────────────────────────────────

class RedeemIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class RedeemOut(BaseModel):
    redeemed: Decimal
    balance: Decimal

    @field_serializer("redeemed", "balance")
    def _ser_redeem(self, v: Decimal) -> str:
        return str(v)


class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)


# ── usage ───────────────────────────────────────────────────────────────

class UsageDayOut(BaseModel):
    date: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: Decimal

    @field_serializer("cost")
    def _ser(self, v: Decimal) -> str:
        return str(v)


class UsageModelOut(BaseModel):
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: Decimal

    # `model` is a field name, not the pydantic namespace.
    model_config = {"protected_namespaces": ()}

    @field_serializer("cost")
    def _ser(self, v: Decimal) -> str:
        return str(v)


class UsageRecentOut(BaseModel):
    id: int
    model: str
    endpoint: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    gavincoin_cost: Decimal
    duration_ms: int
    status: str
    request_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True, "protected_namespaces": ()}

    @field_serializer("gavincoin_cost")
    def _ser(self, v: Decimal) -> str:
        return str(v)


class DashboardOut(BaseModel):
    balance: Decimal
    currency: str
    total_tokens_30d: int
    total_spend_30d: Decimal
    request_count_30d: int
    usage_by_day: list[UsageDayOut]
    usage_by_model: list[UsageModelOut]
    recent: list[UsageRecentOut]

    @field_serializer("balance", "total_spend_30d")
    def _ser(self, v: Decimal) -> str:
        return str(v)