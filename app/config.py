"""Application configuration, loaded from environment / .env.

Two env prefixes coexist (per spec): ``FREELLM_API_*`` for the upstream
connection and ``GAPI_*`` for everything gapi-owned, so each field carries
an explicit validation alias instead of a global prefix.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from decimal import Decimal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor the env file to the project root (this file's grandparent), not the
# process CWD — uvicorn started from any directory must read the same .env.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Upstream FreeLLM API
    freellm_api_base: str = Field(
        default="http://localhost:3001/v1",
        validation_alias=AliasChoices("FREELLM_API_BASE"),
    )
    freellm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("FREELLM_API_KEY"),
    )

    # gapi
    jwt_secret: str = Field(
        default="dev-insecure-secret-change-me",
        validation_alias=AliasChoices("GAPI_JWT_SECRET"),
    )
    database_url: str = Field(
        default="sqlite:///./data/gapi.db",
        validation_alias=AliasChoices("GAPI_DATABASE_URL"),
    )
    default_currency: str = Field(
        default="GavinCoin",
        validation_alias=AliasChoices("GAPI_DEFAULT_CURRENCY"),
    )
    # Quota plans: 1 GavinCoin buys this many *thousand* tokens of plan quota.
    # 100 → ◎1 = 100k tokens, a deliberate bulk discount versus pay-as-you-go
    # list rates, which is what makes subscribing to a plan rational.
    token_package_rate: int = Field(
        default=100,
        validation_alias=AliasChoices("GAPI_TOKEN_PACKAGE_RATE"),
    )
    upstream_timeout_chat: int = Field(
        default=300,
        validation_alias=AliasChoices("GAPI_UPSTREAM_TIMEOUT_CHAT"),
    )
    upstream_timeout_embeddings: int = Field(
        default=60,
        validation_alias=AliasChoices("GAPI_UPSTREAM_TIMEOUT_EMBEDDINGS"),
    )
    upstream_timeout_media: int = Field(
        default=300,
        validation_alias=AliasChoices("GAPI_UPSTREAM_TIMEOUT_MEDIA"),
    )
    # Signup: every new account is credited this much GavinCoin.
    registration_bonus: Decimal = Field(
        default=Decimal("20"),
        validation_alias=AliasChoices("GAPI_REGISTRATION_BONUS"),
    )

    # Email verification
    smtp_host: str = Field(
        default="localhost",
        validation_alias=AliasChoices("SMTP_HOST"),
    )
    smtp_port: int = Field(
        default=25,
        validation_alias=AliasChoices("SMTP_PORT"),
    )
    smtp_user: str = Field(
        default="",
        validation_alias=AliasChoices("SMTP_USER"),
    )
    smtp_password: str = Field(
        default="",
        validation_alias=AliasChoices("SMTP_PASSWORD"),
    )
    email_sender: str = Field(
        default="noreply@gapi.local",
        validation_alias=AliasChoices("EMAIL_SENDER"),
    )
    # Master switch for outbound mail. Tests set GAPI_EMAIL_ENABLED=false.
    email_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("GAPI_EMAIL_ENABLED"),
    )
    base_url: str = Field(
        default="http://localhost:3002",
        validation_alias=AliasChoices("BASE_URL"),
    )

    jwt_algorithm: str = "HS256"
    jwt_expire_days: int = 7

    @field_validator("freellm_api_base")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.jwt_secret != "dev-insecure-secret-change-me"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
