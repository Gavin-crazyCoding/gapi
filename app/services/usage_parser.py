"""Normalize token usage across the four upstream wire protocols.

- chat:      OpenAI chat/completions, embeddings (usage.prompt_tokens …)
- anthropic: /v1/messages — message_start + message_delta
- gemini:    /v1beta — usageMetadata on the body / final chunk
- responses: /v1/responses — response.completed event

The SSE decoder is incremental: chunks go in as they arrive and complete
frames come out, so the full response is never held in memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum


class WireKind(str, Enum):
    CHAT = "chat"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    RESPONSES = "responses"


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False
    saw_usage: bool = False
    stream_error: dict | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class SSEDecoder:
    """Incremental Server-Sent-Events frame splitter.

    feed() returns zero or more complete (event, data) pairs. Multi-line data
    fields are joined per the SSE spec; upstream sends one data line per frame.
    """

    _buffer: bytes = b""

    def feed(self, chunk: bytes) -> list[tuple[str | None, str]]:
        self._buffer += chunk
        frames: list[tuple[str | None, str]] = []
        # SSE frames are separated by a blank line (\n\n, sometimes \r\n\r\n).
        while b"\n\n" in self._buffer or b"\r\n\r\n" in self._buffer:
            sep = b"\n\n" if b"\n\n" in self._buffer else b"\r\n\r\n"
            raw, self._buffer = self._buffer.split(sep, 1)
            event: str | None = None
            data_lines: list[str] = []
            for line in raw.decode("utf-8", errors="replace").splitlines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if data_lines or event:
                frames.append((event, "\n".join(data_lines)))
        return frames


@dataclass
class StreamUsageTracker:
    kind: WireKind
    usage: TokenUsage = field(default_factory=TokenUsage)

    def feed_frame(self, event: str | None, data: str) -> None:
        if not data or data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(obj, dict):
            return

        if self.kind is WireKind.CHAT:
            self._feed_chat(obj)
        elif self.kind is WireKind.ANTHROPIC:
            self._feed_anthropic(event, obj)
        elif self.kind is WireKind.GEMINI:
            self._feed_gemini(obj)
        elif self.kind is WireKind.RESPONSES:
            self._feed_responses(event, obj)

    # ── chat ────────────────────────────────────────────────────────────────
    def _feed_chat(self, obj: dict) -> None:
        usage = obj.get("usage")
        if isinstance(usage, dict):
            self._merge_chat_usage(usage)
        if isinstance(obj.get("error"), dict):
            self.usage.stream_error = obj["error"]

    def _merge_chat_usage(self, usage: dict) -> None:
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        if prompt is not None:
            self.usage.prompt_tokens = prompt
        if completion is not None:
            self.usage.completion_tokens = completion
        self.usage.saw_usage = True
        if usage.get("estimated"):
            self.usage.estimated = True

    # ── anthropic ─────────────────────────────────────────────────────────
    def _feed_anthropic(self, event: str | None, obj: dict) -> None:
        if event == "message_start":
            u = (obj.get("message") or {}).get("usage") or {}
            prompt = _as_int(u.get("input_tokens")) or 0
            # Cache reads/writes are still input-side tokens.
            prompt += _as_int(u.get("cache_read_input_tokens")) or 0
            prompt += _as_int(u.get("cache_creation_input_tokens")) or 0
            if prompt:
                self.usage.prompt_tokens = prompt
                self.usage.saw_usage = True
        elif event == "message_delta":
            u = obj.get("usage") or {}
            out = _as_int(u.get("output_tokens"))
            if out is not None:
                self.usage.completion_tokens = out
                self.usage.saw_usage = True
        elif event == "error":
            self.usage.stream_error = obj.get("error") if isinstance(obj.get("error"), dict) else obj

    # ── gemini ──────────────────────────────────────────────────────────────
    def _feed_gemini(self, obj: dict) -> None:
        meta = obj.get("usageMetadata")
        if isinstance(meta, dict):
            prompt = _as_int(meta.get("promptTokenCount"))
            completion = _as_int(meta.get("candidatesTokenCount"))
            # Last non-empty metadata is cumulative in Gemini streams.
            if prompt is not None:
                self.usage.prompt_tokens = prompt
            if completion is not None:
                self.usage.completion_tokens = completion
            if prompt is not None or completion is not None:
                self.usage.saw_usage = True
        if isinstance(obj.get("error"), dict):
            self.usage.stream_error = obj["error"]

    # ── responses ───────────────────────────────────────────────────────────
    def _feed_responses(self, event: str | None, obj: dict) -> None:
        if event == "response.completed":
            u = (obj.get("response") or {}).get("usage") or {}
            self._merge_responses_usage(u)
        elif event == "response.failed":
            err = (obj.get("response") or {}).get("error") or obj.get("error")
            self.usage.stream_error = err if isinstance(err, dict) else {"message": "response failed"}
        # Some providers emit a final plain usage object too.
        elif isinstance(obj.get("usage"), dict):
            self._merge_responses_usage(obj["usage"])

    def _merge_responses_usage(self, u: dict) -> None:
        prompt = _as_int(u.get("input_tokens"))
        completion = _as_int(u.get("output_tokens"))
        if prompt is not None:
            self.usage.prompt_tokens = prompt
        if completion is not None:
            self.usage.completion_tokens = completion
        self.usage.saw_usage = True
        if u.get("estimated"):
            self.usage.estimated = True


# ── Non-streaming bodies ────────────────────────────────────────────────────

def extract_usage(kind: WireKind, body: dict) -> TokenUsage:
    tracker = StreamUsageTracker(kind)
    if kind is WireKind.CHAT:
        if isinstance(body.get("usage"), dict):
            tracker._merge_chat_usage(body["usage"])
        elif body.get("prompt_tokens") is not None:  # embeddings-style
            tracker._merge_chat_usage(
                {"prompt_tokens": body.get("prompt_tokens"), "completion_tokens": 0}
            )
    elif kind is WireKind.ANTHROPIC:
        u = body.get("usage") or {}
        prompt = (_as_int(u.get("input_tokens")) or 0) + (
            _as_int(u.get("cache_read_input_tokens")) or 0
        ) + (_as_int(u.get("cache_creation_input_tokens")) or 0)
        completion = _as_int(u.get("output_tokens")) or 0
        tracker.usage = TokenUsage(
            prompt_tokens=prompt, completion_tokens=completion, saw_usage=bool(u)
        )
    elif kind is WireKind.GEMINI:
        meta = body.get("usageMetadata") or {}
        prompt = _as_int(meta.get("promptTokenCount")) or 0
        completion = _as_int(meta.get("candidatesTokenCount")) or 0
        tracker.usage = TokenUsage(
            prompt_tokens=prompt, completion_tokens=completion, saw_usage=bool(meta)
        )
    elif kind is WireKind.RESPONSES:
        u = body.get("usage") or {}
        tracker._merge_responses_usage(u)
    return tracker.usage


def _as_int(v) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None
