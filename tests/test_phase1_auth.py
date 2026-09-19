"""Phase 1 acceptance: register → login → JWT, roles, error shapes."""

import httpx

from app.main import app


async def test_first_user_is_admin_second_is_user(client):
    r1 = await client.post("/auth/register", json={"email": "a@x.com", "password": "password123"})
    assert r1.status_code == 201
    assert r1.json()["user"]["role"] == "admin"

    r2 = await client.post("/auth/register", json={"email": "b@x.com", "password": "password123"})
    assert r2.status_code == 201
    assert r2.json()["user"]["role"] == "user"


async def test_duplicate_email_conflicts(client, registered_admin):
    r = await client.post(
        "/auth/register",
        json={"email": "admin@example.com", "password": "password123"},
    )
    assert r.status_code == 409
    assert r.headers.get("x-gapi-error") == "1"
    assert r.json()["error"]["code"] == "email_taken"


async def test_email_case_insensitive_and_trimmed(client):
    await client.post(
        "/auth/register", json={"email": "Alice@Example.com  ", "password": "password123"}
    )
    r = await client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "password123"}
    )
    assert r.status_code == 200


async def test_login_wrong_password(client, registered_admin):
    r = await client.post(
        "/auth/login", json={"email": "admin@example.com", "password": "nope-nope"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"


async def test_weak_password_rejected(client):
    r = await client.post("/auth/register", json={"email": "weak@x.com", "password": "short"})
    assert r.status_code == 422


async def test_me_requires_valid_jwt(client, registered_admin):
    # A fresh client carries neither a bearer token nor the session cookie
    # (register/login set one on the shared fixture client).
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gapi.test"
    ) as fresh:
        assert (await fresh.get("/auth/me")).status_code == 401
        assert (
            await fresh.get("/auth/me", headers={"Authorization": "Bearer garbage"})
        ).status_code == 401

    r = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {registered_admin['access_token']}"}
    )
    assert r.status_code == 200
    assert r.json()["email"] == "admin@example.com"
