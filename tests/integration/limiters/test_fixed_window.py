import pytest

from gateway.limiters import FixedWindow


@pytest.mark.parametrize("limit", [1, 3, 10, 100])
async def test_allows_limit_per_window(limiter, limit):
    window = FixedWindow(limit=limit, window_ms=1000)
    results = [(await limiter.check("c", window)).allowed for _ in range(limit + 1)]
    assert results == [True] * limit + [False]


@pytest.mark.parametrize(("offset", "expected_retry"), [(0, 1000), (1, 999), (250, 750), (999, 1)])
async def test_retry_after_is_time_to_window_end(limiter, clock, offset, expected_retry):
    window = FixedWindow(limit=1, window_ms=1000)
    clock.advance(offset)
    await limiter.check("c", window)
    decision = await limiter.check("c", window)
    assert not decision.allowed
    assert decision.retry_after_ms == expected_retry
    assert decision.reset_ms == expected_retry


async def test_counter_resets_at_boundary(limiter, clock):
    window = FixedWindow(limit=2, window_ms=1000)
    await limiter.check("c", window)
    await limiter.check("c", window)
    clock.advance(999)
    assert not (await limiter.check("c", window)).allowed
    clock.advance(1)
    assert (await limiter.check("c", window)).allowed


async def test_allows_double_burst_across_boundary(limiter, clock):
    """The known weakness of fixed windows, kept here as documented behaviour."""
    window = FixedWindow(limit=5, window_ms=1000)
    clock.advance(999)
    first = [(await limiter.check("c", window)).allowed for _ in range(5)]
    clock.advance(1)
    second = [(await limiter.check("c", window)).allowed for _ in range(5)]
    assert all(first) and all(second)


async def test_stale_window_state_is_ignored(limiter, clock):
    window = FixedWindow(limit=1, window_ms=1000)
    await limiter.check("c", window)
    clock.advance(5000)
    assert (await limiter.check("c", window)).allowed


async def test_cost_counts_against_limit(limiter):
    window = FixedWindow(limit=10, window_ms=1000)
    assert (await limiter.check("c", window, cost=7)).remaining == 3
    decision = await limiter.check("c", window, cost=4)
    assert not decision.allowed
    assert decision.remaining == 3
    assert (await limiter.check("c", window, cost=3)).allowed


async def test_rejections_do_not_increment(limiter, redis):
    window = FixedWindow(limit=1, window_ms=60_000)
    for _ in range(5):
        await limiter.check("c", window)
    (key,) = limiter.keys_for("c", window)
    assert int(await redis.hget(key, "count")) == 1


async def test_ttl_covers_rest_of_window(limiter, clock, redis):
    window = FixedWindow(limit=5, window_ms=10_000)
    clock.advance(4000)
    await limiter.check("c", window)
    (key,) = limiter.keys_for("c", window)
    assert 6000 < await redis.pttl(key) <= 7000
