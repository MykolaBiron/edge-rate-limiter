"""Straightforward Python models of each algorithm, used as test oracles for the Lua.

They favour clarity over speed and share no code with the production scripts.
"""

import math
from dataclasses import dataclass, field

EPS = 1e-9


@dataclass
class Result:
    allowed: bool
    remaining: int
    retry_after_ms: int


@dataclass
class TokenBucketModel:
    capacity: int
    rate: float
    tokens: float | None = None
    ts: float = 0.0

    def check(self, now: float, cost: int) -> Result:
        if self.tokens is None:
            self.tokens, self.ts = float(self.capacity), now
        if now > self.ts:
            self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate / 1000)
            self.ts = now
        if self.tokens + EPS >= cost:
            self.tokens = max(0.0, self.tokens - cost)
            return Result(True, math.floor(self.tokens + EPS), 0)
        retry = max(1, math.ceil((cost - self.tokens) * 1000 / self.rate))
        return Result(False, math.floor(self.tokens + EPS), retry)


@dataclass
class LeakyBucketModel:
    capacity: int
    rate: float
    free_at: float | None = None

    def check(self, now: float, cost: int) -> Result:
        interval = 1000 / self.rate
        horizon = self.capacity * interval
        free_at = now if self.free_at is None or self.free_at < now else self.free_at
        backlog = free_at - now
        if backlog + cost * interval <= horizon + EPS:
            self.free_at = free_at + cost * interval
            return Result(
                True, math.floor((horizon - backlog - cost * interval) / interval + EPS), 0
            )
        remaining = max(0, math.floor((horizon - backlog) / interval + EPS))
        return Result(False, remaining, max(1, math.ceil(backlog + cost * interval - horizon)))


@dataclass
class FixedWindowModel:
    limit: int
    window: int
    counts: dict[int, int] = field(default_factory=dict)

    def check(self, now: float, cost: int) -> Result:
        index = math.floor(now / self.window)
        count = self.counts.get(index, 0)
        if count + cost <= self.limit:
            self.counts[index] = count + cost
            return Result(True, self.limit - count - cost, 0)
        end = (index + 1) * self.window
        return Result(False, self.limit - count, max(1, math.ceil(end - now)))


@dataclass
class SlidingLogModel:
    limit: int
    window: int
    log: list[float] = field(default_factory=list)

    def check(self, now: float, cost: int) -> Result:
        self.log = [t for t in self.log if t > now - self.window]
        if len(self.log) + cost <= self.limit:
            self.log.extend([now] * cost)
            return Result(True, self.limit - len(self.log), 0)
        must_expire = len(self.log) + cost - self.limit
        oldest_needed = sorted(self.log)[must_expire - 1]
        retry = max(1, math.ceil(oldest_needed + self.window - now))
        return Result(False, self.limit - len(self.log), retry)


@dataclass
class SlidingCounterModel:
    limit: int
    window: int
    counts: dict[int, int] = field(default_factory=dict)

    def estimate_at(self, t: float) -> float:
        index = math.floor(t / self.window)
        elapsed = t - index * self.window
        prev = self.counts.get(index - 1, 0)
        return prev * (self.window - elapsed) / self.window + self.counts.get(index, 0)

    def check(self, now: float, cost: int) -> Result:
        estimate = self.estimate_at(now)
        if estimate + cost <= self.limit + EPS:
            index = math.floor(now / self.window)
            self.counts[index] = self.counts.get(index, 0) + cost
            return Result(True, max(0, math.floor(self.limit - estimate - cost + EPS)), 0)
        # Brute-force the earliest admitting millisecond instead of solving for it.
        wait = 1
        while self.estimate_at(now + wait) + cost > self.limit + EPS:
            wait += 1
        return Result(False, max(0, math.floor(self.limit - estimate + EPS)), wait)
