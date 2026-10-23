import pytest

from gateway.limiters import TokenBucket


@pytest.mark.parametrize("capacity", [1, 2, 5, 50, 500])
async def test_allows_full_burst_then_denies(limiter, capacity):
    bucket = TokenBucket(capacity=capacity, refill_rate=1)
    for i in range(capacity):
        decision = await limiter.check("c", bucket)
        assert decision.allowed
        assert decision.remaining == capacity - i - 1
    assert not (await limiter.check("c", bucket)).allowed


@pytest.mark.parametrize(
    ("rate", "expected_retry"), [(1, 1000), (2, 500), (4, 250), (10, 100), (0.5, 2000), (1000, 1)]
)
async def test_retry_after_is_time_to_next_token(limiter, rate, expected_retry):
    bucket = TokenBucket(capacity=1, refill_rate=rate)
    await limiter.check("c", bucket)
    decision = await limiter.check("c", bucket)
    assert not decision.allowed
    assert decision.retry_after_ms == expected_retry


async def test_refills_over_time(limiter, clock):
    bucket = TokenBucket(capacity=3, refill_rate=2)
    for _ in range(3):
        await limiter.check("c", bucket)
    clock.advance(499)
    assert not (await limiter.check("c", bucket)).allowed
    clock.advance(1)
    assert (await limiter.check("c", bucket)).allowed
    assert not (await limiter.check("c", bucket)).allowed


async def test_partial_refill_reports_remaining_wait(limiter, clock):
    bucket = TokenBucket(capacity=1, refill_rate=1)
    await limiter.check("c", bucket)
    clock.advance(400)
    decision = await limiter.check("c", bucket)
    assert not decision.allowed
    assert decision.retry_after_ms == 600


async def test_never_exceeds_capacity_after_idle(limiter, clock):
    bucket = TokenBucket(capacity=5, refill_rate=100)
    await limiter.check("c", bucket)
    clock.advance(3_600_000)
    results = [(await limiter.check("c", bucket)).allowed for _ in range(6)]
    assert results == [True] * 5 + [False]


@pytest.mark.parametrize(("cost", "allowed_count"), [(1, 10), (2, 5), (3, 3), (4, 2), (10, 1)])
async def test_cost_consumes_multiple_tokens(limiter, cost, allowed_count):
    bucket = TokenBucket(capacity=10, refill_rate=1)
    results = [
        (await limiter.check("c", bucket, cost=cost)).allowed for _ in range(allowed_count + 1)
    ]
    assert results == [True] * allowed_count + [False]


async def test_retry_accounts_for_cost(limiter):
    bucket = TokenBucket(capacity=10, refill_rate=2)
    await limiter.check("c", bucket, cost=8)
    decision = await limiter.check("c", bucket, cost=4)
    assert not decision.allowed
    assert decision.remaining == 2
    assert decision.retry_after_ms == 1000


async def test_denied_requests_do_not_consume(limiter, clock):
    bucket = TokenBucket(capacity=1, refill_rate=1)
    await limiter.check("c", bucket)
    for _ in range(20):
        assert not (await limiter.check("c", bucket)).allowed
    clock.advance(1000)
    assert (await limiter.check("c", bucket)).allowed


async def test_clock_going_backwards_adds_no_tokens(limiter, clock):
    bucket = TokenBucket(capacity=2, refill_rate=1)
    await limiter.check("c", bucket)
    await limiter.check("c", bucket)
    clock.advance(-10_000)
    assert not (await limiter.check("c", bucket)).allowed
    clock.advance(10_000)
    assert not (await limiter.check("c", bucket)).allowed
    clock.advance(1000)
    assert (await limiter.check("c", bucket)).allowed


async def test_reset_is_time_until_full(limiter):
    bucket = TokenBucket(capacity=10, refill_rate=10)
    decision = await limiter.check("c", bucket, cost=3)
    assert decision.reset_ms == 300


async def test_keys_are_isolated(limiter):
    bucket = TokenBucket(capacity=1, refill_rate=1)
    assert (await limiter.check("a", bucket)).allowed
    assert (await limiter.check("b", bucket)).allowed
    assert not (await limiter.check("a", bucket)).allowed


async def test_state_expires_once_bucket_would_be_full(limiter, redis):
    bucket = TokenBucket(capacity=10, refill_rate=5)
    await limiter.check("c", bucket)
    (key,) = limiter.keys_for("c", bucket)
    ttl = await redis.pttl(key)
    assert 2000 < ttl <= 3000


async def test_sustained_rate_matches_refill(limiter, clock):
    bucket = TokenBucket(capacity=1, refill_rate=10)
    allowed = 0
    for _ in range(1000):  # 1000 attempts, one per ms, over one second
        allowed += (await limiter.check("c", bucket)).allowed
        clock.advance(1)
    assert allowed == 10
