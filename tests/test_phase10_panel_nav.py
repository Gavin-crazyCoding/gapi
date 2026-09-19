"""Phase 10: panel shell must load for fingerprint-bound accounts (navigation
requests can't carry X-Fingerprint), while data APIs stay strict."""

from __future__ import annotations

import pytest


async def _register(client, email, fp, password="hunter2hunter"):
    r = await client.post(
        "/auth/register", json={"email": email, "password": password, "fingerprint": fp}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_bound_account_can_load_panel_shell_and_assets(client):
    reg = await _register(client, "bound@x.com", fp="fp-nav")
    # Cookie session (set by register) with NO fingerprint header — exactly
    # what a browser navigation looks like.
    r = await client.get("/panel")
    assert r.status_code == 200, f"panel shell gated by fingerprint: {r.status_code}"
    r = await client.get("/panel/assets/panel.js")
    assert r.status_code == 200
    r = await client.get("/panel/assets/tabs/overview.js")
    assert r.status_code == 200


async def test_bound_account_data_api_still_requires_fingerprint(client):
    reg = await _register(client, "strict@x.com", fp="fp-data")
    token = reg["access_token"]
    # No header → 403; wrong header → 403; bound header → 200.
    assert (await client.get("/user/balance", headers={"Authorization": f"Bearer {token}"})).status_code == 403
    assert (
        await client.get(
            "/user/balance",
            headers={"Authorization": f"Bearer {token}", "X-Fingerprint": "fp-nope"},
        )
    ).status_code == 403
    assert (
        await client.get(
            "/user/balance",
            headers={"Authorization": f"Bearer {token}", "X-Fingerprint": "fp-data"},
        )
    ).status_code == 200


async def test_anonymous_still_gets_401_on_panel(client):
    assert (await client.get("/panel")).status_code == 401
    assert (await client.get("/panel/assets/panel.js")).status_code == 401
