"""The guards in front of the chat endpoints, kept free of FastAPI so each one can be tested (and broken on purpose) alone.

  * ConversationSigner: the server issues a conversation id and signs it with a key only the server holds. The signature covers
    the tenant, so an id issued to a visitor of one site is worthless on another, and an id a visitor invents or edits is refused.
  * RateLimiter: a sliding window per key (an IP address, a conversation). Memory is bounded: the oldest key is dropped
    when there are too many.
  * read_capped: a request body is read up to a limit and no further, whatever the Content-Length says.
  * plain_text: a reply leaves the server as text with control characters removed. The widget then draws it as text, never HTML.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import deque
from typing import AsyncIterator, Callable

MAX_TOKEN_CHARS = 200
TOKEN_MAX_AGE_SECONDS = 24 * 3600
MAX_REPLY_CHARS = 2000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class ConversationSigner:
    """`<tenant>.<random id>.<issued at>.<signature>`. The random part is 128 bits; the signature is HMAC-SHA256 over the other
    three. A visitor can see their own id and cannot make another one that verifies, nor move it to another tenant."""

    def __init__(self, key: bytes, *, now: Callable[[], float] = time.time, max_age: int = TOKEN_MAX_AGE_SECONDS):
        if len(key) < 16:
            raise ValueError("the signing key must be at least 16 bytes")
        self._key, self._now, self._max_age = key, now, max_age

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).digest())

    def issue(self, tenant_id: str) -> str:
        payload = "%s.%s.%d" % (tenant_id, secrets.token_urlsafe(16), int(self._now()))
        return payload + "." + self._sign(payload)

    def verify(self, token: object, tenant_id: str) -> str | None:
        """The conversation id (the random part) when the token is genuine, for this tenant and not too old; else None."""
        if not isinstance(token, str) or not (0 < len(token) <= MAX_TOKEN_CHARS):
            return None
        parts = token.split(".")
        if len(parts) != 4:
            return None
        tenant, cid, issued, sig = parts
        if not hmac.compare_digest(sig.encode("utf-8"), self._sign(".".join(parts[:3])).encode("utf-8")):
            return None
        if tenant != tenant_id or not issued.isdigit() or self._now() - int(issued) > self._max_age:
            return None
        return cid


class RateLimiter:
    """At most `limit` hits per `window` seconds per key. `hit` returns (allowed, seconds until a slot frees up)."""

    def __init__(self, limit: int, window: float = 60.0, *, now: Callable[[], float] = time.monotonic, max_keys: int = 20000):
        self.limit, self.window, self._now, self._max_keys = limit, window, now, max_keys
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> tuple[bool, int]:
        t = self._now()
        q = self._hits.get(key)
        if q is None:
            if len(self._hits) >= self._max_keys:
                self._hits.pop(next(iter(self._hits)))  # dict order is insertion order: the oldest key goes
            q = self._hits[key] = deque()
        while q and t - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False, max(1, int(self.window - (t - q[0])) + 1)
        q.append(t)
        return True, 0


class BodyTooLarge(Exception):
    pass


async def read_capped(stream: AsyncIterator[bytes], limit: int) -> bytes:
    """Read a body, refusing as soon as it passes `limit` bytes. A chunked body has no Content-Length to check first."""
    out = bytearray()
    async for chunk in stream:
        out += chunk
        if len(out) > limit:
            raise BodyTooLarge()
    return bytes(out)


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f  ]")


def plain_text(value: object, limit: int = MAX_REPLY_CHARS) -> str:
    """Text for the widget: control characters removed, newlines kept, length capped."""
    return _CONTROL.sub("", str(value))[:limit]


def client_ip(peer: str | None, forwarded_for: str | None, trust_proxy: bool) -> str:
    """The address to rate-limit on. X-Forwarded-For is a header anyone can send, so it counts only when the deployment says a
    proxy of its own sits in front (CHAT_TRUST_PROXY=1), and then only the LAST entry, which that proxy wrote."""
    if trust_proxy and forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if last:
            return last[:64]
    return peer or "unknown"
