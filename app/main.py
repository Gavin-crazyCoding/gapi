"""gapi FastAPI application entrypoint.

Run: uvicorn app.main:app --port 3002 --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import settings
from app.database import get_db
from app.errors import GapiError, gapi_error_handler
from app.routers import admin as admin_router
from app.routers import auth as auth_router
from app.routers import keys as keys_router
from app.routers import panel as panel_router
from app.routers import playground as playground_router
from app.routers import proxy as proxy_router
from app.routers import user as user_router
from app.routers import users as users_router
from app.services.upstream import close_client

log = logging.getLogger("gapi")

# Strict CSP: the panel has no external dependencies, no inline scripts, and
# only talks back to its own origin. Inline style attributes (style="…") still
# need 'unsafe-inline' for styles.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'none'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    ),
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not settings.is_production:
        log.warning(
            "GAPI_JWT_SECRET is the built-in development default — set a long "
            "random secret before exposing this service"
        )
    if not settings.freellm_api_key:
        log.warning("FREELLM_API_KEY is empty — every proxied call will fail upstream auth")
    yield
    # Return pooled upstream connections rather than leaving them to the GC.
    await close_client()


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def create_app() -> FastAPI:
    app = FastAPI(
        title="gapi — FreeLLM user gateway",
        version=__version__,
        lifespan=lifespan,
    )

    # Compress text/JSON/JS before it leaves the origin: the model catalog and
    # tab modules shrink ~80%, which matters on slow/CF-proxied links.
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

    # SDK/browser clients may call gapi cross-origin; proxy auth is key-based.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-Routed-Via",
            "X-Fallback-Attempts",
            "X-Fallback-Trail",
            "X-Fallback-Detail",
            "X-Provider",
            "X-Model",
            "X-Request-ID",
            "X-Gapi-Error",
        ],
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    app.add_exception_handler(GapiError, gapi_error_handler)

    # Without these, FastAPI's own 422/404/405/500 would return `{"detail": …}`
    # with no X-Gapi-Error header — and clients could not tell a gateway error
    # from an upstream error body, which is passed through verbatim.
    @app.exception_handler(RequestValidationError)
    async def _validation(_r: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_error_body("validation_error", "Request body failed validation")
            | {"details": exc.errors()},
            headers={"X-Gapi-Error": "1"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_r: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(f"http_{exc.status_code}", str(exc.detail)),
            headers={"X-Gapi-Error": "1"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_r: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", "Internal server error"),
            headers={"X-Gapi-Error": "1"},
        )

    app.include_router(auth_router.router)
    app.include_router(keys_router.router)
    app.include_router(keys_router.debug_router)
    app.include_router(user_router.router)
    # Admin-only management surface (settings, users, announcements).
    app.include_router(admin_router.router)
    app.include_router(users_router.router)
    # Authenticated panel shell + lazily-loaded tab modules. Must come before
    # both the proxy catch-all and the public static mount.
    app.include_router(panel_router.router)
    # Panel playground (JWT-authed) before the /v1 catch-all.
    app.include_router(playground_router.router)
    # Last: its /v1/{path:path} catch-all must not shadow the panel routes.
    app.include_router(proxy_router.router)

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok", "service": "gapi", "version": __version__}

    @app.get("/config", tags=["meta"])
    def public_config(db=Depends(get_db)):
        """Values the signed-out web panel needs before it has a session.

        bonus/tokenRate mirror the live (admin-editable) system settings so
        what the landing page promises is what actually happens.
        """
        from app.services import coin
        merged = coin.get_settings(db)
        return {
            "currency": settings.default_currency,
            "bonus": merged["registration_bonus"],
            "tokenRate": int(Decimal(merged["coin_rate"])),
        }

    # Public login shell only. The authenticated panel lives in web/panel/ and
    # is delivered by app/routers/panel.py behind the session check.
    public_dir = Path(__file__).resolve().parent.parent / "web" / "public"
    if public_dir.is_dir():
        app.mount("/", StaticFiles(directory=public_dir, html=True), name="web")
    else:
        log.warning("web/public not found at %s; panel disabled", public_dir)

    return app


app = create_app()
