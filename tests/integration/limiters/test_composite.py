from gateway.limiters import Composite, FixedWindow, LeakyBucket, SlidingWindow, TokenBucket

BURST_AND_QUOTA = Composite(
    parts=[
        TokenBucket(capacity=5, refill_rate=10),
        FixedWindow(limit=8, window_ms=60_000),
    ]
)


async def test_tightest_part_wins(limiter, clock):
    results = []
    for _ in range(20):
        results.append((await limiter.check("c", BURST_AND_QUOTA)).allowed)
        clock.advance(100)
    assert sum(results) == 8


async def test_denial_consumes_nothing_from_other_parts(limiter, redis):
    for _ in range(5):
        assert (await limiter.check("c", BURST_AND_QUOTA)).allowed
    for _ in range(10):
        assert not (await limiter.check("c", BURST_AND_QUOTA)).allowed
    window_key = limiter.keys_for("c", BURST_AND_QUOTA)[1]
    assert int(await redis.hget(window_key, "count")) == 5


async def test_reports_binding_limit(limiter, clock):
    first = await limiter.check("c", BURST_AND_QUOTA)
    assert first.limit == 5  # token bucket has fewer remaining
    for _ in range(4):
        await limiter.check("c", BURST_AND_QUOTA)
    burst_denied = await limiter.check("c", BURST_AND_QUOTA)
    assert not burst_denied.allowed
    assert burst_denied.limit == 5
    assert burst_denied.retry_after_ms == 100

    for _ in range(3):
        clock.advance(100)
        await limiter.check("c", BURST_AND_QUOTA)
    clock.advance(1000)
    quota_denied = await limiter.check("c", BURST_AND_QUOTA)
    assert not quota_denied.allowed
    assert quota_denied.limit == 8


async def test_retry_is_longest_wait_among_denying_parts(limiter):
    composite = Composite(
        parts=[TokenBucket(capacity=1, refill_rate=1), TokenBucket(capacity=1, refill_rate=0.25)]
    )
    await limiter.check("c", composite)
    decision = await limiter.check("c", composite)
    assert decision.retry_after_ms == 4000


async def test_remaining_is_minimum(limiter):
    composite = Composite(
        parts=[FixedWindow(limit=100, window_ms=1000), FixedWindow(limit=3, window_ms=60_000)]
    )
    decision = await limiter.check("c", composite)
    assert decision.remaining == 2


async def test_mixes_every_algorithm(limiter):
    composite = Composite(
        parts=[
            TokenBucket(capacity=10, refill_rate=1),
            LeakyBucket(capacity=10, leak_rate=1),
            FixedWindow(limit=10, window_ms=1000),
            SlidingWindow(limit=10, window_ms=1000),
            SlidingWindow(limit=4, window_ms=1000, precise=True),
        ]
    )
    results = [(await limiter.check("c", composite)).allowed for _ in range(6)]
    assert results == [True] * 4 + [False] * 2


async def test_carries_leaky_bucket_delay(limiter):
    composite = Composite(
        parts=[LeakyBucket(capacity=5, leak_rate=10), FixedWindow(limit=100, window_ms=1000)]
    )
    delays = [(await limiter.check("c", composite)).delay_ms for _ in range(3)]
    assert delays == [0, 100, 200]


async def test_parts_share_cluster_hash_slot(limiter):
    keys = limiter.keys_for("user:42", BURST_AND_QUOTA)
    assert len(keys) == 2
    assert all("{user:42}" in key for key in keys)
