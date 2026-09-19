"""Phase 9: /users trailing-slash fix, rate multiplier, responsive nav assets."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import User
from app.services import billing, upstream


@pytest.fixture()
def jwt(registered_admin):
    return {"Authorization": f"Bearer {registered_admin['access_token']}"}


def _balance(email: str) -> Decimal:
    db = SessionLocal()
    try:
        return db.scalar(select(User.gavincoin_balance).where(User.email == email))
    finally:
        db.close()


# ── /users without trailing slash must not hit the static mount ──────────────

async def test_users_listing_without_trailing_slash(client, jwt):
    r = await client.get("/users", headers=jwt)
    assert r.status_code == 200, f"got {r.status_code} — static mount swallowed the route?"
    assert isinstance(r.json(), list)


async def test_create_user_without_trailing_slash(client, jwt):
    r = await client.post(
        "/users", json={"email": "noslash@x.com", "password": "password123"}, headers=jwt
    )
    assert r.status_code == 201, r.text


# ── rate multiplier ──────────────────────────────────────────────────────────

async def test_rate_multiplier_scales_metered_billing(client, jwt):
    created = await client.post("/user/keys", json={"name": "mult"}, headers=jwt)
    key = created.json()["key"]
    db = SessionLocal()
    try:
        billing.seed_default_pricing(db)
    finally:
        db.close()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
            },
        )

    original = upstream._client
    upstream._client = httpx.AsyncClient(
        base_url="http://upstream.test/v1",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer upstream-key"},
    )
    payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    headers = {"Authorization": f"Bearer {key}"}
    try:
        before = _balance("admin@example.com")
        resp = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert resp.status_code == 200
        base_cost = before - _balance("admin@example.com")
        # gpt-4o default: 1k*0.005 + 1k*0.015 = 0.02
        assert base_cost == Decimal("0.020000")

        # Triple the global multiplier: the very next call costs 3x.
        r = await client.put(
            "/admin/settings", json=[{"key": "rate_multiplier", "value": "3"}], headers=jwt
        )
        assert r.status_code == 200
        resp = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert resp.status_code == 200
        assert _balance("admin@example.com") == before - base_cost - Decimal("0.060000")
    finally:
        upstream._client = original


async def test_rate_multiplier_scales_flat_fee(client, jwt):
    created = await client.post("/user/keys", json={"name": "multflat"}, headers=jwt)
    key = created.json()["key"]
    await client.put(
        "/admin/settings", json=[{"key": "rate_multiplier", "value": "2"}], headers=jwt
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xd8-bin", headers={"content-type": "audio/mpeg"})

    original = upstream._client
    upstream._client = httpx.AsyncClient(
        base_url="http://upstream.test/v1",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer upstream-key"},
    )
    try:
        before = _balance("admin@example.com")
        resp = await client.post(
            "/v1/audio/speech",
            json={"model": "tts-1", "input": "hi"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        # default 0.02 flat fee × multiplier 2 = 0.04
        assert before - _balance("admin@example.com") == Decimal("0.040000")
    finally:
        upstream._client = original


async def test_settings_expose_rate_multiplier(client, jwt):
    body = (await client.get("/admin/settings", headers=jwt)).json()
    assert body["rate_multiplier"] == "1"


# ── hamburger nav markup is delivered with the panel ─────────────────────────

async def test_panel_shell_contains_nav_toggle(client, registered_admin):
    r = await client.get("/panel")
    assert r.status_code == 200
    assert 'id="navToggle"' in r.text
    assert 'id="mainNav"' in r.text
