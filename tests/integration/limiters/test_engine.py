import asyncio

import pytest
from redis.exceptions import ResponseError

from gateway.config import Settings
from gateway.limiters import (
    Composite,
    FixedWindow,
    LeakyBucket,
    RateLimiter,
    SlidingWindow,
    TokenBucket,
)
from gateway.limiters.engine import SCRIPT
from gateway.redis_client import create_redis
from tests.conftest import REDIS_URL

ALL_STRATEGIES = {
    "token_bucket": TokenBucket(capacity=3, refill_rate=2),
    "leaky_bucket": LeakyBucket(capacity=3, leak_rate=2),
    "fixed_window": FixedWindow(limit=3, window_ms=1000),
    "sliding_counter": SlidingWindow(limit=3, window_ms=1000),
    "sliding_log": SlidingWindow(limit=3, window_ms=1000, precise=True),
    "composite": Composite(
        parts=[TokenBucket(capacity=3, refill_rate=2), FixedWindow(limit=5, window_ms=2000)]
    ),
}
STRATEGIES = pytest.mark.parametrize(
    "strategy", list(ALL_STRATEGIES.values()), ids=list(ALL_STRATEGIES)
)


@STRATEGIES
async def test_waiting_retry_after_is_enough_and_necessary(limiter, clock, strategy):
    while (await limiter.check("c", strategy)).allowed:
        pass
    decision = await limiter.check("c", strategy)
    clock.advance(decision.retry_after_ms - 1)
    assert not (await limiter.check("c", strategy)).allowed
    clock.advance(1)
    assert (await limiter.check("c", strategy)).allowed


@pytest.mark.parametrize(
    "strategy",
    [
        TokenBucket(capacity=3, refill_rate=0.01),
        LeakyBucket(capacity=3, leak_rate=0.01),
        FixedWindow(limit=3, window_ms=3_600_000),
        SlidingWindow(limit=3, window_ms=3_600_000),
        SlidingWindow(limit=3, window_ms=3_600_000, precise=True),
    ],
    ids=["token_bucket", "leaky_bucket", "fixed_window", "sliding_counter", "sliding_log"],
)
async def test_uses_redis_server_clock_without_fake_clock(redis, strategy):
    # Long windows so a real window boundary can't land mid-test.
    limiter = RateLimiter(redis)
    results = [(await limiter.check("server-clock", strategy)).allowed for _ in range(4)]
    assert results == [True] * 3 + [False]


@STRATEGIES
async def test_recovers_after_script_flush(limiter, redis, strategy):
    assert (await limiter.check("c", strategy)).allowed
    await redis.script_flush()
    assert (await limiter.check("c", strategy)).allowed


@STRATEGIES
async def test_recovers_when_state_is_evicted(limiter, redis, strategy):
    for _ in range(3):
        await limiter.check("c", strategy)
    await redis.delete(*limiter.keys_for("c", strategy))
    assert (await limiter.check("c", strategy)).allowed


@STRATEGIES
@pytest.mark.parametrize("cost", [0, -1, 6])
async def test_rejects_cost_outside_limits(limiter, strategy, cost):
    with pytest.raises(ValueError):
        await limiter.check("c", strategy, cost=cost)


@STRATEGIES
async def test_every_key_has_a_ttl(limiter, redis, strategy):
    await limiter.check("c", strategy)
    for key in limiter.keys_for("c", strategy):
        assert await redis.pttl(key) > 0


async def test_prefix_namespaces_keys(redis, clock):
    a = RateLimiter(redis, prefix="tenant-a", clock=clock)
    b = RateLimiter(redis, prefix="tenant-b", clock=clock)
    bucket = TokenBucket(capacity=1, refill_rate=1)
    assert (await a.check("c", bucket)).allowed
    assert (await b.check("c", bucket)).allowed
    assert a.keys_for("c", bucket)[0].startswith("tenant-a:")


async def test_unknown_algorithm_is_a_redis_error(redis):
    script = redis.register_script(SCRIPT)
    with pytest.raises(ResponseError, match="unknown algorithm"):
        await script(keys=["k"], args=["", 1, 1, "nope", 1, 1])


async def test_corrupt_state_surfaces_as_redis_error(limiter, redis):
    bucket = TokenBucket(capacity=1, refill_rate=1)
    (key,) = limiter.keys_for("c", bucket)
    await redis.set(key, "not a hash")
    with pytest.raises(ResponseError):
        await limiter.check("c", bucket)


async def test_concurrent_checks_share_a_small_blocking_pool(redis):
    # 200 in-flight checks over 10 connections: callers queue for a connection
    # instead of failing, as in production (see create_redis).
    settings = Settings(redis_url=REDIS_URL, redis_max_connections=10, redis_timeout_ms=2000)
    pooled = create_redis(settings)
    try:
        limiter = RateLimiter(pooled)
        bucket = TokenBucket(capacity=50, refill_rate=0.001)
        results = await asyncio.gather(*(limiter.check("c", bucket) for _ in range(200)))
        assert sum(d.allowed for d in results) == 50
    finally:
        await pooled.aclose()
