"""/v1 and /v1beta proxy: authenticate with a gapi key, bill, forward upstream.

Streaming is forwarded chunk-by-chunk — the full body is never buffered. Usage
is accumulated as bytes pass through, so settlement works even when the client
disconnects mid-stream.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import text, update
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import KeyPrincipal, get_key_principal
from app.errors import GapiError
from app.models import ApiKey, User
from app.services import billing, ratelimit
from app.services.headers import (
    build_upstream_headers,
    forward_response_headers,
    sanitize_query_params,
)
from app.services.prompt_guard import scan_body
from app.services.token_counter import estimate_request_tokens
from app.services.upstream import get_client, route_timeout
from app.services.usage_parser import SSEDecoder, StreamUsageTracker, WireKind, extract_usage

log = logging.getLogger("gapi.proxy")

router = APIRouter(tags=["proxy"])

# Upstream paths gapi exposes, mapped to the wire protocol their usage block
# follows. Anything not listed here is not forwarded.
ROUTES: dict[str, WireKind] = {
    "/v1/chat/completions": WireKind.CHAT,
    "/v1/embeddings": WireKind.CHAT,
    "/v1/images/generations": WireKind.CHAT,
    "/v1/videos/generations": WireKind.CHAT,
    "/v1/audio/speech": WireKind.CHAT,
    "/v1/audio/transcriptions": WireKind.CHAT,
    "/v1/messages": WireKind.ANTHROPIC,
    "/v1/responses": WireKind.RESPONSES,
}

# Endpoints that produce no billable tokens (or bill by another unit entirely).
UNMETERED = {"/v1/models", "/v1beta/models"}

# 5xx from upstream is worth one retry for a non-streaming request; a streamed
# response cannot be retried once bytes have reached the client.
RETRY_STATUSES = {500, 502, 503, 504}
MAX_RETRIES = 2

# A user's stored routing strategy is expressed to upstream via its `auto:*`
# suffix (see docs/api/01-rest-api.md). Only a bare "auto" is rewritten: an
# explicit model or an explicit per-request `auto:something` always wins.
STRATEGY_TO_UPSTREAM: dict[str, str] = {
    "fastest": "auto:fast",
    "smart": "auto:smart",
    "balanced": "auto:balanced",
    "stable": "auto:reliable",
}


def _wire_kind(path: str) -> WireKind:
    if path in ROUTES:
        return ROUTES[path]
    if path.startswith("/v1beta"):
        return WireKind.GEMINI
    return WireKind.CHAT


def _model_of(body: dict, path: str) -> str:
    model = body.get("model")
    if isinstance(model, str) and model:
        return model
    # Gemini puts the model in the path: /v1beta/models/gemini-2.5-flash:generateContent
    if "/models/" in path:
        tail = path.split("/models/", 1)[1]
        return tail.split(":", 1)[0] or "unknown"
    return "unknown"


def _max_tokens_of(body: dict) -> int:
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        v = body.get(key)
        if isinstance(v, int) and v > 0:
            return v
    gen = body.get("generationConfig")
    if isinstance(gen, dict) and isinstance(gen.get("maxOutputTokens"), int):
        return gen["maxOutputTokens"]
    return billing.DEFAULT_MAX_TOKENS


def _is_stream(body: dict, path: str, params: list[tuple[str, str]] | None = None) -> bool:
    if body.get("stream") is True:
        return True
    if "streamGenerateContent" in path:
        return True
    # Gemini's generateContent also streams when ?alt=sse is in the query.
    if params and any(k.lower() == "alt" and v.lower() == "sse" for k, v in params):
        return True
    return False


async def _forward_once(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    params: list[tuple[str, str]],
    content: bytes,
    timeout: float,
) -> httpx.Response:
    return await client.request(
        method,
        path,
        headers=headers,
        params=params,
        content=content,
        timeout=timeout,
    )


@router.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
async def proxy_v1(
    path: str,
    request: Request,
    principal: KeyPrincipal = Depends(get_key_principal),
    db: Session = Depends(get_db),
):
    return await run_proxy(f"/v1/{path}", request, principal.user, principal.api_key, db)


@router.api_route(
    "/v1beta/{path:path}",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def proxy_v1beta(
    path: str,
    request: Request,
    principal: KeyPrincipal = Depends(get_key_principal),
    db: Session = Depends(get_db),
):
    return await run_proxy(f"/v1beta/{path}", request, principal.user, principal.api_key, db)


def _acquire_slot(db: Session, user_id: int) -> None:
    """Atomically take one concurrency slot, or raise 429.

    Same BEGIN IMMEDIATE discipline as billing: the check and the increment
    are one guarded UPDATE, so two racing requests cannot both pass.
    """
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        result = db.execute(
            update(User)
            .where(User.id == user_id, User.concurrent_active < User.concurrent_limit)
            .values(concurrent_active=User.concurrent_active + 1)
        )
        if result.rowcount == 0:
            db.rollback()
            raise GapiError(429, "concurrency_limit", "并发请求数已达上限")
        db.commit()
    except Exception:
        db.rollback()
        raise


def _release_slot(db: Session, user_id: int) -> None:
    """Give a slot back. Guarded against going negative; never raises."""
    try:
        db.execute(
            update(User)
            .where(User.id == user_id, User.concurrent_active > 0)
            .values(concurrent_active=User.concurrent_active - 1)
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("failed to release concurrency slot for user %s", user_id)


def _routed_rates(db: Session, headers: httpx.Headers, requested_model: str) -> "billing.Rates | None":
    """Re-price from X-Routed-Via when upstream served a different model.

    The client may send ``model=auto`` (or any alias); the platform/model that
    actually answered arrives in this header (design D6), and that is the rate
    the operator intends to bill at. Returns None to keep the reserved rates.
    """
    routed = headers.get("x-routed-via")
    if not routed or "/" not in routed:
        return None
    routed_model = routed.rsplit("/", 1)[1].strip()
    if not routed_model or routed_model == requested_model:
        return None
    return billing.get_rates(db, routed_model)


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    """Read the request body, refusing anything over ``limit`` bytes.

    The upstream 25 MB cap is upstream's, not ours: the body is materialized
    in memory before billing, so the gateway needs its own bound. The
    Content-Length check is a cheap early rejection; the streamed count is
    the real guard, since the header can be absent (chunked) or a lie, and
    the read aborts as soon as the limit is crossed instead of allocating
    the whole body first.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise GapiError(
                    413, "payload_too_large", f"Request body exceeds {limit} bytes"
                )
        except ValueError:
            pass  # a garbage Content-Length falls through to the streamed count
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise GapiError(
                413, "payload_too_large", f"Request body exceeds {limit} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def run_proxy(
    full_path: str,
    request: Request,
    user: User,
    api_key: ApiKey | None,
    db: Session,
    *,
    record_endpoint: str | None = None,
):
    """Forward a request to upstream, billing ``user``.

    ``api_key`` is None for panel-internal callers (the playground); their
    usage records carry no key id. ``record_endpoint`` overrides the endpoint
    label written to usage_records (e.g. ``/playground/chat``).
    """
    # Rate limit check: 100 requests per hour per API key to prevent abuse
    api_key_prefix = api_key.key_prefix if api_key else "unknown"
    ratelimit.check(f"proxy:{api_key_prefix}:{request.client.host if request.client else 'unknown'}", limit=100, window_seconds=3600)
    if full_path not in ROUTES and full_path not in UNMETERED and not full_path.startswith("/v1beta"):
        raise GapiError(404, "endpoint_not_forwarded", f"{full_path} is not proxied by gapi")

    raw = await _read_bounded_body(request, settings.max_body_bytes)
    body: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                body = parsed
        except json.JSONDecodeError:
            # Non-JSON payloads (multipart audio, say) forward untouched and
            # simply bill nothing on the prompt side.
            body = {}

    # Prompt-injection guard: scan the user-authored text *before* billing or
    # forwarding. The upstream LLM carries its own system prompt and gapi does
    # not inject one, so this is a gateway-level refusal, not a model fix.
    if scan_body(body) is not None:
        raise GapiError(
            400,
            "prompt_injection_detected",
            "请求内容包含可能的指令注入模式，已被网关拒绝",
        )

    request_id = request.headers.get("x-request-id") or f"gapi-{uuid.uuid4().hex[:16]}"
    kind = _wire_kind(full_path)
    model = _model_of(body, full_path)

    # Apply the user's routing strategy. Only a bare "auto" on a /v1 JSON call
    # is rewritten — explicit models and explicit auto:<profile> pass through,
    # and Gemini carries its model in the path rather than the body.
    if request.method == "POST" and full_path.startswith("/v1/") and model == "auto":
        upstream_model = STRATEGY_TO_UPSTREAM.get(user.routing_strategy or "auto")
        if upstream_model:
            body["model"] = upstream_model
            model = upstream_model
            # content-length is a hop-by-hop header here (headers.py strips it),
            # so httpx re-framing the re-encoded body is all that is needed.
            raw = json.dumps(body, ensure_ascii=False).encode()

    endpoint_label = record_endpoint or full_path
    api_key_id = api_key.id if api_key is not None else None
    user_id = user.id

    upstream_path = full_path[len("/v1") :] if full_path.startswith("/v1/") else full_path
    # headers.py works on httpx.Headers (multi_items); Starlette exposes the
    # same data as raw (bytes, bytes) pairs.
    headers = build_upstream_headers(httpx.Headers(request.headers.raw), settings.freellm_api_key)
    headers["x-request-id"] = request_id
    params = sanitize_query_params(list(request.query_params.multi_items()))
    timeout = route_timeout(full_path)

    streaming = _is_stream(body, full_path, params)
    metered = full_path not in UNMETERED and request.method == "POST"

    # Atomic slot acquire: one guarded UPDATE does check-and-increment.
    _acquire_slot(db, user_id)
    slot_released = False

    def release_slot() -> None:
        nonlocal slot_released
        if not slot_released:
            slot_released = True
            _release_slot(db, user_id)

    reservation = None
    if metered:
        prompt_estimate = estimate_request_tokens(body, model)
        reservation = billing.reserve(
            db,
            user,
            model,
            _max_tokens_of(body),
            prompt_estimate,
            request_id,
            flat_fee=billing.get_flat_fee(db, model, full_path),
        )

    started = time.perf_counter()

    client = get_client()

    # Streaming hands the slot to the response generator: the slot stays held
    # for the whole stream, and body_iter releases it when the last byte (or
    # an error) goes out. Everything else releases in the finally below.
    deferred = False
    try:
        if streaming:
            deferred = True
            return await _stream(
                client,
                request.method,
                upstream_path,
                headers=headers,
                params=params,
                content=raw,
                timeout=timeout,
                kind=kind,
                db=db,
                user=user,
                api_key_id=api_key_id,
                reservation=reservation,
                endpoint=endpoint_label,
                started=started,
                request_id=request_id,
                on_done=release_slot,
            )

        last_exc: Exception | None = None
        response: httpx.Response | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await _forward_once(
                    client,
                    request.method,
                    upstream_path,
                    headers=headers,
                    params=params,
                    content=raw,
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES - 1:
                    break
                continue
            if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                continue
            break

        duration_ms = int((time.perf_counter() - started) * 1000)

        if response is None:
            if reservation is not None:
                billing.settle(
                    db,
                    reservation,
                    prompt_tokens=0,
                    completion_tokens=0,
                    endpoint=endpoint_label,
                    api_key_id=api_key_id,
                    duration_ms=duration_ms,
                    status="upstream_error",
                    charge_flat=False,
                )
            raise GapiError(502, "upstream_unreachable", f"Upstream request failed: {last_exc}")

        prompt_tokens = completion_tokens = 0
        if reservation is not None:
            try:
                payload = response.json()
                # Gemini answers with a JSON *array* when not using alt=sse;
                # only dict bodies carry a usage block.
                if isinstance(payload, dict):
                    usage = extract_usage(kind, payload)
                    prompt_tokens, completion_tokens = usage.prompt_tokens, usage.completion_tokens
            except (json.JSONDecodeError, ValueError):
                pass
            billing.settle(
                db,
                reservation,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                endpoint=endpoint_label,
                api_key_id=api_key_id,
                duration_ms=duration_ms,
                status="ok" if response.status_code < 400 else "error",
                charge_flat=response.status_code < 400,
                rates_override=_routed_rates(db, response.headers, reservation.model),
            )

        out_headers = forward_response_headers(response.headers)
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers={k.decode(): v.decode() for k, v in out_headers},
        )
    finally:
        if not deferred:
            release_slot()


