"""Phase 4: panel delivery is session-gated and the public shell leaks nothing."""

async def test_anonymous_cannot_fetch_panel_shell(client):
    r = await client.get("/panel")
    assert r.status_code == 401


async def test_anonymous_cannot_fetch_panel_modules(client):
    for asset in ("panel.js", "tabs/models.js", "tabs/billing.js"):
        r = await client.get(f"/panel/assets/{asset}")
        assert r.status_code == 401, asset


async def test_panel_asset_path_traversal_blocked(client, registered_admin):
    # Even with a session, allow-listing rejects anything outside the modules.
    for evil in (
        "/panel/assets/..%2f..%2f..%2fetc/passwd",
        "/panel/assets/tabs/../../main.py",
        "/panel/assets/panel.html",
    ):
        r = await client.get(evil)
        assert r.status_code == 404, evil


async def test_login_sets_httponly_strict_cookie(client, registered_admin):
    r = await client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "hunter2hunter"},
    )
    assert r.status_code == 200
    cookie = r.headers.get("set-cookie", "")
    assert "gapi_session=" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()


async def test_session_cookie_unlocks_panel(client, registered_admin):
    # The shared client picked up the cookie from the register fixture call.
    r = await client.get("/panel")
    assert r.status_code == 200
    assert "tab-models" in r.text
    rjs = await client.get("/panel/assets/panel.js")
    assert rjs.status_code == 200
    assert "javascript" in rjs.headers["content-type"]


async def test_logout_destroys_session(client, registered_admin):
    await client.post("/auth/logout")
    r = await client.get("/panel")
    assert r.status_code == 401


async def test_public_shell_does_not_leak_panel_surface(client):
    r = await client.get("/")
    assert r.status_code == 200
    body = r.text
    # None of the internal panel endpoints or tab structure may appear in the
    # anonymous payload.
    for marker in ("/user/models", "/user/keys", "/user/billing", "tab-models", "routingSelect"):
        assert marker not in body, marker


async def test_security_headers_present(client):
    r = await client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in {k.lower() for k in r.headers}
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
