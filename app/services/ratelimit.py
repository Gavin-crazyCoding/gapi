"""In-process sliding-window rate limiter.

Single-instance gapi keeps the counters in memory — no new dependencies. The
limiter guards credential-stuffing surfaces (login, register, resend), not
the /v1 proxy (upstream already rate-limits per IP).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from app.errors import GapiError

# key -> deque of attempt timestamps (monotonic)
_attempts: dict[str, deque[float]] = defaultdict(deque)
_last_sweep = time.monotonic()
_SWEEP_INTERVAL = 300.0


def _sweep(now: float, max_window: float) -> None:
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL:
        return
    _last_sweep = now
    stale = [k for k, dq in _attempts.items() if not dq or now - dq[-1] > max_window]
    for k in stale:
        _attempts.pop(k, None)


def check(key: str, *, limit: int, window_seconds: float) -> None:
    """Record an attempt; raise GapiError(429) when over the limit."""
    now = time.monotonic()
    _sweep(now, window_seconds)
    dq = _attempts[key]
    while dq and now - dq[0] > window_seconds:
        dq.popleft()
    if len(dq) >= limit:
        raise GapiError(
            429,
            "rate_limited",
            "请求过于频繁，请稍后再试",
        )
    dq.append(now)


def clear() -> None:
    """Reset all counters (tests)."""
    _attempts.clear()