async def _stream(
    client: httpx.AsyncClient,
    method: str,
    upstream_path: str,
    *,
    headers: dict[str, str],
    params: list[tuple[str, str]],
    content: bytes,
    timeout: float,
    kind: WireKind,
    db: Session,
    user: User,
    api_key_id: int | None,
    reservation,
    endpoint: str,
    started: float,
    request_id: str,
    on_done,
):
    """Stream upstream bytes through, settling usage and the slot at the end.

    ``on_done`` releases the concurrency slot: it fires when the stream ends
    (cleanly or not), which is when this request actually stops occupying the
    upstream — not when this coroutine returns the response object.
    """
    req = client.build_request(
        method, upstream_path, headers=headers, params=params, content=content, timeout=timeout
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        # The reserve hold must come back; without this the user pays for a
        # request that never reached the upstream.
        on_done()
        if reservation is not None:
            billing.settle(
                db,
                reservation,
                prompt_tokens=0,
                completion_tokens=0,
                endpoint=endpoint,
                api_key_id=api_key_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="upstream_error",
                charge_flat=False,
            )
        raise GapiError(502, "upstream_unreachable", f"Upstream request failed: {exc}") from exc

    if upstream.status_code >= 400:
        # Error bodies are short; read and pass through verbatim, then refund.
        on_done()
        payload = await upstream.aread()
        await upstream.aclose()
        if reservation is not None:
            billing.settle(
                db,
                reservation,
                prompt_tokens=0,
                completion_tokens=0,
                endpoint=endpoint,
                api_key_id=api_key_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="error",
                charge_flat=False,
            )
        return Response(
            content=payload,
            status_code=upstream.status_code,
            headers={
                k.decode(): v.decode() for k, v in forward_response_headers(upstream.headers)
            },
        )

    tracker = StreamUsageTracker(kind)
    decoder = SSEDecoder()
    settled = False
    routed_rates = _routed_rates(db, upstream.headers, reservation.model) if reservation else None

    def _settle(status: str) -> None:
        nonlocal settled
        if settled or reservation is None:
            return
        settled = True
        try:
            billing.settle(
                db,
                reservation,
                prompt_tokens=tracker.usage.prompt_tokens,
                completion_tokens=tracker.usage.completion_tokens,
                endpoint=endpoint,
                api_key_id=api_key_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status=status,
                rates_override=routed_rates,
            )
        except Exception:
            log.exception("settlement failed for %s", request_id)

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes():
                for event, data in decoder.feed(chunk):
                    tracker.feed_frame(event, data)
                yield chunk
        except BaseException:
            # Client hung up, or upstream broke. Bill what was produced so far
            # and let the exception propagate so the server tears the socket down.
            _settle("interrupted")
            on_done()
            await upstream.aclose()
            raise
        else:
            _settle("ok")
            on_done()
            await upstream.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers={k.decode(): v.decode() for k, v in forward_response_headers(upstream.headers)},
    )
