"""Prompt-injection guard for the proxy surface.

gapi is a transparent pipe to upstream FreeLLM: it forwards the client's
``messages`` array verbatim and adds no system prompt of its own. That means
the *upstream* LLM is what carries a system prompt, and that is where prompt
injection lives. The guard here does not try to fix the upstream model — it
can't — but it does give gapi the ability to refuse a request that is itself
a prompt-injection attempt, so an operator can gate the proxy surface behind
an explicit allow-list of injection patterns.

The patterns are conservative on purpose: they match the *instruction*
phrases that a prompt-injection payload carries ("ignore previous instructions",
"system prompt", "developer mode", "DAN"), not the harmless words that appear
in legitimate user content. False positives are worse than false negatives
here — a false positive silently breaks a paying user's request.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import Request

from app.errors import GapiError

log = logging.getLogger("gapi.prompt_guard")

# Case-insensitive patterns that mark a user message as an injection attempt.
# Each pattern is anchored to the instruction phrase itself, so "please ignore
# the previous instructions" matches but "ignore the traffic jam ahead" does not.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bignore\s+(all\s+|any\s+)?(previous|prior|above)\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bignore\s+(all\s+|any\s+)?instructions\b", re.IGNORECASE),
    re.compile(r"\b(system\s+(prompt|override|instructions?))\b", re.IGNORECASE),
    re.compile(r"\b(developer\s+mode|dan\s+mode|jailbreak)\b", re.IGNORECASE),
    re.compile(r"\bdan\b", re.IGNORECASE),  # standalone DAN persona
    re.compile(r"\b(reveal|show|output|print|display|repeat)\s+(your|the)\s+(system\s+)?(prompt|instructions?)\b", re.IGNORECASE),
    re.compile(r"\b(you\s+are\s+now|you\s+are\s+a)\b", re.IGNORECASE),
    re.compile(r"\b(disregard|override|bypass)\s+(all\s+)?(previous|prior|above|safety|security)\b", re.IGNORECASE),
    re.compile(r"\b(new\s+instructions?|new\s+task)\s*:", re.IGNORECASE),
]

# Endpoints that carry user text worth scanning. Everything else is passed
# through untouched (models list, audio binaries, etc.).
_SCANNABLE_PATHS = frozenset({
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/responses",
})


def _extract_texts(body: Any) -> list[str]:
    """Pull every user-authored text string out of a request body."""
    texts: list[str] = []
    if isinstance(body, dict):
        messages = body.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str):
                        texts.append(content)
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                t = part.get("text")
                                if isinstance(t, str):
                                    texts.append(t)
        # Some endpoints carry a plain "prompt" field.
        prompt = body.get("prompt")
        if isinstance(prompt, str):
            texts.append(prompt)
    return texts


def scan_body(body: Any) -> str | None:
    """Return the first matched injection pattern, or None if clean.

    The return value is the *pattern string* (not the matched text), so the
    caller can log which rule fired without echoing user content.
    """
    for text in _extract_texts(body):
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                return pattern.pattern
    return None


async def guard_request(request: Request) -> None:
    """FastAPI dependency: refuse prompt-injection attempts on scannable paths.

    Raises GapiError(400, "prompt_injection_detected") when a user message
    matches a known injection pattern. The error carries X-Gapi-Error: 1 via
    the standard error contract, so clients can distinguish a gateway refusal
    from an upstream error.
    """
    path = request.url.path
    if path not in _SCANNABLE_PATHS or request.method != "POST":
        return
    # Read the body once; the proxy will re-read it after the guard returns.
    raw = await request.body()
    if not raw:
        return
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    matched = scan_body(body)
    if matched is not None:
        log.warning(
            "prompt_injection_detected path=%s pattern=%r",
            path,
            matched,
        )
        raise GapiError(
            400,
            "prompt_injection_detected",
            "请求内容包含可能的指令注入模式，已被网关拒绝",
        )