"""Phase 1 acceptance: key CRUD and key-based authentication."""


async def _make_key(client, token, name="ci"):
    r = await client.post(
        "/user/keys", headers={"Authorization": f"Bearer {token}"}, json={"name": name}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_key_shown_once_never_listed(client, registered_admin):
    token = registered_admin["access_token"]
    created = await _make_key(client, token)
    assert created["key"].startswith("gapi-")
    assert created["key_prefix"] == created["key"][:12]

    listing = await client.get("/user/keys", headers={"Authorization": f"Bearer {token}"})
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert "key" not in rows[0]  # secret never exposed again
    assert rows[0]["key_prefix"] == created["key_prefix"]


async def test_key_auth_three_headers(client, registered_admin):
    token = registered_admin["access_token"]
    key = (await _make_key(client, token))["key"]

    r = await client.get("/user/verify-key", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == "admin@example.com"
    assert body["key_name"] == "ci"

    for header in ("x-api-key", "x-goog-api-key"):
        r = await client.get("/user/verify-key", headers={header: key})
        assert r.status_code == 200, header
        assert r.json()["user"]["role"] == "admin"


async def test_invalid_and_deleted_key_unauthorized(client, registered_admin):
    token = registered_admin["access_token"]
    created = await _make_key(client, token)

    assert (
        await client.get("/user/verify-key", headers={"Authorization": "Bearer gapi-deadbeef"})
    ).status_code == 401
    assert (await client.get("/user/verify-key")).status_code == 401

    r = await client.delete(
        f"/user/keys/{created['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 204
    r = await client.get("/user/verify-key", headers={"Authorization": f"Bearer {created['key']}"})
    assert r.status_code == 401


async def test_keys_isolated_per_user(client, registered_admin):
    other = (
        await client.post(
            "/auth/register", json={"email": "bob@x.com", "password": "password123"}
        )
    ).json()
    await _make_key(client, registered_admin["access_token"], "admin-key")
    await _make_key(client, other["access_token"], "bob-key")

    rows = (
        await client.get("/user/keys", headers={"Authorization": f"Bearer {other['access_token']}"})
    ).json()
    assert len(rows) == 1
    assert rows[0]["name"] == "bob-key"


async def test_last_used_at_updates(client, registered_admin):
    token = registered_admin["access_token"]
    created = await _make_key(client, token)
    assert created["last_used_at"] is None
    await client.get("/user/verify-key", headers={"Authorization": f"Bearer {created['key']}"})
    rows = (await client.get("/user/keys", headers={"Authorization": f"Bearer {token}"})).json()
    assert rows[0]["last_used_at"] is not None
