"""Pytest fixtures: isolated temp DB + async httpx client via ASGI transport.

Env vars must be set before app.config is imported; settings are cached.
Async tests run under pytest-asyncio auto mode (see pyproject.toml).
"""

from __future__ import annotations

import os
import tempfile

import httpx
import pytest

_TMP = tempfile.mkdtemp(prefix="gapi-test-")
os.environ.setdefault("GAPI_DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("GAPI_JWT_SECRET", "test-secret-do-not-use-in-prod-0123456789")
# Tests must never open SMTP connections.
os.environ.setdefault("GAPI_EMAIL_ENABLED", "false")
# Pin the env-derived economy defaults so the repo's own .env cannot leak
# into assertions (process env beats the .env file in pydantic-settings).
os.environ.setdefault("GAPI_TOKEN_PACKAGE_RATE", "100")
os.environ.setdefault("GAPI_REGISTRATION_BONUS", "20")

from app.database import Base, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
import app.models  # noqa: E402,F401  (register metadata)


@pytest.fixture(autouse=True)
def _drop_caches():
    """TTL caches (settings, pricing) must never leak between tests."""
    from app.services import billing, coin, ratelimit
    coin.invalidate_settings()
    billing.invalidate_pricing()
    ratelimit.clear()
    yield
    coin.invalidate_settings()
    billing.invalidate_pricing()
    ratelimit.clear()


@pytest.fixture()
async def client():
    # Tests start from a clean schema every time.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gapi.test") as c:
        yield c


@pytest.fixture()
async def registered_admin(client):
    resp = await client.post(
        "/auth/register",
        json={"email": "admin@example.com", "password": "hunter2hunter"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return {"email": "admin@example.com", "password": "hunter2hunter", **data}
