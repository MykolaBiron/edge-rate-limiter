"""Race-condition tests: many concurrent callers, several "replicas", one Redis."""

import asyncio
import math
import random
import time

import pytest

from gateway.config import Settings
from gateway.limiters import (
    Composite,
    FixedWindow,
    LeakyBucket,
    RateLimiter,
    SlidingWindow,
    TokenBucket,
)
from gateway.redis_client import create_redis
from tests.conftest import REDIS_URL
from tests.integration.conftest import CLIENT_PEER

REPLICAS = 5

# Long windows and near-zero refill so the real clock can't admit extra requests mid-test.
STRATEGIES = {
    "token_bucket": TokenBucket(capacity=100, refill_rate=0.0001),
    "leaky_bucket": LeakyBucket(capacity=100, leak_rate=0.0001),
    "fixed_window": FixedWindow(limit=100, window_ms=3_600_000),
    "sliding_counter": SlidingWindow(limit=100, window_ms=3_600_000),
    "sliding_log": SlidingWindow(limit=100, window_ms=3_600_000, precise=True),
    "composite": Composite(
        parts=[
            TokenBucket(capacity=150, refill_rate=0.0001),
            FixedWindow(limit=100, window_ms=3_600_000),
        ]
    ),
}


@pytest.fixture
async def replicas(redis):
    """Independent limiters, each with its own connection pool, like separate gateways.

    Small blocking pools keep connection churn low; Docker Desktop's port forwarding
    on Windows starts refusing connections after a few thousand in quick succession.
    """
    settings = Settings(
        _env_file=None, redis_url=REDIS_URL, redis_max_connections=20, redis_timeout_ms=5000
    )
    clients = [create_redis(settings) for _ in range(REPLICAS)]
    yield [RateLimiter(c) for c in clients]
    for c in clients:
        await c.aclose()


async def _hammer(replicas, key, strategy, total, cost=1):
    calls = [replicas[i % len(replicas)].check(key, strategy, cost) for i in range(total)]
    random.Random(0).shuffle(calls)
    return await asyncio.gather(*calls)


@pytest.mark.parametrize("strategy", list(STRATEGIES.values()), ids=list(STRATEGIES))
@pytest.mark.parametrize("total", [101, 250, 600])
async def test_never_admits_more_than_the_limit(replicas, strategy, total):
    decisions = await _hammer(replicas, "hot-key", strategy, total)
    assert sum(d.allowed for d in decisions) == 100


@pytest.mark.parametrize("strategy", list(STRATEGIES.values()), ids=list(STRATEGIES))
async def test_weighted_cost_under_contention(replicas, strategy):
    decisions = await _hammer(replicas, "hot-key", strategy, 200, cost=3)
    assert sum(d.allowed for d in decisions) == 33


@pytest.mark.parametrize("strategy", list(STRATEGIES.values()), ids=list(STRATEGIES))
async def test_remaining_values_are_unique_among_admitted(replicas, strategy):
    # Each admitted request saw a distinct state, which only happens if no two
    # scripts interleaved.
    decisions = await _hammer(replicas, "hot-key", strategy, 300)
    remaining = sorted(d.remaining for d in decisions if d.allowed)
    assert remaining == list(range(100))


async def test_leaky_bucket_assigns_each_slot_once(replicas):
    bucket = LeakyBucket(capacity=50, leak_rate=10)
    start = time.monotonic()
    decisions = await _hammer(replicas, "shape", bucket, 200)
    elapsed = time.monotonic() - start
    delays = [d.delay_ms for d in decisions if d.allowed]
    # The queue keeps draining (real clock) while the burst is in flight.
    assert 50 <= len(delays) <= 50 + math.ceil(elapsed * 10)
    # Slots are 100 ms apart; the real clock moves during the test, so allow drift.
    spaced = sorted(delays)
    assert all(b - a >= 90 for a, b in zip(spaced, spaced[1:], strict=False))


async def test_many_keys_in_parallel_stay_independent(replicas):
    bucket = TokenBucket(capacity=10, refill_rate=0.0001)
    keys = [f"user-{i}" for i in range(30)]
    calls = [replicas[i % REPLICAS].check(k, bucket) for i, k in enumerate(keys * 15)]
    decisions = await asyncio.gather(*calls)
    per_key: dict[str, int] = {}
    for key, decision in zip(keys * 15, decisions, strict=True):
        per_key[key] = per_key.get(key, 0) + decision.allowed
    assert set(per_key.values()) == {10}


async def test_gateway_replicas_share_limits(gateway, upstream):
    """Three gateway apps, each with its own production-style pool, act as one limiter."""
    settings = Settings(_env_file=None, redis_url=REDIS_URL, redis_timeout_ms=2000)
    pools = [create_redis(settings) for _ in range(3)]
    try:
        apps = [await gateway(redis_client=pool) for pool in pools]
        clients = [client_for(CLIENT_PEER) for _, client_for in apps]
        responses = await asyncio.gather(*(clients[i % 3].get("/bulk") for i in range(300)))
    finally:
        for pool in pools:
            await pool.aclose()

    # A degraded (fail-open) response would mean Redis errored, not that limits held.
    assert not any("x-ratelimit-status" in r.headers for r in responses)
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 100
    assert statuses.count(429) == 200
    assert len(upstream.requests) == 100
