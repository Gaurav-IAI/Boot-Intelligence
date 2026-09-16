"""Shared HTTP client.

Deliberately conservative: low concurrency, an inter-request delay, exponential
backoff with jitter, and no retry on 4xx other than 429. Every request is logged
with a request id so a stored record can be traced back to the call that fetched it.

`election.uk.gov.in` requires legacy TLS renegotiation, which OpenSSL 3 refuses by
default (`UNSAFE_LEGACY_RENEGOTIATION_DISABLED`). We enable OP_LEGACY_SERVER_CONNECT
for that host only.
"""
from __future__ import annotations

import logging
import random
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from .config import settings

log = logging.getLogger(__name__)

# OpenSSL's OP_LEGACY_SERVER_CONNECT. Not exposed as an ssl.OP_* constant in
# CPython, so it is spelled out here.
OP_LEGACY_SERVER_CONNECT = 0x4

LEGACY_TLS_HOSTS = {"election.uk.gov.in", "ceo.uk.gov.in"}

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _legacy_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.options |= OP_LEGACY_SERVER_CONNECT
    # These government hosts present chains that Python's default store often
    # cannot complete. We are reading published public documents, so we accept
    # the chain but record the fact in the source registry.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@dataclass
class FetchStats:
    """Counters used by the data-quality dashboard."""

    attempted: int = 0
    ok: int = 0
    failed: int = 0
    retries: int = 0
    by_status: dict[int, int] = field(default_factory=dict)

    def record(self, status: int | None, ok: bool) -> None:
        self.attempted += 1
        if ok:
            self.ok += 1
        else:
            self.failed += 1
        if status is not None:
            self.by_status[status] = self.by_status.get(status, 0) + 1

    @property
    def success_rate(self) -> float:
        return (self.ok / self.attempted * 100.0) if self.attempted else 0.0


class HttpClient:
    """Small synchronous client with retries, backoff and politeness delay."""

    def __init__(
        self,
        *,
        timeout: float | None = None,
        retries: int | None = None,
        delay: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.timeout = timeout if timeout is not None else settings.http_timeout
        self.retries = retries if retries is not None else settings.http_retries
        self.delay = delay if delay is not None else settings.http_delay_seconds
        self.user_agent = user_agent or settings.user_agent
        self.stats = FetchStats()
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._clients: dict[bool, httpx.Client] = {}

    # -- lifecycle -------------------------------------------------------
    def _client(self, legacy_tls: bool) -> httpx.Client:
        if legacy_tls not in self._clients:
            verify: Any = _legacy_ssl_context() if legacy_tls else True
            self._clients[legacy_tls] = httpx.Client(
                timeout=self.timeout,
                verify=verify,
                follow_redirects=True,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json, text/html, application/pdf;q=0.9, */*;q=0.8",
                    "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
                },
            )
        return self._clients[legacy_tls]

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
        self._clients.clear()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- politeness ------------------------------------------------------
    def _throttle(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last_call
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self._last_call = time.monotonic()

    # -- core ------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        expect: str | None = None,
    ) -> httpx.Response:
        """Issue a request, retrying transient failures.

        `expect` may be "json" or "pdf"; the response content-type is validated
        against it so a captcha/error HTML page is never silently stored as a PDF.
        """
        host = httpx.URL(url).host or ""
        legacy = host in LEGACY_TLS_HOSTS
        rid = uuid.uuid4().hex[:12]
        last_exc: Exception | None = None

        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self._client(legacy).request(
                    method, url, params=params, json=json_body,
                    headers={**(headers or {}), "X-Request-Id": rid},
                )
            except Exception as exc:  # network / TLS / timeout
                last_exc = exc
                self.stats.retries += 1
                log.warning("rid=%s attempt=%d %s %s -> %s: %s",
                            rid, attempt, method, url, type(exc).__name__, exc)
                if attempt == self.retries:
                    break
                self._sleep_backoff(attempt)
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt < self.retries:
                self.stats.retries += 1
                log.warning("rid=%s attempt=%d %s %s -> HTTP %d (retrying)",
                            rid, attempt, method, url, resp.status_code)
                self._sleep_backoff(attempt, resp)
                continue

            ok = resp.status_code < 400
            if ok and expect:
                ctype = (resp.headers.get("content-type") or "").lower()
                if expect == "json" and "json" not in ctype:
                    ok = False
                elif expect == "pdf" and not resp.content.startswith(b"%PDF"):
                    ok = False
                if not ok:
                    log.error("rid=%s %s returned HTTP %d but content-type=%r "
                              "did not match expected %s",
                              rid, url, resp.status_code, ctype, expect)
            self.stats.record(resp.status_code, ok)
            log.info("rid=%s %s %s -> %d (%d bytes)",
                     rid, method, url, resp.status_code, len(resp.content))
            if not ok:
                resp.raise_for_status()
                raise ValueError(
                    f"{url} returned HTTP {resp.status_code} with unexpected "
                    f"content-type for expect={expect!r}"
                )
            return resp

        self.stats.record(None, False)
        raise RuntimeError(f"request failed after {self.retries} attempts: {method} {url}") from last_exc

    def _sleep_backoff(self, attempt: int, resp: httpx.Response | None = None) -> None:
        if resp is not None and resp.status_code == 429:
            ra = resp.headers.get("retry-after")
            if ra and ra.isdigit():
                time.sleep(min(float(ra), 60.0))
                return
        wait = settings.http_backoff_base * (2 ** (attempt - 1))
        time.sleep(min(wait, 30.0) * (0.7 + 0.6 * random.random()))

    # -- helpers ---------------------------------------------------------
    def get_json(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, expect="json", **kw).json()

    def get_text(self, url: str, **kw: Any) -> str:
        return self.request("GET", url, **kw).text

    def get_pdf(self, url: str, **kw: Any) -> bytes:
        return self.request("GET", url, expect="pdf", **kw).content
