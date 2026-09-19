"""Phase 6: regression tests for the audit fixes (announcements, security,
billing edge cases, settings wiring)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import CreditTransaction, UsageRecord, User
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


async def _register(client, email, password="hunter2hunter", **extra):
    r = await client.post("/auth/register", json={"email": email, "password": password, **extra})
    assert r.status_code == 201, r.text
    return r.json()


# ── announcements ────────────────────────────────────────────────────────────

async def test_announcement_full_lifecycle(client, jwt):
    # Admin creates; a regular user sees it; toggle hides it again.
    await _register(client, "viewer@x.com")

    create = await client.post(
        "/admin/announcements",
        json={"title": "维护通知", "content": "今晚 <b>停机</b> 升级", "priority": 5},
        headers=jwt,
    )
    assert create.status_code == 201, create.text
    aid = create.json()["id"]
    assert create.json()["is_active"] is True
    assert create.json()["live"] is True

    # Single-fetch endpoint exists (the edit modal needs it).
    got = await client.get(f"/admin/announcements/{aid}", headers=jwt)
    assert got.status_code == 200
    assert got.json()["title"] == "维护通知"

    # User-facing feed returns it (plain-text mode, content unmodified).
    feed = await client.get("/user/announcements", headers=jwt)
    assert feed.status_code == 200
    assert any(a["id"] == aid for a in feed.json())

    # Toggle off via PATCH — the panel's enable/disable button.
    off = await client.patch(f"/admin/announcements/{aid}", json={"is_active": False}, headers=jwt)
    assert off.status_code == 200, off.text
    assert off.json()["is_active"] is False
    assert off.json()["live"] is False

    feed = await client.get("/user/announcements", headers=jwt)
    assert all(a["id"] != aid for a in feed.json())

    # Full update via PUT keeps the switch in the contract.
    put = await client.put(
        f"/admin/announcements/{aid}",
        json={"title": "已恢复", "content": "升级完成", "is_active": True, "priority": 1},
        headers=jwt,
    )
    assert put.status_code == 200
    assert put.json()["title"] == "已恢复"
    feed = await client.get("/user/announcements", headers=jwt)
    assert any(a["id"] == aid for a in feed.json())


async def test_announcements_require_admin(client):
    await _register(client, "first@x.com")  # first account becomes the admin
    await _register(client, "plain@x.com")
    login = await client.post("/auth/login", json={"email": "plain@x.com", "password": "hunter2hunter"})
    ujwt = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/admin/announcements", headers=ujwt)).status_code == 403
    assert (
        await client.post("/admin/announcements", json={"title": "x", "content": "y"}, headers=ujwt)
    ).status_code == 403


async def test_announcement_window_excludes_future(client, jwt):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r = await client.post(
        "/admin/announcements",
        json={"title": "未来", "content": "还没开始", "start_at": future},
        headers=jwt,
    )
    assert r.status_code == 201
    assert r.json()["live"] is False  # switch on, but outside the window
    feed = await client.get("/user/announcements", headers=jwt)
    assert all(a["title"] != "未来" for a in feed.json())


# ── admin user management ────────────────────────────────────────────────────

async def test_cannot_delete_the_only_admin_returns_400(client, jwt, registered_admin):
    uid = registered_admin["user"]["id"]
    r = await client.delete(f"/users/{uid}", headers=jwt)
    assert r.status_code == 400, r.text  # was a 500 (property used as SQL)
    assert r.json()["error"]["code"] == "cannot_delete_only_admin"


async def test_delete_user_with_pending_email_verification(client, jwt):
    # Registration always writes an email_verifications row; deleting the user
    # must cascade it instead of violating the FK.
    victim = await _register(client, "victim@x.com")
    r = await client.delete(f"/users/{victim['user']['id']}", headers=jwt)
    assert r.status_code == 204, r.text


async def test_moderator_role_rejected_by_schema(client, jwt):
    victim = await _register(client, "mod@x.com")
    r = await client.put(f"/users/{victim['user']['id']}", json={"role": "moderator"}, headers=jwt)
    assert r.status_code == 422  # DB CHECK only allows user|admin


async def test_admin_create_user_normalizes_email(client, jwt):
    r = await client.post(
        "/users", json={"email": "Mixed@Example.COM", "password": "password123"}, headers=jwt
    )
    assert r.status_code == 201, r.text
    login = await client.post(
        "/auth/login", json={"email": "mixed@example.com", "password": "password123"}
    )
    assert login.status_code == 200


# ── api keys ─────────────────────────────────────────────────────────────────

async def test_expired_key_is_rejected(client, jwt):
    r = await client.post(
        "/user/keys",
        json={"name": "old", "expires_at": "2020-01-01T00:00:00+00:00"},
        headers=jwt,
    )
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    resp = await client.get("/user/verify-key", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 401


async def test_deleting_a_key_keeps_usage_history(client, jwt, priced):
    created = await client.post("/user/keys", json={"name": "k1"}, headers=jwt)
    key = created.json()["key"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    original = _stub_upstream(handler)
    try:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200, resp.text
    finally:
        upstream._client = original

    kid = created.json()["id"]
    db = SessionLocal()
    try:
        assert db.scalar(select(UsageRecord).where(UsageRecord.api_key_id == kid)) is not None
    finally:
        db.close()

    assert (await client.delete(f"/user/keys/{kid}", headers=jwt)).status_code == 204

    db = SessionLocal()
    try:
        row = db.scalar(select(UsageRecord).order_by(UsageRecord.id.desc()))
        assert row is not None, "usage history must survive key deletion"
        assert row.api_key_id is None  # FK settled to NULL, row kept
    finally:
        db.close()


# ── fingerprint binding ──────────────────────────────────────────────────────

async def test_bound_fingerprint_requires_header(client):
    await _register(client, "fp2@x.com", fingerprint="fp-A")
    login = await client.post(
        "/auth/login", json={"email": "fp2@x.com", "password": "hunter2hunter", "fingerprint": "fp-A"}
    )
    token = login.json()["access_token"]

    # Missing header is refused now — a stolen token cannot just omit it.
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 403
    assert (
        await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}", "X-Fingerprint": "fp-B"}
        )
    ).status_code == 403
    assert (
        await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}", "X-Fingerprint": "fp-A"}
        )
    ).status_code == 200


async def test_api_key_surface_ignores_fingerprint(client):
    reg = await _register(client, "fp3@x.com", fingerprint="fp-A")
    created = await client.post(
        "/user/keys",
        json={"name": "sdk"},
        headers={"Authorization": f"Bearer {reg['access_token']}", "X-Fingerprint": "fp-A"},
    )
    key = created.json()["key"]
    # SDK clients never send X-Fingerprint; key auth must not demand it.
    resp = await client.get("/user/verify-key", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200


# ── billing: streams, media flat fee, routed pricing ─────────────────────────

async def test_stream_upstream_connect_failure_refunds(client, jwt, priced):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    original = _stub_upstream(handler)
    before = _balance("admin@example.com")
    try:
        resp = await client.post(
            "/user/playground/chat",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=jwt,
        )
        assert resp.status_code == 502, resp.text
    finally:
        upstream._client = original
    assert _balance("admin@example.com") == before  # hold fully refunded


async def test_media_flat_fee_charged_on_success(client, jwt, priced):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xd8\xff-binary-audio", headers={"content-type": "audio/mpeg"})

    created = await client.post("/user/keys", json={"name": "media"}, headers=jwt)
    key = created.json()["key"]

    original = _stub_upstream(handler)
    before = _balance("admin@example.com")
    try:
        resp = await client.post(
            "/v1/audio/speech",
            json={"model": "tts-1", "input": "hello"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200, resp.text
    finally:
        upstream._client = original
    assert before - _balance("admin@example.com") == Decimal("0.020000")


async def test_media_flat_fee_refunded_on_upstream_error(client, jwt):
    created = await client.post("/user/keys", json={"name": "media2"}, headers=jwt)
    key = created.json()["key"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    original = _stub_upstream(handler)
    before = _balance("admin@example.com")
    try:
        resp = await client.post(
            "/v1/audio/speech",
            json={"model": "tts-1", "input": "hello"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 500
    finally:
        upstream._client = original
    assert _balance("admin@example.com") == before


async def test_routed_via_reprices_settlement(client, jwt):
    created = await client.post("/user/keys", json={"name": "routed"}, headers=jwt)
    key = created.json()["key"]
    db = SessionLocal()
    try:
        from app.models import Pricing
        db.add(Pricing(model="expensive-ultra", input_per_1k=Decimal("1"), output_per_1k=Decimal("1")))
        db.commit()
    finally:
        db.close()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
            },
            headers={"X-Routed-Via": "someplatform/expensive-ultra"},
        )

    original = _stub_upstream(handler)
    before = _balance("admin@example.com")
    try:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
    finally:
        upstream._client = original
    # 1k in * 1.0 + 1k out * 1.0 = ◎2 at the *routed* model's rate, not gpt-4o's.
    assert before - _balance("admin@example.com") == Decimal("2.000000")


async def test_gemini_alt_sse_is_treated_as_stream(client, jwt, priced):
    created = await client.post("/user/keys", json={"name": "gemini"}, headers=jwt)
    key = created.json()["key"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":generateContent")
        assert request.url.params.get("alt") == "sse"
        body = (
            'data: {"candidates": [{"content": {"parts": [{"text": "你"}]}}]}\n\n'
            'data: {"candidates": [{"content": {"parts": [{"text": "好"}]}}], '
            '"usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2}}\n\n'
        )
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    original = _stub_upstream(handler)
    before = _balance("admin@example.com")
    try:
        resp = await client.post(
            "/v1beta/models/gemini-2.5-flash:generateContent?alt=sse",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200, resp.text
        # Streamed through verbatim as SSE frames.
        assert 'data: {"candidates"' in resp.text
        assert "usageMetadata" in resp.text
    finally:
        upstream._client = original
    # Charged at gemini-2.5-flash rates: 7/1000*0.0001 + 2/1000*0.0004 = 1.5e-6,
    # which quantises (CENT, HALF_UP) to 0.000002 — billed, not free.
    spent = before - _balance("admin@example.com")
    assert spent == Decimal("0.000002")


# ── settings wiring ──────────────────────────────────────────────────────────

async def test_registration_bonus_comes_from_settings(client, jwt):
    r = await client.put(
        "/admin/settings",
        json=[{"key": "registration_bonus", "value": "7"}],
        headers=jwt,
    )
    assert r.status_code == 200
    reg = await _register(client, "bonus@x.com")
    assert reg["user"]["gavincoin_balance"] == "7.000000"


async def test_coin_rate_drives_package_size(client, jwt):
    await client.put("/admin/settings", json=[{"key": "coin_rate", "value": "10"}], headers=jwt)
    buy = await client.post("/user/packages", json={"cost": "2"}, headers=jwt)
    assert buy.status_code == 201, buy.text
    assert buy.json()["total_tokens"] == 2 * 10 * 1000


async def test_settings_batch_is_atomic(client, jwt):
    r = await client.put(
        "/admin/settings",
        json=[{"key": "coin_rate", "value": "55"}, {"key": "no_such_key", "value": "1"}],
        headers=jwt,
    )
    assert r.status_code == 422
    got = await client.get("/admin/settings", headers=jwt)
    assert got.json()["coin_rate"] == "100"  # first entry was NOT half-applied


async def test_public_config_reflects_settings(client, jwt):
    await client.put(
        "/admin/settings",
        json=[{"key": "registration_bonus", "value": "33"}, {"key": "coin_rate", "value": "42"}],
        headers=jwt,
    )
    r = await client.get("/config")
    assert r.json()["bonus"] == "33"
    assert r.json()["tokenRate"] == 42
