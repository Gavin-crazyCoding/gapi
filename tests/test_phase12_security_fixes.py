"""Phase 12: security-fix regressions (Claude Security scan 2026-09-19).

Covers the three code findings from CLAUDE-SECURITY-20260919-144245:
- F2/F8: the proxy bounds request bodies itself (413 before materializing).
- F5: X-Forwarded-For is only honoured from configured trusted proxies.
- F7: concurrent registrations can never both become admin.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database import SessionLocal
from app.models import User
from app.services import billing, upstream


@pytest.fixture()
async def api_key(client, registered_admin):
    headers = {"Authorization": f"Bearer {registered_admin['access_token']}"}
    resp = await client.post("/user/keys", json={"name": "sec-test"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


@pytest.fixture()
def priced():
    db = SessionLocal()
    try:
        billing.seed_default_pricing(db)
        yield
    finally:
        db.close()


# ── F2/F8: bounded request body ──────────────────────────────────────────


async def test_proxy_rejects_oversized_body_by_content_length(
    client, api_key, priced, monkeypatch
):
    """A declared-oversize body is refused before a single byte is read."""
    monkeypatch.setattr(settings, "max_body_bytes", 100)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        content=b"x" * 200,  # httpx sets Content-Length: 200 > 100
    )
    assert resp.status_code == 413
    assert resp.headers["x-gapi-error"] == "1"
    assert resp.json()["error"]["code"] == "payload_too_large"


async def test_proxy_rejects_oversized_body_streamed(client, api_key, priced, monkeypatch):
    """Chunked bodies carry no Content-Length; the streamed count is the guard."""
    monkeypatch.setattr(settings, "max_body_bytes", 100)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"y" * 64  # 192 bytes total, limit crossed mid-stream

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        content=chunks(),
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


async def test_proxy_accepts_body_within_limit(client, api_key, priced, monkeypatch):
    """A small body still flows through to the upstream stub and bills."""
    monkeypatch.setattr(settings, "max_body_bytes", 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    original = upstream._client
    upstream._client = httpx.AsyncClient(
        base_url="http://upstream.test/v1",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer upstream-key"},
    )
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
    finally:
        upstream._client = original
    assert resp.status_code == 200, resp.text


# ── F5: X-Forwarded-For trust gate ───────────────────────────────────────


async def test_xff_ignored_when_proxy_not_trusted(client):
    """Spoofed XFF must not mint fresh limiter buckets: the 31st login is 429."""
    last = None
    for i in range(31):
        last = await client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password"},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},  # a fresh "IP" per call
        )
    assert last is not None
    assert last.status_code == 429
    assert last.json()["error"]["code"] == "rate_limited"


async def test_xff_honored_when_proxy_trusted(client, monkeypatch):
    """Behind a configured proxy the limiter keys on the forwarded client IP."""
    monkeypatch.setattr(settings, "trusted_proxy_ips", "127.0.0.1")
    last = None
    for i in range(31):
        last = await client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password"},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        )
    assert last is not None
    # Each XFF gets its own bucket, so the 31st distinct IP still reaches auth.
    assert last.status_code == 401


# ── F7: first-user-admin race ────────────────────────────────────────────


async def test_concurrent_register_only_one_admin(client):
    """Two racing signups on an empty table must yield exactly one admin."""
    r1, r2 = await asyncio.gather(
        client.post(
            "/auth/register",
            json={"email": "race-a@example.com", "password": "password123"},
        ),
        client.post(
            "/auth/register",
            json={"email": "race-b@example.com", "password": "password123"},
        ),
    )
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text

    db = SessionLocal()
    try:
        admins = db.scalar(select(func.count()).select_from(User).where(User.role == "admin"))
        roles = set(
            db.scalars(
                select(User.role).where(
                    User.email.in_(["race-a@example.com", "race-b@example.com"])
                )
            ).all()
        )
    finally:
        db.close()
    assert admins == 1
    assert roles == {"admin", "user"}
