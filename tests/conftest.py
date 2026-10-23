import os

import pytest
from redis.asyncio import Redis

from gateway.limiters import RateLimiter

# 127.0.0.1 rather than localhost: avoids a doomed IPv6 attempt on every connect.
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")

# Aligned to whole seconds, minutes and hours so window boundaries are easy to reason about.
T0 = 1_700_002_800_000.0


class FakeClock:
    def __init__(self, start: float = T0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, ms: float) -> None:
        self.now += ms


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


_redis_seen = False


@pytest.fixture
async def redis():
    global _redis_seen
    client = Redis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        # Skip only when Redis was never there; losing it mid-run is a real failure.
        if _redis_seen:
            raise
        pytest.skip(f"Redis not reachable at {REDIS_URL}: {exc}")
    _redis_seen = True
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def limiter(redis, clock) -> RateLimiter:
    return RateLimiter(redis, clock=clock)
