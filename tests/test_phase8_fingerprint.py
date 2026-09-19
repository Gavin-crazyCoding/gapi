"""Phase 8: multi-fingerprint binding semantics (drift tolerance + escape hatch)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import UserFingerprint


def _bindings(email: str) -> list[str]:
    db = SessionLocal()
    try:
        return [
            r.fp_hash
            for r in db.scalars(
                select(UserFingerprint)
                .join(UserFingerprint.user)
                .where(UserFingerprint.user.has(email=email))
            ).all()
        ]
    finally:
        db.close()


async def _register(client, email, fp=None, password="hunter2hunter"):
    body = {"email": email, "password": password}
    if fp:
        body["fingerprint"] = fp
    r = await client.post("/auth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _login(client, email, fp=None, password="hunter2hunter"):
    body = {"email": email, "password": password}
    if fp:
        body["fingerprint"] = fp
    return await client.post("/auth/login", json=body)


async def test_login_registers_each_new_environment(client):
    await _register(client, "multi@x.com", fp="fp-1")
    for fp in ("fp-2", "fp-3"):
        r = await _login(client, "multi@x.com", fp=fp)
        assert r.status_code == 200, fp
    assert sorted(_bindings("multi@x.com")) == ["fp-1", "fp-2", "fp-3"]


async def test_oldest_binding_evicted_beyond_cap(client):
    await _register(client, "cap@x.com", fp="fp-old")
    for i in range(1, 7):  # 6 more, cap is 5
        r = await _login(client, "cap@x.com", fp=f"fp-new-{i}")
        assert r.status_code == 200

    bindings = _bindings("cap@x.com")
    assert len(bindings) == 5
    assert "fp-old" not in bindings  # least-recently-seen evicted first
    assert "fp-new-6" in bindings

    # The evicted fingerprint no longer passes the panel check.
    login = await _login(client, "cap@x.com", fp="fp-new-6")
    token = login.json()["access_token"]
    assert (
        await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}", "X-Fingerprint": "fp-old"}
        )
    ).status_code == 403


async def test_admin_clear_unbinds_everything(client):
    reg = await _register(client, "boss@x.com", fp="fp-1")  # first user → admin
    admin = {"Authorization": f"Bearer {reg['access_token']}", "X-Fingerprint": "fp-1"}
    victim = await _register(client, "locked@x.com", fp="fp-9")
    uid = victim["user"]["id"]

    r = await client.delete(f"/users/{uid}/fingerprints", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["cleared"] == 1
    assert _bindings("locked@x.com") == []

    # With no bindings left the panel check is open; the next login re-binds.
    login = await _login(client, "locked@x.com", fp="fp-10")
    assert login.status_code == 200
    assert _bindings("locked@x.com") == ["fp-10"]


async def test_register_without_fingerprint_binds_at_first_login(client):
    await _register(client, "curl@x.com")  # no fingerprint (API client)
    assert _bindings("curl@x.com") == []

    login = await _login(client, "curl@x.com", fp="fp-browser")
    assert login.status_code == 200
    assert _bindings("curl@x.com") == ["fp-browser"]


async def test_delete_user_cascades_fingerprints(client):
    reg = await _register(client, "admin2@x.com", fp="fp-1")
    admin = {"Authorization": f"Bearer {reg['access_token']}", "X-Fingerprint": "fp-1"}
    victim = await _register(client, "gone@x.com", fp="fp-g")
    assert _bindings("gone@x.com") == ["fp-g"]
    r = await client.delete(f"/users/{victim['user']['id']}", headers=admin)
    assert r.status_code == 204
    assert _bindings("gone@x.com") == []
