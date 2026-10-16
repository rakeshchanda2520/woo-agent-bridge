"""Cross-cutting guard rails: rate limiting, PII masking, untrusted-text fencing, log redaction."""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

from pydantic import BaseModel


class TokenBucket:
    """Client-side throttle so we never hammer a merchant's store.

    ponytail: per-process bucket. Several replicas sharing one store need a shared limiter (e.g. Redis).
    """

    def __init__(
        self,
        rate_per_sec: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.rate, self.capacity = rate_per_sec, float(burst)
        self.tokens, self.updated = float(burst), clock()
        self._clock, self._sleep = clock, sleep
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:  # FIFO-ish: callers queue on the lock instead of racing
            while True:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await self._sleep((1 - self.tokens) / self.rate)

    def penalize(self) -> None:
        """Server said 429: drain the bucket so queued callers back off too, not just the one that failed."""
        self.tokens = min(self.tokens, 0.0)


# ---------- PII ----------


def mask_email(email: str) -> str:
    if "@" not in email:
        return "***" if email else ""
    local, domain = email.split("@", 1)
    return f"{local[:2]}***@{domain}"


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return f"******{digits[-4:]}" if len(digits) >= 4 else ("***" if phone else "")


# ---------- Untrusted text ----------


class UntrustedText(BaseModel):
    """Free text written by a shopper or third party. The agent must treat it as data, never as instructions."""

    untrusted: bool = True
    source: str
    text: str


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def fence(text: str | None, source: str, limit: int = 500) -> UntrustedText | None:
    if not text:
        return None
    clean = _CONTROL.sub(" ", text).strip()
    if len(clean) > limit:
        clean = clean[:limit] + " …[truncated]"
    return UntrustedText(source=source, text=clean)


_TAGS = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str:
    return _TAGS.sub("", text or "").strip()


# ---------- Logging ----------


class RedactingFilter(logging.Filter):
    """Last line of defence: scrub key/secret-looking strings from any log record."""

    _SECRET = re.compile(r"\b(c[ks]_[0-9a-f]{20,})\b|(consumer_(?:key|secret)=)[^&\s]+", re.I)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg = self._SECRET.sub(lambda m: (m.group(2) or "") + "[REDACTED]", msg)
        record.args = None
        return True
