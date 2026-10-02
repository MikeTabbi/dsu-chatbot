"""Per-client rate limits and the daily Claude budget. /chat and /feedback depend on the RateLimiter
interface only.

The counters live in memory (InMemoryRateLimiter), so each server process counts on its own and a
restart resets them. That's enough for one instance; running several needs a shared store (Redis,
for example) behind the same interface.
"""

import ipaddress
import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from starlette.requests import Request

from api.app.config import Settings, settings

MINUTE = 60
DAY = 24 * 60 * 60


@dataclass(frozen=True)
class Limit:
    name: str  # for the log, e.g. chat_per_minute
    count: int  # requests allowed per window; 0 or less means no limit
    seconds: int  # window length: MINUTE or DAY


@dataclass(frozen=True)
class Blocked:
    limit: Limit  # the limit that was reached (the one with the longest wait, if several)
    retry_after: int  # whole seconds until a request would be allowed again, at least 1


class RateLimiter(Protocol):
    def hit(self, key: str, limits: Sequence[Limit]) -> Blocked | None:
        """Count one request for key against every limit and return None, or, if any limit is
        already reached, count nothing and return what blocked it."""
        ...


class InMemoryRateLimiter:
    """Fixed windows aligned to the clock: per-minute counts reset on the minute, per-day counts at
    midnight UTC. A client can get up to twice a limit across one reset, which is fine here; in
    return it's a dict of counters with no dependency.

    clock is time.time by default; tests pass a fake one so nothing waits.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._counts: dict[tuple[str, int], tuple[int, int]] = {}  # (key, seconds): (window, n)
        self._lock = threading.Lock()  # /chat and /feedback run on a thread pool
        self._next_sweep = 0.0

    def hit(self, key: str, limits: Sequence[Limit]) -> Blocked | None:
        limits = [limit for limit in limits if limit.count > 0]
        now = self._clock()
        with self._lock:
            self._sweep(now)
            blocked = []
            for limit in limits:
                window = int(now // limit.seconds)
                start, n = self._counts.get((key, limit.seconds), (window, 0))
                if start == window and n >= limit.count:
                    wait = (window + 1) * limit.seconds - now
                    blocked.append(Blocked(limit, max(1, math.ceil(wait))))
            if blocked:
                return max(blocked, key=lambda b: b.retry_after)
            for limit in limits:
                window = int(now // limit.seconds)
                start, n = self._counts.get((key, limit.seconds), (window, 0))
                self._counts[key, limit.seconds] = (window, n + 1 if start == window else 1)
            return None

    def _sweep(self, now: float) -> None:
        """Drop counters from past windows about once a minute, so memory doesn't grow with every
        address ever seen."""
        if now < self._next_sweep:
            return
        self._next_sweep = now + MINUTE
        for (key, seconds), (window, _) in list(self._counts.items()):
            if window != int(now // seconds):
                del self._counts[key, seconds]


class NoRateLimiter:
    """Never limits. For RATE_LIMITER=none and eval runs, which send every case from one place."""

    def hit(self, key: str, limits: Sequence[Limit]) -> Blocked | None:
        return None


def get_rate_limiter(config: Settings = settings) -> RateLimiter:
    """The limiter the RATE_LIMITER setting selects."""
    if config.rate_limiter == "memory":
        return InMemoryRateLimiter()
    if config.rate_limiter == "none":
        return NoRateLimiter()
    raise ValueError(f"Unknown RATE_LIMITER {config.rate_limiter!r}: expected 'memory' or 'none'")


def client_ip(request: Request, trust_proxy: bool) -> str:
    """Who is asking, for per-client limits.

    X-Forwarded-For is only read when TRUST_PROXY is on, because anyone can send that header.
    Behind one proxy (Azure App Service), the last entry is the one the proxy added; earlier
    entries came from the client and could be made up. IPv6 addresses count per /64, the block one
    home or phone usually gets, so a client can't dodge the limit by changing the low bits.
    """
    host = request.client.host if request.client else ""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        host = _without_port(forwarded) or host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host or "unknown"  # e.g. "testclient" under FastAPI's TestClient
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def _without_port(value: str) -> str:
    """Azure's X-Forwarded-For can include a port: 203.0.113.7:51234 or [2001:db8::1]:51234."""
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")]
    if value.count(":") == 1:
        return value.split(":")[0]
    return value
