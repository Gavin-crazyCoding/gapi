"""Live model catalog, fetched from the upstream FreeLLM API.

The catalog is not stored: it changes whenever the operator adds a key or a
provider rate-limits, so gapi asks upstream and caches the answer briefly.

Two behaviours matter more than freshness:

* **Single-flight.** A burst of panel loads must produce one upstream call,
  not one per request.
* **Stale-on-error.** If upstream is unreachable, serving a slightly old list
  beats showing the user an empty model picker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.errors import GapiError
from app.services.upstream import get_client

log = logging.getLogger("gapi.catalog")

# Upstream reflects key-health changes within seconds; a few minutes of staleness
# is invisible to a user browsing models and spares the upstream a request per
# page load — important on slow/CF-proxied links where the model picker must not
# block on a live fetch. Stale entries are still served if a refresh fails.
TTL_SECONDS = 300.0
STALE_LIMIT_SECONDS = 3600.0
# Fail fast so a slow upstream makes us serve stale data (or let the client fall
# back to its own cached list) instead of hanging the panel for 20s.
FETCH_TIMEOUT_SECONDS = 8.0

@dataclass
class Catalog:
    models: list[dict[str, Any]]
    fetched_at: float

    @property
    def age(self) -> float:
        return time.monotonic() - self.fetched_at

    def ready(self) -> list[dict[str, Any]]:
        """Models a request could actually reach right now."""
        return [
            m
            for m in self.models
            # Ready models, plus router pseudo-models (auto/fusion and the
            # auto:<profile> chains), which carry no execution_status at all.
            if m.get("available", True)
            and (m.get("execution_status") == "ready" or _is_router(m))
        ]



def _is_router(model: dict[str, Any]) -> bool:
    # Router entries are exactly the rows with no execution_status; matching on
    # that rather than a hardcoded id list keeps auto:<profile> chains working.
    return model.get("execution_status") is None


_cache: Catalog | None = None
_lock = asyncio.Lock()


async def _fetch() -> list[dict[str, Any]]:
    client = get_client()
    resp = await client.get("/models", timeout=FETCH_TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError(f"unexpected /v1/models payload: {type(payload).__name__}")
    return data


async def get_catalog(force: bool = False) -> Catalog:
    """Return the catalog, refreshing it at most once per TTL across callers."""
    global _cache

    if not force and _cache is not None and _cache.age < TTL_SECONDS:
        return _cache

    async with _lock:
        # Another coroutine may have refreshed while we waited for the lock.
        if not force and _cache is not None and _cache.age < TTL_SECONDS:
            return _cache
        try:
            models = await _fetch()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            if _cache is not None and _cache.age < STALE_LIMIT_SECONDS:
                log.warning("catalog refresh failed (%s); serving %.0fs-old cache", exc, _cache.age)
                return _cache
            log.error("catalog unavailable and no usable cache: %s", exc)
            raise GapiError(
                503,
                "catalog_unavailable",
                "Upstream model catalog is unreachable",
            ) from exc
        _cache = Catalog(models=models, fetched_at=time.monotonic())
        log.info("catalog refreshed: %d models", len(models))
        return _cache


def invalidate() -> None:
    """Drop the cache (used by tests and after a pricing sync)."""
    global _cache
    _cache = None
