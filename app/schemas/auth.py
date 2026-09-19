"""Auth request/response schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field, field_serializer

# Mirrors the client-side rule and the design doc.
PASSWORD_MIN = 8
PASSWORD_MAX = 72  # bcrypt truncates past 72 bytes; reject rather than silently cut


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    # SHA-256 browser/environment fingerprint collected by web/public/fp.js.
    fingerprint: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=PASSWORD_MAX)
    fingerprint: str | None = None


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    gavincoin_balance: Decimal
    is_active: bool
    email_verified: bool
    concurrent_limit: int = 5
    concurrent_active: int = 0
    last_login_date: datetime | None = None

    model_config = {"from_attributes": True}

    @field_serializer("gavincoin_balance")
    def _ser_balance(self, v: Decimal) -> str:
        return str(v)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
