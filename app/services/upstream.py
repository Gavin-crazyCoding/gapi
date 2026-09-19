"""Shared httpx.AsyncClient for forwarding to the upstream FreeLLM API."""

from __future__ import annotations

import httpx

from app.config import settings


def build_client() -> httpx.AsyncClient:
    """Return a pooled AsyncClient with per-route timeouts."""
    timeout = httpx.Timeout(
        connect=10.0,
        read=settings.upstream_timeout_chat,
        write=30.0,
        pool=10.0,
    )
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
    return httpx.AsyncClient(
        base_url=settings.freellm_api_base,
        headers={
            "Authorization": f"Bearer {settings.freellm_api_key}",
            "Accept-Encoding": "identity",
        },
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
    )


# Module-level singleton; the proxy layer imports this so all routes share
# one connection pool instead of opening a new pool per request.
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = build_client()
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def route_timeout(path: str) -> float:
    """Pick the right read timeout for the upstream path."""
    if "/embeddings" in path:
        return float(settings.upstream_timeout_embeddings)
    if any(s in path for s in ("/images/generations", "/videos/generations", "/audio/speech")):
        return float(settings.upstream_timeout_media)
    return float(settings.upstream_timeout_chat)