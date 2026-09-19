"""Phase 2/3: proxy forwarding, streaming, and GavinCoin billing.

The upstream is a stub ASGI app mounted into httpx, so these exercise gapi's
own forwarding, usage accounting and settlement without a live FreeLLM API.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import CreditTransaction, Pricing, UsageRecord, User
from app.services import billing, upstream


@pytest.fixture()
async def api_key(client, registered_admin):
    """A live gapi key plus the JWT headers of the account that owns it."""
    headers = {"Authorization": f"Bearer {registered_admin['access_token']}"}
    resp = await client.post("/user/keys", json={"name": "proxy-test"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


@pytest.fixture()
def priced():
    """gpt-4o priced at the documented default rates."""
    db = SessionLocal()
    try:
        billing.seed_default_pricing(db)
        yield
    finally:
        db.close()


def _stub_upstream(handler):
    """Point the shared upstream client at an in-process handler."""
    original = upstream._client
    upstream._client = httpx.AsyncClient(
        base_url="http://upstream.test/v1",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer upstream-key"},
    )
    return original


def _restore(original):
    upstream._client = original


def _balance(email: str) -> Decimal:
    db = SessionLocal()
    try:
        return db.scalar(select(User.gavincoin_balance).where(User.email == email))
    finally:
        db.close()


def _usage_rows(email: str) -> list[UsageRecord]:
    db = SessionLocal()
    try:
        uid = db.scalar(select(User.id).where(User.email == email))
        return db.scalars(select(UsageRecord).where(UsageRecord.user_id == uid)).all()
    finally:
        db.close()


async def test_non_streaming_forwards_and_bills(client, api_key, priced):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["task_type"] = request.headers.get("x-freellm-task-type")
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 2000},
            },
            headers={"X-Routed-Via": "openrouter", "X-Fallback-Attempts": "2"},
        )

    original = _stub_upstream(handler)
    try:
        before = _balance("admin@example.com")
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100},
            headers={"Authorization": f"Bearer {api_key}", "X-FreeLLM-Task-Type": "code"},
        )
    finally:
        _restore(original)

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hi"

    # The user's gapi key never reaches upstream; the configured unified key
    # replaces it.
    from app.config import settings

    assert captured["auth"] == f"Bearer {settings.freellm_api_key}"
    assert api_key not in (captured["auth"] or "")
    # Client hint survives the hop.
    assert captured["task_type"] == "code"
    # Routing headers reach the caller.
    assert resp.headers["x-routed-via"] == "openrouter"
    assert resp.headers["x-fallback-attempts"] == "2"
    # Hop-by-hop headers are not forwarded.
    assert "transfer-encoding" not in resp.headers

    # 1000 input @0.005/1k + 2000 output @0.015/1k = 0.005 + 0.030 = 0.035
    after = _balance("admin@example.com")
    assert before - after == Decimal("0.035000")

    rows = _usage_rows("admin@example.com")
    assert len(rows) == 1
    assert (rows[0].prompt_tokens, rows[0].completion_tokens) == (1000, 2000)
    assert rows[0].gavincoin_cost == Decimal("0.035000")
    assert rows[0].status == "ok"


async def test_streaming_forwards_chunks_and_settles(client, api_key, priced):
    frames = [
        b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
        b'data: {"usage":{"prompt_tokens":500,"completion_tokens":1500}}\n\n',
        b"data: [DONE]\n\n",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"".join(frames)),
            headers={"content-type": "text/event-stream", "X-Routed-Via": "kilo"},
        )

    original = _stub_upstream(handler)
    try:
        before = _balance("admin@example.com")
        body = b""
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers={"Authorization": f"Bearer {api_key}"},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["x-routed-via"] == "kilo"
            async for chunk in resp.aiter_bytes():
                body += chunk
    finally:
        _restore(original)

    # Every frame reached the client, in order, byte-for-byte.
    for frame in frames:
        assert frame in body
    assert body == b"".join(frames)

    # 500 @0.005/1k + 1500 @0.015/1k = 0.0025 + 0.0225 = 0.025
    after = _balance("admin@example.com")
    assert before - after == Decimal("0.025000")

    rows = _usage_rows("admin@example.com")
    assert rows[-1].completion_tokens == 1500
    assert rows[-1].status == "ok"


async def test_insufficient_balance_returns_402(client, registered_admin, api_key, priced):
    """A request whose worst case exceeds the balance is refused before forwarding."""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    # Drain the account down to a rounding error.
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "admin@example.com"))
        billing.grant(db, user, -user.gavincoin_balance, "drain for test")
    finally:
        db.close()

    original = _stub_upstream(handler)
    try:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}], "max_tokens": 4096},
            headers={"Authorization": f"Bearer {api_key}"},
        )
    finally:
        _restore(original)

    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "insufficient_balance"
    assert resp.headers["x-gapi-error"] == "1"
    # Nothing was forwarded — the refusal happens before the upstream call.
    assert called["n"] == 0


async def test_reserve_is_refunded_when_actual_is_lower(client, api_key, priced):
    """Reserving max_tokens then using fewer must return the difference."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {"prompt_tokens": 0, "completion_tokens": 10}})

    original = _stub_upstream(handler)
    try:
        before = _balance("admin@example.com")
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [], "max_tokens": 4000},
            headers={"Authorization": f"Bearer {api_key}"},
        )
    finally:
        _restore(original)

    assert resp.status_code == 200
    after = _balance("admin@example.com")
    # Reserve was 4000/1000*0.015 = 0.06; actual is 10/1000*0.015 = 0.00015.
    assert before - after == Decimal("0.000150")


async def test_unknown_endpoint_is_not_proxied(client, api_key):
    resp = await client.post(
        "/v1/api/settings/whatever",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "endpoint_not_forwarded"


async def test_proxy_requires_a_valid_key(client):
    resp = await client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": []})
    assert resp.status_code == 401
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": []},
        headers={"Authorization": "Bearer gapi-not-a-real-key"},
    )
    assert resp.status_code == 401


async def test_routing_strategy_rewrites_bare_auto(client, api_key, priced):
    """The user's strategy maps bare 'auto' onto upstream's auto:* suffix."""
    from app.database import SessionLocal
    from app.models import User as UserModel

    db = SessionLocal()
    try:
        u = db.scalar(select(UserModel).where(UserModel.email == "admin@example.com"))
        u.routing_strategy = "fastest"
        db.commit()
    finally:
        db.close()

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["model"] = json.loads(request.content)["model"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )

    original = _stub_upstream(handler)
    try:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
            headers={"Authorization": f"Bearer {api_key}"},
        )
    finally:
        _restore(original)

    assert resp.status_code == 200, resp.text
    assert captured["model"] == "auto:fast"


async def test_explicit_model_is_not_rewritten(client, api_key, priced):
    """A concrete model choice always wins over the stored strategy."""
    from app.database import SessionLocal
    from app.models import User as UserModel

    db = SessionLocal()
    try:
        u = db.scalar(select(UserModel).where(UserModel.email == "admin@example.com"))
        u.routing_strategy = "smart"
        db.commit()
    finally:
        db.close()

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["model"] = json.loads(request.content)["model"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )

    original = _stub_upstream(handler)
    try:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
            headers={"Authorization": f"Bearer {api_key}"},
        )
    finally:
        _restore(original)

    assert resp.status_code == 200, resp.text
    assert captured["model"] == "gpt-4o"

