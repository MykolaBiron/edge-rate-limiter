import pytest

from gateway.limiters import FixedWindow, SlidingWindow

BOTH = pytest.mark.parametrize("precise", [False, True], ids=["counter", "log"])


@BOTH
@pytest.mark.parametrize("limit", [1, 4, 25])
async def test_allows_limit_then_denies(limiter, precise, limit):
    window = SlidingWindow(limit=limit, window_ms=1000, precise=precise)
    results = [(await limiter.check("c", window)).allowed for _ in range(limit + 1)]
    assert results == [True] * limit + [False]


@BOTH
async def test_blocks_the_fixed_window_boundary_burst(limiter, clock, precise):
    window = SlidingWindow(limit=5, window_ms=1000, precise=precise)
    clock.advance(999)
    first = [(await limiter.check("c", window)).allowed for _ in range(5)]
    clock.advance(1)
    second = [(await limiter.check("c", window)).allowed for _ in range(5)]
    assert all(first)
    assert not any(second)


async def test_fixed_window_contrast(limiter, clock):
    fixed = FixedWindow(limit=5, window_ms=1000)
    clock.advance(999)
    for _ in range(5):
        await limiter.check("f", fixed)
    clock.advance(1)
    assert (await limiter.check("f", fixed)).allowed


@pytest.mark.parametrize(("into_next", "expected_allowed"), [(0, 0), (250, 2), (500, 5), (900, 9)])
async def test_counter_weights_previous_window(limiter, clock, into_next, expected_allowed):
    window = SlidingWindow(limit=10, window_ms=1000)
    for _ in range(10):
        await limiter.check("c", window)
    clock.advance(1000 + into_next)
    allowed = 0
    for _ in range(12):
        allowed += (await limiter.check("c", window)).allowed
    assert allowed == expected_allowed


async def test_counter_forgets_windows_older_than_previous(limiter, clock):
    window = SlidingWindow(limit=3, window_ms=1000)
    for _ in range(3):
        await limiter.check("c", window)
    clock.advance(2000)
    results = [(await limiter.check("c", window)).allowed for _ in range(4)]
    assert results == [True] * 3 + [False]


@pytest.mark.parametrize("gap", [0, 1, 250, 500, 999])
async def test_log_entries_expire_exactly_after_window(limiter, clock, gap):
    window = SlidingWindow(limit=2, window_ms=1000, precise=True)
    await limiter.check("c", window)
    clock.advance(gap)
    await limiter.check("c", window)
    clock.advance(999 - gap)
    decision = await limiter.check("c", window)
    assert not decision.allowed
    assert decision.retry_after_ms == 1
    clock.advance(1)
    assert (await limiter.check("c", window)).allowed


async def test_log_retry_waits_for_enough_entries_to_expire(limiter, clock):
    window = SlidingWindow(limit=4, window_ms=1000, precise=True)
    for _ in range(4):
        await limiter.check("c", window)
        clock.advance(100)
    decision = await limiter.check("c", window, cost=3)
    assert not decision.allowed
    assert decision.retry_after_ms == 1000 + 200 - 400


@BOTH
async def test_cost(limiter, precise):
    window = SlidingWindow(limit=10, window_ms=1000, precise=precise)
    assert (await limiter.check("c", window, cost=6)).allowed
    assert not (await limiter.check("c", window, cost=6)).allowed
    assert (await limiter.check("c", window, cost=4)).allowed


async def test_log_stores_one_entry_per_admitted_unit(limiter, redis):
    window = SlidingWindow(limit=10, window_ms=1000, precise=True)
    await limiter.check("c", window, cost=3)
    await limiter.check("c", window, cost=3)
    await limiter.check("c", window, cost=5)
    (key,) = limiter.keys_for("c", window)
    assert await redis.zcard(key) == 6


async def test_counter_uses_constant_memory(limiter, redis):
    window = SlidingWindow(limit=1000, window_ms=1000)
    for _ in range(500):
        await limiter.check("c", window)
    (key,) = limiter.keys_for("c", window)
    assert await redis.hlen(key) == 3


@BOTH
async def test_remaining(limiter, precise):
    window = SlidingWindow(limit=3, window_ms=1000, precise=precise)
    remaining = [(await limiter.check("c", window)).remaining for _ in range(4)]
    assert remaining == [2, 1, 0, 0]
