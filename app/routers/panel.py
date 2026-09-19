"""Authenticated delivery of the user panel shell and its JS modules.

The public StaticFiles mount only serves the login shell. Everything the panel
needs (its markup and per-tab modules) goes through these routes, so an
anonymous attacker probing the server learns nothing about the panel's
structure or its endpoint surface. Modules are additionally fetched on demand
by the browser, so a normal session only downloads the tabs the user opens.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.deps import User, get_current_user_lenient

router = APIRouter(tags=["panel"], include_in_schema=False)

_PANEL_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "panel"

# Explicit allow-list: even if other files land in web/panel/, they are not
# web-served. Each entry is a tab module lazy-loaded the first time it opens.
_ASSETS = {
    "panel.js",
    "tabs/overview.js",
    "tabs/playground.js",
    "tabs/models.js",
    "tabs/keys.js",
    "tabs/docs.js",
    "tabs/billing.js",
    "tabs/usage.js",
    "tabs/settings.js",
    "tabs/stats.js",
    "tabs/users.js",
    "tabs/announcements.js",
}


def _guarded(rel: str) -> Path | None:
    """Resolve an allow-listed relative path, rejecting any traversal."""
    if rel not in _ASSETS:
        return None
    candidate = (_PANEL_DIR / rel).resolve()
    try:
        candidate.relative_to(_PANEL_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


@router.get("/panel")
def panel_shell(user: User = Depends(get_current_user_lenient)) -> FileResponse:
    return FileResponse(
        _PANEL_DIR / "panel.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/panel/assets/{name:path}")
def panel_asset(name: str, user: User = Depends(get_current_user_lenient)) -> FileResponse:
    path = _guarded(name)
    if path is None:
        from app.errors import not_found

        raise not_found("asset_not_found", "Unknown panel asset")
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # ES modules need a JS MIME type or browsers refuse to execute them.
    if path.suffix == ".js":
        media = "text/javascript; charset=utf-8"
    # no-cache = revalidate every load: FileResponse emits ETag/Last-Modified,
    # so unchanged modules still come back 304 (zero bytes) but a fresh deploy
    # is picked up on the very next click. A max-age here once served a stale
    # panel.js for five minutes and the UI appeared "not to work".
    return FileResponse(
        path,
        media_type=media,
        headers={"Cache-Control": "no-cache"},
    )
