"""What the gateway does when Redis is down, slow or holding bad state."""

import asyncio

import pytest
from redis.asyncio import Redis

from gateway.config import Settings
from gateway.redis_client import CircuitBreaker, create_redis
from tests.conftest import REDIS_URL


@pytest.fixture
async def dead_redis():
    # Nothing listens on port 1; the tiny timeout keeps Windows from retrying for seconds.
    client = Redis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.05, socket_timeout=0.05
    )
    yield client
    await client.aclose()


async def test_fail_open_lets_traffic_through_and_flags_it(gateway, upstream, dead_redis):
    app, client_for = await gateway(redis_client=dead_redis, fail_mode="open")
    response = await client_for().get("/api/x")
    assert response.status_code == 200
    assert response.headers["x-ratelimit-status"] == "degraded"
    assert "x-ratelimit-limit" not in response.headers
    assert len(upstream.requests) == 1
    metrics = app.state.gateway.metrics.render().decode()
    assert "gateway_redis_errors_total 1.0" in metrics
    assert 'outcome="degraded"' in metrics


async def test_fail_closed_sheds_traffic(gateway, upstream, dead_redis):
    _, client_for = await gateway(redis_client=dead_redis, fail_mode="closed")
    response = await client_for().get("/api/x")
    assert response.status_code == 503
    assert upstream.requests == []


async def test_unlimited_routes_unaffected_by_redis_outage(gateway, dead_redis):
    config = {
        "policies": [
            {
                "name": "api",
                "match": {"path_prefix": "/api"},
                "strategy": {"type": "fixed_window", "limit": 1, "window_ms": 1000},
            }
        ]
    }
    _, client_for = await gateway(config, redis_client=dead_redis, fail_mode="closed")
    assert (await client_for().get("/public")).status_code == 200


async def test_circuit_breaker_stops_calling_dead_redis(gateway, dead_redis):
    breaker = CircuitBreaker(min_failures=2, cooldown_s=60)
    app, client_for = await gateway(redis_client=dead_redis, breaker=breaker)
    client = client_for()
    for _ in range(10):
        assert (await client.get("/api/x")).status_code == 200
    metrics = app.state.gateway.metrics.render().decode()
    assert "gateway_redis_errors_total 2.0" in metrics
    assert "gateway_redis_circuit_open 1.0" in metrics


async def test_circuit_open_with_fail_closed_returns_503_fast(gateway, dead_redis):
    breaker = CircuitBreaker(min_failures=1, cooldown_s=60)
    _, client_for = await gateway(redis_client=dead_redis, breaker=breaker, fail_mode="closed")
    client = client_for()
    await client.get("/api/x")
    statuses = [(await client.get("/api/x")).status_code for _ in range(5)]
    assert statuses == [503] * 5


async def test_ready_reports_redis_outage(gateway, dead_redis):
    _, client_for = await gateway(redis_client=dead_redis)
    client = client_for()
    assert (await client.get("/_gw/ready")).status_code == 503
    assert (await client.get("/_gw/health")).status_code == 200


async def test_check_endpoint_reports_outage(gateway, dead_redis):
    _, client_for = await gateway(redis_client=dead_redis)
    response = await client_for().get("/_gw/check", params={"policy": "api", "key": "k"})
    assert response.status_code == 503


async def test_slow_redis_times_out_then_recovers(gateway, redis):
    pooled = create_redis(Settings(_env_file=None, redis_url=REDIS_URL, redis_timeout_ms=50))
    try:
        app, client_for = await gateway(
            redis_client=pooled, breaker=CircuitBreaker(min_failures=100)
        )
        client = client_for()
        await redis.execute_command("CLIENT", "PAUSE", 400, "ALL")
        slow = await client.get("/api/x")
        assert slow.headers["x-ratelimit-status"] == "degraded"
        await asyncio.sleep(0.5)
        recovered = await client.get("/api/x")
        assert recovered.status_code == 200
        assert recovered.headers["x-ratelimit-limit"] == "5"
    finally:
        await pooled.aclose()


async def test_corrupt_state_degrades_instead_of_500(gateway, redis):
    app, client_for = await gateway()
    limiter = app.state.gateway.limiter
    strategy = app.state.gateway.resolver.by_name["api"].strategy
    (key,) = limiter.keys_for("api:ip:203.0.113.7", strategy)
    await redis.set(key, "garbage")
    response = await client_for().get("/api/x")
    assert response.status_code == 200
    assert response.headers["x-ratelimit-status"] == "degraded"


async def test_script_cache_flush_is_transparent(gateway, redis):
    _, client_for = await gateway()
    client = client_for()
    assert (await client.get("/api/x")).headers["x-ratelimit-remaining"] == "4"
    await redis.script_flush()
    assert (await client.get("/api/x")).headers["x-ratelimit-remaining"] == "3"


async def test_evicted_state_starts_fresh(gateway, redis):
    _, client_for = await gateway()
    client = client_for()
    for _ in range(5):
        await client.get("/api/x")
    assert (await client.get("/api/x")).status_code == 429
    await redis.flushdb()
    assert (await client.get("/api/x")).status_code == 200
