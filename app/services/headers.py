"""Header sanitization for the proxy.

Request side: strip every credential the user sent (Authorization / x-api-key /
x-goog-api-key / Gemini ?key=) and inject the upstream unified key.

Response side: forward everything except hop-by-hop headers (the ASGI server
recomputes framing) so Content-Type, Cache-Control, Retry-After and all the
X-Routed-Via / X-Fallback-* headers reach the client untouched.
"""

from __future__ import annotations

from httpx import Headers as HttpxHeaders

# Hop-by-hop headers must never be forwarded (RFC 2616 §13.5.1 + framing).
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    # recomputed per hop
    "content-length",
}

# Credentials gapi replaces with its own upstream key.
INBOUND_AUTH_HEADERS = {"authorization", "x-api-key", "x-goog-api-key"}

# Headers clients may send that upstream understands and that must survive.
REQUEST_ALLOWLIST_PREFIX = ("x-",)
REQUEST_ALLOWLIST_EXACT = {
    "accept",
    "content-type",
    "user-agent",
    "accept-language",
    "anthropic-version",
    "anthropic-beta",
    "x-request-id",
    "x-codex-session-id",
    "session-id",
    "x-session-id",
    "x-freellm-compress",
    "x-freellm-task-type",
}

# Response headers we never want to leak from our own client stack.
RESPONSE_DROP_EXACT = {"server", "date"}

# Query parameter carrying a Gemini-style key; always removed before forwarding.
SENSITIVE_QUERY_PARAMS = {"key", "api_key"}


def build_upstream_headers(incoming: HttpxHeaders, upstream_key: str) -> dict[str, str]:
    """Produce the request header set for the upstream call."""
    out: dict[str, str] = {"Authorization": f"Bearer {upstream_key}"}
    for name, value in incoming.multi_items():
        lower = name.lower()
        if lower in INBOUND_AUTH_HEADERS:
            continue
        if lower in REQUEST_ALLOWLIST_EXACT or lower.startswith(REQUEST_ALLOWLIST_PREFIX):
            # httpx Headers may contain multiple values; last write wins for
            # singleton headers, which matches upstream expectations.
            out[name] = value
    return out


def forward_response_headers(upstream_headers: HttpxHeaders) -> list[tuple[bytes, bytes]]:
    """Raw (name, value) pairs suitable for ASGI ``raw_headers``."""
    result: list[tuple[bytes, bytes]] = []
    for name, value in upstream_headers.multi_items():
        lower = name.lower()
        if lower in HOP_BY_HOP or lower in RESPONSE_DROP_EXACT:
            continue
        result.append((name.encode(), value.encode()))
    return result


def sanitize_query_params(query: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop Gemini-style credentials from the query string."""
    return [(k, v) for k, v in query if k.lower() not in SENSITIVE_QUERY_PARAMS]
