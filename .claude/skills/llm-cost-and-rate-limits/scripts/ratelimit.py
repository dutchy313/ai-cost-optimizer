"""Drop-in client-side rate limiting for LLM calls (standard library only).

Copy this module into an application. It provides:
  - TokenBucket: continuously refilling bucket, matching Anthropic's model
  - LimitSet: one bucket per dimension (RPM, ITPM, OTPM) for a model class
  - backoff_delay: honours Retry-After, else exponential backoff with full jitter
  - CircuitBreaker: stops calling after repeated failures
  - call_with_limits: wraps an async call with all of the above

Run `python3 ratelimit.py` for a self-test.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

RETRYABLE = {429, 500, 502, 503, 504, 529}


class TokenBucket:
    """Capacity per minute, refilled continuously (60 RPM ~ 1 request per second)."""

    def __init__(self, capacity_per_min: float):
        self.capacity = float(capacity_per_min)
        self.tokens = float(capacity_per_min)
        self.rate = self.capacity / 60.0
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    async def take(self, amount: float) -> None:
        if amount > self.capacity:
            raise ValueError(f"request needs {amount} but bucket capacity is {self.capacity}; split it or raise the limit")
        async with self.lock:
            while True:
                self._refill()
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                await asyncio.sleep((amount - self.tokens) / self.rate)

    def give_back(self, amount: float) -> None:
        """Refund unused reservation (for example max_tokens minus actual output)."""
        self._refill()
        self.tokens = min(self.capacity, self.tokens + max(0.0, amount))

    def utilisation(self) -> float:
        self._refill()
        return 1 - self.tokens / self.capacity


@dataclass
class LimitSet:
    rpm: float
    itpm: float
    otpm: float
    buckets: dict = field(init=False)

    def __post_init__(self):
        self.buckets = {"rpm": TokenBucket(self.rpm), "itpm": TokenBucket(self.itpm), "otpm": TokenBucket(self.otpm)}

    async def acquire(self, uncached_input: int, max_tokens: int) -> None:
        await self.buckets["rpm"].take(1)
        await self.buckets["itpm"].take(uncached_input)  # cache reads do not count on Anthropic
        await self.buckets["otpm"].take(max_tokens)

    def settle(self, max_tokens: int, actual_output: int) -> None:
        self.buckets["otpm"].give_back(max_tokens - actual_output)

    def max_utilisation(self) -> float:
        return max(b.utilisation() for b in self.buckets.values())


def backoff_delay(attempt: int, retry_after: float | None = None, base: float = 1.0, cap: float = 32.0) -> float:
    if retry_after is not None:
        return float(retry_after)
    return random.uniform(0, min(cap, base * 2 ** attempt))


class CircuitBreaker:
    def __init__(self, failures: int = 10, window_s: float = 60, cooldown_s: float = 60):
        self.failures, self.window_s, self.cooldown_s = failures, window_s, cooldown_s
        self.events: list[float] = []
        self.open_until = 0.0

    def allow(self) -> bool:
        return time.monotonic() >= self.open_until

    def record(self, ok: bool) -> None:
        now = time.monotonic()
        if ok:
            self.events.clear()
            return
        self.events = [t for t in self.events if now - t < self.window_s] + [now]
        if len(self.events) >= self.failures:
            self.open_until = now + self.cooldown_s
            self.events.clear()


class LLMError(Exception):
    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"status {status}")
        self.status, self.retry_after = status, retry_after


async def call_with_limits(
    call: Callable[[], Awaitable[Any]],
    limits: LimitSet,
    breaker: CircuitBreaker,
    uncached_input: int,
    max_tokens: int,
    output_of: Callable[[Any], int] = lambda r: 0,
    max_attempts: int = 5,
    fallback: Callable[[], Any] | None = None,
):
    """Acquire budget, call, retry with jitter on retryable errors, refund unused output."""
    for attempt in range(max_attempts):
        if not breaker.allow():
            if fallback:
                return fallback()
            raise LLMError(503)
        await limits.acquire(uncached_input, max_tokens)
        try:
            result = await call()
        except LLMError as e:
            limits.settle(max_tokens, 0)
            breaker.record(False)
            if e.status not in RETRYABLE or attempt == max_attempts - 1:
                if fallback:
                    return fallback()
                raise
            await asyncio.sleep(backoff_delay(attempt, e.retry_after))
            continue
        breaker.record(True)
        limits.settle(max_tokens, output_of(result))
        return result


if __name__ == "__main__":
    async def _selftest():
        b = TokenBucket(120)  # 2 per second
        t0 = time.monotonic()
        for _ in range(122):
            await b.take(1)
        waited = time.monotonic() - t0
        assert 0.7 < waited < 2.5, waited
        assert backoff_delay(3, retry_after=7) == 7
        assert all(0 <= backoff_delay(10) <= 32 for _ in range(100))

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise LLMError(429, retry_after=0.01)
            return {"output_tokens": 40}

        limits = LimitSet(rpm=600, itpm=100_000, otpm=20_000)
        out = await call_with_limits(flaky, limits, CircuitBreaker(), 500, 1000, lambda r: r["output_tokens"])
        assert out["output_tokens"] == 40 and calls["n"] == 3
        print(f"self-test passed (bucket waited {waited:.2f}s for 2 extra requests; retry succeeded on attempt 3)")

    asyncio.run(_selftest())
