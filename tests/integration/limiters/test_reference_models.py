"""Differential tests: random traffic through the Lua scripts and the Python models."""

import copy
import random

import pytest

from gateway.limiters import Composite, FixedWindow, LeakyBucket, SlidingWindow, TokenBucket
from tests import reference

# Small advances keep requests colliding; large ones cross windows and refill buckets.
ADVANCES = [0, 0, 0, 1, 3, 17, 50, 100, 250, 333, 999, 1000, 2500]

CASES = {
    "token_bucket": (
        TokenBucket(capacity=7, refill_rate=4),
        lambda: reference.TokenBucketModel(7, 4),
    ),
    "leaky_bucket": (
        LeakyBucket(capacity=6, leak_rate=5),
        lambda: reference.LeakyBucketModel(6, 5),
    ),
    "fixed_window": (
        FixedWindow(limit=9, window_ms=1000),
        lambda: reference.FixedWindowModel(9, 1000),
    ),
    "sliding_counter": (
        SlidingWindow(limit=8, window_ms=1000),
        lambda: reference.SlidingCounterModel(8, 1000),
    ),
    "sliding_log": (
        SlidingWindow(limit=8, window_ms=1000, precise=True),
        lambda: reference.SlidingLogModel(8, 1000),
    ),
}


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("name", list(CASES))
async def test_matches_reference_model(limiter, clock, name, seed):
    strategy, make_model = CASES[name]
    model = make_model()
    rng = random.Random(f"{name}-{seed}")
    for step in range(150):
        clock.advance(rng.choice(ADVANCES))
        cost = rng.choice([1, 1, 1, 2, 3])
        got = await limiter.check("c", strategy, cost=cost)
        want = model.check(clock.now, cost)
        context = f"step {step} at +{clock.now - 1_700_002_800_000:.0f}ms cost {cost}"
        assert got.allowed == want.allowed, context
        assert got.remaining == want.remaining, context
        assert abs(got.retry_after_ms - want.retry_after_ms) <= 1, context


@pytest.mark.parametrize("seed", range(12))
async def test_composite_matches_all_or_nothing_model(limiter, clock, seed):
    strategy = Composite(
        parts=[
            TokenBucket(capacity=5, refill_rate=3),
            FixedWindow(limit=12, window_ms=2000),
            SlidingWindow(limit=10, window_ms=1000, precise=True),
        ]
    )
    rng = random.Random(seed)
    models = [
        reference.TokenBucketModel(5, 3),
        reference.FixedWindowModel(12, 2000),
        reference.SlidingLogModel(10, 1000),
    ]
    for step in range(150):
        clock.advance(rng.choice(ADVANCES))
        cost = rng.choice([1, 1, 2])
        got = await limiter.check("c", strategy, cost=cost)

        # Dry-run every model on a copy; commit only if all of them allow.
        trial = [copy.deepcopy(m) for m in models]
        results = [m.check(clock.now, cost) for m in trial]
        expected = all(r.allowed for r in results)
        if expected:
            models = trial
        assert got.allowed == expected, f"step {step}"
        assert got.remaining == min(r.remaining for r in results), f"step {step}"
