"""The ONLY module that talks HTTP to WooCommerce. Everything above it works with plain dicts."""

import asyncio
import email.utils
import logging
import random
import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .errors import ConnectorError
from .guards import TokenBucket

log = logging.getLogger("woo_agent_bridge.client")

API_PREFIX = "/wp-json/wc/v3"
RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_BACKOFF_S = 30.0


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int
    total_pages: int


class WooClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None, sleep=asyncio.sleep):
        scheme = urlsplit(settings.base_url).scheme
        if scheme not in ("https", "http"):
            raise ConnectorError("BAD_REQUEST", f"WOO_BASE_URL must be http(s), got {settings.base_url!r}")
        self._key = settings.consumer_key.get_secret_value()
        self._secret = settings.consumer_secret.get_secret_value()

        auth: httpx.Auth | None = None
        self._query_auth: dict[str, str] = {}
        if scheme == "https":
            auth = httpx.BasicAuth(self._key, self._secret)
        elif settings.allow_insecure_http:
            # WooCommerce rejects Basic auth over plain http; it only accepts keys in the query string there.
            log.warning("INSECURE: plain-http store, API keys are sent in the query string. Never use in production.")
            self._query_auth = {"consumer_key": self._key, "consumer_secret": self._secret}
        else:
            raise ConnectorError(
                "INSECURE_TRANSPORT",
                "Refusing to send API keys to a plain-http store.",
                hint="Serve the store over HTTPS, or set WOO_ALLOW_INSECURE_HTTP=true for a local demo.",
            )

        self._http = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + API_PREFIX,
            auth=auth,
            timeout=settings.timeout_s,
            verify=ssl.create_default_context(cafile=settings.ca_bundle) if settings.ca_bundle else True,
            transport=transport,
            headers={"User-Agent": "woo-agent-bridge/0.1 (+Razorpay Agent Studio connector POC)"},
        )
        self._bucket = TokenBucket(settings.rate_per_sec, settings.burst, sleep=sleep)
        self._slots = asyncio.Semaphore(settings.max_concurrency)
        self._max_retries = settings.max_retries
        self._sleep = sleep
        self.stats = {"requests": 0, "retries": 0, "rate_limited": 0}  # surfaced in demo/load test

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------- public ----------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._request(path, params)
        return self._json(resp)

    async def get_page(self, path: str, params: dict[str, Any] | None = None) -> Page:
        resp = await self._request(path, params)
        return Page(
            items=self._json(resp),
            total=int(resp.headers.get("X-WP-Total", 0)),
            total_pages=int(resp.headers.get("X-WP-TotalPages", 0)),
        )

    # ---------- internals ----------

    async def _request(self, path: str, params: dict[str, Any] | None) -> httpx.Response:
        query = {k: v for k, v in (params or {}).items() if v is not None} | self._query_auth
        attempt = 0
        while True:
            await self._bucket.acquire()
            async with self._slots:
                started = time.monotonic()
                self.stats["requests"] += 1
                try:
                    resp = await self._http.get(path, params=query)
                except httpx.TransportError as exc:
                    resp, transport_error = None, exc
                else:
                    transport_error = None
            elapsed_ms = int((time.monotonic() - started) * 1000)

            if resp is not None:
                log.info("GET %s -> %s in %sms (attempt %s)", path, resp.status_code, elapsed_ms, attempt + 1)
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in RETRYABLE_STATUS:
                    raise self._map_error(resp)
                if resp.status_code == 429:
                    self.stats["rate_limited"] += 1
                    self._bucket.penalize()
            else:
                log.warning(
                    "GET %s -> network error %s (attempt %s)", path, type(transport_error).__name__, attempt + 1
                )

            if attempt >= self._max_retries:
                raise self._give_up(resp, transport_error)
            wait = self._backoff(attempt, resp)
            attempt += 1
            self.stats["retries"] += 1
            await self._sleep(wait)

    @staticmethod
    def _retry_after(resp: httpx.Response | None) -> float | None:
        raw = resp.headers.get("Retry-After") if resp is not None else None
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            parsed = email.utils.parsedate_to_datetime(raw)  # HTTP-date form
            return max(0.0, parsed.timestamp() - time.time()) if parsed else None

    def _backoff(self, attempt: int, resp: httpx.Response | None) -> float:
        server_hint = self._retry_after(resp)
        if server_hint is not None:
            return min(MAX_BACKOFF_S, server_hint) + random.uniform(0, 0.25)  # noqa: S311 - jitter, not crypto
        return random.uniform(0, min(MAX_BACKOFF_S, 0.5 * 2**attempt))  # noqa: S311 - "full jitter" backoff

    def _give_up(self, resp: httpx.Response | None, transport_error: Exception | None) -> ConnectorError:
        if resp is not None and resp.status_code == 429:
            return ConnectorError(
                "RATE_LIMITED",
                "The store kept rate-limiting after several retries.",
                retryable=True,
                retry_after_s=self._retry_after(resp) or 5.0,
                hint="Wait and retry; tell the customer you are checking.",
            )
        if transport_error is not None:
            return ConnectorError(
                "UPSTREAM_ERROR",
                f"Could not reach the store ({type(transport_error).__name__}).",
                retryable=True,
                retry_after_s=5.0,
            )
        return ConnectorError(
            "UPSTREAM_ERROR",
            f"Store returned HTTP {resp.status_code if resp else '?'}.",
            retryable=True,
            retry_after_s=5.0,
        )

    @staticmethod
    def _map_error(resp: httpx.Response) -> ConnectorError:
        try:
            body = resp.json()
            woo_code, woo_msg = body.get("code", ""), body.get("message", "")
        except ValueError:
            woo_code, woo_msg = "", ""
        status = resp.status_code
        if status == 401:
            return ConnectorError(
                "AUTH_FAILED",
                f"WooCommerce rejected the API key ({woo_code or 'unauthorized'}).",
                hint="Check WOO_CONSUMER_KEY/SECRET. Over plain http WooCommerce ignores Basic auth; use HTTPS.",
            )
        if status == 403:
            return ConnectorError(
                "FORBIDDEN",
                f"Key lacks permission ({woo_code}).",
                hint="Create a key with Read permission for an admin/shop-manager user.",
            )
        if status == 404 or woo_code.endswith("invalid_id"):
            return ConnectorError("NOT_FOUND", "No such record in this store.")
        if status == 400:
            return ConnectorError("BAD_REQUEST", f"Store rejected the query: {woo_msg or woo_code}")
        return ConnectorError("UPSTREAM_ERROR", f"Store returned HTTP {status}.", retryable=status >= 500)

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            # Typical cause: a WAF/maintenance HTML page, or permalinks disabled so /wp-json/ 404s to the theme.
            raise ConnectorError(
                "UPSTREAM_ERROR",
                "Store returned non-JSON content.",
                hint="Check that pretty permalinks are enabled and no firewall page is intercepting /wp-json/.",
            ) from exc
