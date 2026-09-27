"""Connector'lar için dayanıklı HTTP istemcisi: rate limit + retry/backoff."""
from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable

import httpx

from .base import AuthError, ConnectorError, RetryableError

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RateLimiter:
    """Token bucket. `rate_per_minute` isteğe izin verir, kısa patlamalara
    `burst` kadar tolerans tanır. Thread-safe."""

    def __init__(self, rate_per_minute: int, burst: int | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.rate = max(rate_per_minute, 1) / 60.0
        self.capacity = float(burst or max(1, min(rate_per_minute, 10)))
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self.updated = clock()
        self.lock = threading.Lock()

    def acquire(self) -> float:
        """Bir token alır; beklenen süreyi (sn) döner."""
        with self.lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            wait = (1 - self.tokens) / self.rate
            self.tokens = 0.0
            self.updated = now + wait
        self.sleep(wait)
        return wait


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0,
                  rand: Callable[[], float] = random.random) -> float:
    """Full-jitter exponential backoff. attempt 1'den başlar."""
    return min(cap, base * (2 ** (attempt - 1))) * (0.5 + rand() / 2)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class ResilientClient:
    def __init__(self, base_url: str, *, headers: dict | None = None, auth=None,
                 rate_limiter: RateLimiter | None = None, max_attempts: int = 4,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.client = httpx.Client(base_url=base_url, headers=headers or {}, auth=auth,
                                   timeout=timeout, transport=transport)
        self.rate_limiter = rate_limiter
        self.max_attempts = max_attempts
        self.sleep = sleep

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            if self.rate_limiter:
                self.rate_limiter.acquire()
            try:
                resp = self.client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = RetryableError(f"Ağ hatası: {exc.__class__.__name__}")
                if attempt < self.max_attempts:
                    self.sleep(backoff_delay(attempt))
                continue

            if resp.status_code in (401, 403):
                raise AuthError(f"Yetkisiz erişim (HTTP {resp.status_code}). API bilgilerini kontrol edin.")
            if resp.status_code in RETRY_STATUS:
                retry_after = _retry_after(resp)
                last_error = RetryableError(f"HTTP {resp.status_code}", retry_after)
                if attempt < self.max_attempts:
                    self.sleep(retry_after if retry_after is not None else backoff_delay(attempt))
                continue
            if resp.status_code >= 400:
                err = ConnectorError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                err.retryable = False
                raise err
            return resp
        assert last_error is not None
        raise last_error

    def get_json(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs).json()

    def close(self) -> None:
        self.client.close()
