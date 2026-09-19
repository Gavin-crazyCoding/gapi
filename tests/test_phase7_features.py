"""Phase 7: optimization & feature pack — rate limiting, redemption codes,
password change, resend verification, registration gate, admin stats,
per-key usage."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import RedemptionCode, User
from app.services import billing, ratelimit


@pytest.fixture()
def jwt(registered_admin):
    return {"Authorization": f"Bearer {registered_admin['access_token']}"}


def _balance(email: str) -> Decimal:
    db = SessionLocal()
    try:
        return db.scalar(select(User.gavincoin_balance).where(User.email == email))
    finally:
        db.close()


def _mkcode(code="GAVI-TEST-0000-0001", amount="5", max_uses=1, active=True):
    db = SessionLocal()
    try:
        db.add(
            RedemptionCode(
                code=code, amount=Decimal(amount), max_uses=max_uses, is_active=active
            )
        )
        db.commit()
    finally:
        db.close()


# ── rate limiting ────────────────────────────────────────────────────────────

async def test_login_rate_limit_trips(client):
    await client.post("/auth/register", json={"email": "rl@x.com", "password": "password123"})
    for _ in range(30):
        r = await client.post("/auth/login", json={"email": "rl@x.com", "password": "wrong-pass"})
        assert r.status_code == 401
    r = await client.post("/auth/login", json={"email": "rl@x.com", "password": "wrong-pass"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert r.headers["x-gapi-error"] == "1"


def test_ratelimit_window_slides():
    ratelimit.clear()
    for _ in range(3):
        ratelimit.check("unit", limit=3, window_seconds=60)
    with pytest.raises(Exception) as exc:
        ratelimit.check("unit", limit=3, window_seconds=60)
    assert getattr(exc.value, "status_code", None) == 429
    ratelimit.check("other-key", limit=3, window_seconds=60)  # independent bucket


# ── registration gate ────────────────────────────────────────────────────────

async def test_registration_can_be_closed(client, jwt):
    r = await client.put(
        "/admin/settings", json=[{"key": "registration_open", "value": "false"}], headers=jwt
    )
    assert r.status_code == 200
    r = await client.post("/auth/register", json={"email": "late@x.com", "password": "password123"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "registration_closed"

    await client.put(
        "/admin/settings", json=[{"key": "registration_open", "value": "true"}], headers=jwt
    )
    r = await client.post("/auth/register", json={"email": "late@x.com", "password": "password123"})
    assert r.status_code == 201


async def test_settings_expose_registration_open(client, jwt):
    r = await client.get("/admin/settings", headers=jwt)
    assert r.json()["registration_open"] == "true"


# ── redemption codes ─────────────────────────────────────────────────────────

async def test_redeem_full_lifecycle(client, jwt):
    _mkcode(amount="5")
    before = _balance("admin@example.com")

    r = await client.post("/user/redeem", json={"code": "gavi-test-0000-0001"}, headers=jwt)
    assert r.status_code == 200, r.text
    assert r.json()["redeemed"] == "5.000000"
    assert _balance("admin@example.com") == before + Decimal("5")

    # Same user, same code → refused.
    r = await client.post("/user/redeem", json={"code": "GAVI-TEST-0000-0001"}, headers=jwt)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "code_already_redeemed"

    # Another user: code is single-use and already spent.
    await client.post("/auth/register", json={"email": "second@x.com", "password": "password123"})
    login = await client.post("/auth/login", json={"email": "second@x.com", "password": "password123"})
    ujwt = {"Authorization": f"Bearer {login.json()['access_token']}"}
    r = await client.post("/user/redeem", json={"code": "GAVI-TEST-0000-0001"}, headers=ujwt)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "code_exhausted"


async def test_redeem_campaign_code_multiple_users(client, jwt):
    _mkcode(code="GAVI-CAMP-0000-0002", amount="2", max_uses=2)
    r = await client.post("/user/redeem", json={"code": "GAVI-CAMP-0000-0002"}, headers=jwt)
    assert r.status_code == 200

    await client.post("/auth/register", json={"email": "third@x.com", "password": "password123"})
    login = await client.post("/auth/login", json={"email": "third@x.com", "password": "password123"})
    ujwt = {"Authorization": f"Bearer {login.json()['access_token']}"}
    r = await client.post("/user/redeem", json={"code": "GAVI-CAMP-0000-0002"}, headers=ujwt)
    assert r.status_code == 200

    # Third redemption exceeds max_uses=2.
    await client.post("/auth/register", json={"email": "fourth@x.com", "password": "password123"})
    login = await client.post("/auth/login", json={"email": "fourth@x.com", "password": "password123"})
    ujwt = {"Authorization": f"Bearer {login.json()['access_token']}"}
    r = await client.post("/user/redeem", json={"code": "GAVI-CAMP-0000-0002"}, headers=ujwt)
    assert r.status_code == 409


async def test_redeem_unknown_inactive_and_blank(client, jwt):
    assert (await client.post("/user/redeem", json={"code": "GAVI-NOPE-0000-0000"}, headers=jwt)).status_code == 404
    _mkcode(code="GAVI-DEAD-0000-0003", active=False)
    r = await client.post("/user/redeem", json={"code": "GAVI-DEAD-0000-0003"}, headers=jwt)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "code_inactive"
    r = await client.post("/user/redeem", json={"code": "   "}, headers=jwt)
    assert r.status_code == 400


async def test_redeem_appears_in_ledger(client, jwt):
    _mkcode(code="GAVI-LEDG-0000-0004", amount="3")
    await client.post("/user/redeem", json={"code": "GAVI-LEDG-0000-0004"}, headers=jwt)
    txs = (await client.get("/user/transactions", headers=jwt)).json()
    redeem_tx = next(t for t in txs if t["tx_type"] == "redeem")
    assert redeem_tx["amount"] == "3.000000"
    assert redeem_tx["reference_id"] == "GAVI-LEDG-0000-0004"


# ── password change ──────────────────────────────────────────────────────────

async def test_password_change_flow(client):
    await client.post("/auth/register", json={"email": "pw@x.com", "password": "oldpassword1"})
    login = await client.post("/auth/login", json={"email": "pw@x.com", "password": "oldpassword1"})
    ujwt = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Wrong current password → 401.
    r = await client.post(
        "/user/password",
        json={"old_password": "not-the-password", "new_password": "newpassword2"},
        headers=ujwt,
    )
    assert r.status_code == 401

    r = await client.post(
        "/user/password",
        json={"old_password": "oldpassword1", "new_password": "newpassword2"},
        headers=ujwt,
    )
    assert r.status_code == 200, r.text

    assert (
        await client.post("/auth/login", json={"email": "pw@x.com", "password": "oldpassword1"})
    ).status_code == 401
    assert (
        await client.post("/auth/login", json={"email": "pw@x.com", "password": "newpassword2"})
    ).status_code == 200


async def test_password_change_rejects_weak_new_password(client, jwt):
    r = await client.post(
        "/user/password", json={"old_password": "hunter2hunter", "new_password": "short"}, headers=jwt
    )
    assert r.status_code == 422


# ── resend verification ──────────────────────────────────────────────────────

async def test_resend_verification_flow(client):
    reg = await client.post("/auth/register", json={"email": "vrf@x.com", "password": "password123"})
    ujwt = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    assert reg.json()["user"]["email_verified"] is False

    r = await client.post("/auth/resend-verification", headers=ujwt)
    assert r.status_code == 200, r.text

    # 4th resend inside an hour trips the limit (3/hour).
    assert (await client.post("/auth/resend-verification", headers=ujwt)).status_code == 200
    assert (await client.post("/auth/resend-verification", headers=ujwt)).status_code == 200
    r = await client.post("/auth/resend-verification", headers=ujwt)
    assert r.status_code == 429


async def test_resend_verification_noop_when_verified(client, jwt):
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "admin@example.com"))
        user.email_verified = True
        db.commit()
    finally:
        db.close()
    r = await client.post("/auth/resend-verification", headers=jwt)
    assert r.status_code == 200
    assert "无需重发" in r.json()["detail"]


# ── admin stats ──────────────────────────────────────────────────────────────

async def test_admin_stats_shape_and_auth(client, jwt):
    r = await client.get("/admin/stats", headers=jwt)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_users"] >= 1
    for key in (
        "active_users", "requests_30d", "tokens_30d", "spend_30d",
        "signups_7d", "redeems_30d", "daily", "top_models", "top_users",
    ):
        assert key in body, key

    await client.post("/auth/register", json={"email": "nostat@x.com", "password": "password123"})
    login = await client.post("/auth/login", json={"email": "nostat@x.com", "password": "password123"})
    ujwt = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/admin/stats", headers=ujwt)).status_code == 403


# ── per-key usage ────────────────────────────────────────────────────────────

async def test_key_listing_shows_30d_usage(client, jwt):
    created = await client.post("/user/keys", json={"name": "metered"}, headers=jwt)
    key = created.json()["key"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    from app.services import upstream
    original = upstream._client
    upstream._client = httpx.AsyncClient(
        base_url="http://upstream.test/v1",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer upstream-key"},
    )
    try:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
    finally:
        upstream._client = original

    rows = (await client.get("/user/keys", headers=jwt)).json()
    row = next(k for k in rows if k["name"] == "metered")
    assert row["requests_30d"] == 1
    assert row["tokens_30d"] == 150
