import pytest

from gateway.limiters import LeakyBucket


@pytest.mark.parametrize("capacity", [1, 3, 10, 100])
async def test_queue_holds_capacity_requests(limiter, capacity):
    bucket = LeakyBucket(capacity=capacity, leak_rate=10)
    results = [(await limiter.check("c", bucket)).allowed for _ in range(capacity + 1)]
    assert results == [True] * capacity + [False]


@pytest.mark.parametrize("rate", [1, 2, 5, 10, 50, 100])
async def test_delays_space_requests_at_leak_rate(limiter, rate):
    bucket = LeakyBucket(capacity=5, leak_rate=rate)
    delays = [(await limiter.check("c", bucket)).delay_ms for _ in range(5)]
    interval = 1000 / rate
    assert delays == [int(i * interval) for i in range(5)]


async def test_retry_after_is_time_until_a_slot_frees(limiter):
    bucket = LeakyBucket(capacity=2, leak_rate=4)
    await limiter.check("c", bucket)
    await limiter.check("c", bucket)
    decision = await limiter.check("c", bucket)
    assert not decision.allowed
    assert decision.retry_after_ms == 250


async def test_queue_drains_over_time(limiter, clock):
    bucket = LeakyBucket(capacity=4, leak_rate=10)
    for _ in range(4):
        await limiter.check("c", bucket)
    clock.advance(100)
    assert (await limiter.check("c", bucket)).allowed
    assert not (await limiter.check("c", bucket)).allowed
    clock.advance(400)
    results = [(await limiter.check("c", bucket)).allowed for _ in range(5)]
    assert results == [True] * 4 + [False]


async def test_empty_queue_means_no_delay(limiter, clock):
    bucket = LeakyBucket(capacity=10, leak_rate=10)
    await limiter.check("c", bucket)
    clock.advance(1000)
    assert (await limiter.check("c", bucket)).delay_ms == 0


async def test_denied_requests_do_not_join_the_queue(limiter, clock):
    bucket = LeakyBucket(capacity=1, leak_rate=1)
    await limiter.check("c", bucket)
    for _ in range(10):
        assert not (await limiter.check("c", bucket)).allowed
    clock.advance(1000)
    decision = await limiter.check("c", bucket)
    assert decision.allowed
    assert decision.delay_ms == 0


async def test_remaining_counts_free_queue_slots(limiter):
    bucket = LeakyBucket(capacity=3, leak_rate=1)
    remaining = [(await limiter.check("c", bucket)).remaining for _ in range(3)]
    assert remaining == [2, 1, 0]


async def test_cost_takes_multiple_slots(limiter):
    bucket = LeakyBucket(capacity=6, leak_rate=10)
    first = await limiter.check("c", bucket, cost=4)
    second = await limiter.check("c", bucket, cost=4)
    assert first.allowed and first.delay_ms == 0
    assert not second.allowed
    assert second.retry_after_ms == 200


async def test_sustained_output_rate(limiter, clock):
    bucket = LeakyBucket(capacity=1, leak_rate=20)
    allowed = 0
    for _ in range(2000):
        allowed += (await limiter.check("c", bucket)).allowed
        clock.advance(1)
    assert allowed == 40


async def test_reset_reports_backlog(limiter):
    bucket = LeakyBucket(capacity=5, leak_rate=10)
    for _ in range(3):
        decision = await limiter.check("c", bucket)
    assert decision.reset_ms == 300
