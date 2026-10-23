"""In-process throughput and latency checks. Run with: pytest -m load -s

These run on whatever machine executes the tests, so the thresholds are deliberately
loose: they catch order-of-magnitude regressions, not small ones. Real numbers come
from the k6 benchmark in bench/ (see bench/RESULTS.md).
"""

import asyncio
import statistics
import time

import pytest

from gateway.config import Settings
from gateway.limiters import Composite, FixedWindow, RateLimiter, SlidingWindow, TokenBucket
from gateway.redis_client import create_redis
from tests.conftest import REDIS_URL

pytestmark = pytest.mark.load

STRATEGIES = {
    "token_bucket": TokenBucket(capacity=10**9, refill_rate=10**9),
    "fixed_window": FixedWindow(limit=10**9, window_ms=60_000),
    "sliding_counter": SlidingWindow(limit=10**9, window_ms=60_000),
    "composite": Composite(
        parts=[
            TokenBucket(capacity=10**9, refill_rate=10**9),
            FixedWindow(limit=10**9, window_ms=60_000),
        ]
    ),
}


def _report(name: str, latencies: list[float], elapsed: float) -> dict:
    latencies.sort()
    stats = {
        "rps": len(latencies) / elapsed,
        "p50_ms": statistics.median(latencies) * 1000,
        "p95_ms": latencies[int(len(latencies) * 0.95)] * 1000,
        "p99_ms": latencies[int(len(latencies) * 0.99)] * 1000,
    }
    print(
        f"\n{name}: {stats['rps']:.0f} req/s  p50 {stats['p50_ms']:.2f} ms  "
        f"p95 {stats['p95_ms']:.2f} ms  p99 {stats['p99_ms']:.2f} ms"
    )
    return stats


async def _drive(call, total: int, concurrency: int) -> tuple[list[float], float]:
    latencies: list[float] = []

    async def worker(n: int) -> None:
        for i in range(n):
            start = time.perf_counter()
            await call(i)
            latencies.append(time.perf_counter() - start)

    start = time.perf_counter()
    await asyncio.gather(*(worker(total // concurrency) for _ in range(concurrency)))
    return latencies, time.perf_counter() - start


@pytest.mark.parametrize("name", list(STRATEGIES))
async def test_limiter_throughput(redis, name, record_property):
    pool = create_redis(Settings(_env_file=None, redis_url=REDIS_URL, redis_timeout_ms=5000))
    limiter = RateLimiter(pool)
    strategy = STRATEGIES[name]
    try:
        latencies, elapsed = await _drive(
            lambda i: limiter.check(f"user-{i % 5000}", strategy), total=10_000, concurrency=32
        )
    finally:
        await pool.aclose()
    stats = _report(name, latencies, elapsed)
    for key, value in stats.items():
        record_property(key, round(value, 3))
    assert stats["rps"] > 500
    assert stats["p95_ms"] < 100


async def test_sequential_limiter_latency(redis):
    """One caller at a time: the pure round-trip cost of a decision."""
    limiter = RateLimiter(redis)
    strategy = STRATEGIES["token_bucket"]
    latencies, elapsed = await _drive(
        lambda i: limiter.check("solo", strategy), total=2000, concurrency=1
    )
    stats = _report("sequential", latencies, elapsed)
    assert stats["p95_ms"] < 20


async def test_gateway_in_process_throughput(gateway):
    _, client_for = await gateway()
    client = client_for()
    latencies, elapsed = await _drive(
        lambda i: client.get("/bulk", headers={"x-api-key": f"k{i % 10_000}"}),
        total=3000,
        concurrency=32,
    )
    stats = _report("gateway (ASGI, in-process)", latencies, elapsed)
    assert stats["rps"] > 100
