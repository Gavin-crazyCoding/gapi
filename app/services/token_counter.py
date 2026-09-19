"""tiktoken fallback for when upstream returns no usage block.

Only used when :mod:`app.services.usage_parser` reports ``saw_usage == False``.
Encoders are cached because building one costs a download on first use.
"""

from __future__ import annotations

import logging
from functools import lru_cache

log = logging.getLogger("gapi.tokens")

# ~4 characters per token: the standard rough English ratio, used when tiktoken
# is unavailable (no network for the BPE download, say). Never silently exact.
_CHARS_PER_TOKEN = 4

# Per-message framing overhead in the OpenAI chat format.
_TOKENS_PER_MESSAGE = 4


@lru_cache(maxsize=32)
def _encoder(model: str):
    """Return a tiktoken encoder, or None if tiktoken cannot be used.

    Returning None rather than raising is load-bearing: lru_cache does not
    cache exceptions, so a raising version would re-attempt the BPE download
    on every single call and add seconds of latency to each request on a host
    with no network.
    """
    try:
        import tiktoken
    except ImportError:
        log.warning("tiktoken not installed; token estimates use a character ratio")
        return None
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        # Unknown model name: cl100k_base is right for the GPT-4/3.5 family and
        # close enough for everything else we bill.
        pass
    except Exception as exc:
        log.warning("tiktoken encoder for %s unavailable (%s)", model, exc)
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception as exc:
        log.warning("tiktoken cl100k_base unavailable (%s); using character ratio", exc)
        return None


def count_text(text: str, model: str = "gpt-4o") -> int:
    if not text:
        return 0
    enc = _encoder(model)
    if enc is None:
        return max(1, len(text) // _CHARS_PER_TOKEN)
    try:
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        return max(1, len(text) // _CHARS_PER_TOKEN)


def count_messages(messages: list[dict], model: str = "gpt-4o") -> int:
    """Estimate prompt tokens for an OpenAI-style ``messages`` array."""
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        total += _TOKENS_PER_MESSAGE
        for key, value in msg.items():
            if isinstance(value, str):
                total += count_text(value, model)
            elif isinstance(value, list):
                # Multimodal content parts: count the text ones, ignore images
                # (their token cost is provider-specific and upstream reports it).
                for part in value:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total += count_text(part["text"], model)
    return total


def estimate_request_tokens(body: dict, model: str = "gpt-4o") -> int:
    """Prompt-side estimate for any of the request shapes we forward."""
    if not isinstance(body, dict):
        return 0
    if isinstance(body.get("messages"), list):
        return count_messages(body["messages"], model)
    prompt = body.get("prompt") or body.get("input")
    if isinstance(prompt, str):
        return count_text(prompt, model)
    if isinstance(prompt, list):
        return sum(count_text(p, model) for p in prompt if isinstance(p, str))
    return 0
