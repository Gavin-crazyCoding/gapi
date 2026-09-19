"""Phase 5: panel playground, fingerprint binding, daily login reward."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import UsageRecord, User
from app.services import billing, upstream


@pytest.fixture()
def jwt(registered_admin):
    return {"Authorization": f"Bearer {registered_admin['access_token']}"}


@pytest.fixture()
def priced():
    db = SessionLocal()
    try:
        billing.seed_default_pricing(db)
        yield
    finally:
        db.close()


def _stub_upstream(handler):
    original = upstream._client
    upstream._client = httpx.AsyncClient(
        base_url="http://upstream.test/v1",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer upstream-key"},
    )
    return original


def _balance(email: str) -> Decimal:
    db = SessionLocal()
    try:
        return db.scalar(select(User.gavincoin_balance).where(User.email == email))
    finally:
        db.close()


async def test_playground_requires_session(client):
    resp = await client.post(
        "/user/playground/chat",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


async def test_playground_forwards_without_api_key_and_bills(client, jwt, priced):
    """The playground reuses the proxy pipeline but needs no gapi key; usage is
    recorded with endpoint /playground/chat and a NULL api_key_id."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-pg",
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 2000},
            },
        )

    original = _stub_upstream(handler)
    try:
        before = _balance("admin@example.com")
        resp = await client.post(
            "/user/playground/chat",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 100,
            },
            headers=jwt,
        )
    finally:
        upstream._client = original

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "pong"
    assert before - _balance("admin@example.com") == Decimal("0.035000")

    db = SessionLocal()
    try:
        row = db.scalars(select(UsageRecord)).one()
        assert row.endpoint == "/playground/chat"
        assert row.api_key_id is None
        assert (row.prompt_tokens, row.completion_tokens) == (1000, 2000)
    finally:
        db.close()


async def test_playground_streams_sse(client, jwt, priced):
    frames = [
        b'data: {"model":"gpt-4o","choices":[{"delta":{"content":"he"}}]}\n\n',
        b'data: {"model":"gpt-4o","choices":[{"delta":{"content":"y"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"".join(frames)),
            headers={"content-type": "text/event-stream"},
        )

    original = _stub_upstream(handler)
    try:
        body = b""
        async with client.stream(
            "POST",
            "/user/playground/chat",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=jwt,
        ) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_bytes():
                body += chunk
    finally:
        upstream._client = original

    assert b"he" in body and b"y" in body


async def test_fingerprint_login_rebinds_panel_stays_strict(client):
    email = "fp@example.com"
    await client.post(
        "/auth/register",
        json={"email": email, "password": "hunter2hunter", "fingerprint": "fp-A"},
    )

    # A drifted/new environment with the correct password is NOT locked out:
    # login succeeds and registers the new fingerprint alongside the old one.
    moved = await client.post(
        "/auth/login",
        json={"email": email, "password": "hunter2hunter", "fingerprint": "fp-B"},
    )
    assert moved.status_code == 200

    # Both bound environments now pass panel checks…
    token = moved.json()["access_token"]
    for fp in ("fp-A", "fp-B"):
        r = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}", "X-Fingerprint": fp},
        )
        assert r.status_code == 200, fp

    # …but an unbound fingerprint (or a missing header) is still refused.
    denied = await client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}", "X-Fingerprint": "fp-X"},
    )
    assert denied.status_code == 403
    assert (
        await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 403


async def test_daily_login_reward_paid_once_per_day(client):
    email = "daily@example.com"
    reg = await client.post(
        "/auth/register", json={"email": email, "password": "hunter2hunter"}
    )
    assert reg.json()["user"]["gavincoin_balance"] == "20.000000"

    first = await client.post(
        "/auth/login", json={"email": email, "password": "hunter2hunter"}
    )
    assert first.json()["user"]["gavincoin_balance"] == "21.000000"

    second = await client.post(
        "/auth/login", json={"email": email, "password": "hunter2hunter"}
    )
    assert second.json()["user"]["gavincoin_balance"] == "21.000000"


async def test_concurrency_limit_returns_429(client, jwt, priced):
    """A request arriving at the user's limit is refused before forwarding;
    a completed request releases its slot (counter back to zero)."""
    upstream_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_called
        upstream_called = True
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    original = _stub_upstream(handler)
    payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}]}
    try:
        # Saturate the default limit (5) directly in the DB.
        db = SessionLocal()
        try:
            user = db.scalar(select(User).where(User.email == "admin@example.com"))
            user.concurrent_active = user.concurrent_limit
            db.commit()
        finally:
            db.close()

        refused = await client.post("/user/playground/chat", json=payload, headers=jwt)
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "concurrency_limit"
        assert upstream_called is False  # rejected before hitting upstream

        # Free a slot; the request now goes through and releases the counter.
        db = SessionLocal()
        try:
            user = db.scalar(select(User).where(User.email == "admin@example.com"))
            user.concurrent_active = 0
            db.commit()
        finally:
            db.close()

        ok = await client.post("/user/playground/chat", json=payload, headers=jwt)
        assert ok.status_code == 200
        assert upstream_called is True

        db = SessionLocal()
        try:
            user = db.scalar(select(User).where(User.email == "admin@example.com"))
            assert user.concurrent_active == 0
        finally:
            db.close()
    finally:
        upstream._client = original
