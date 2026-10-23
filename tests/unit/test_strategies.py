import pytest
from pydantic import ValidationError

from gateway.limiters import (
    Component,
    Composite,
    FixedWindow,
    LeakyBucket,
    SlidingWindow,
    TokenBucket,
    parse_strategy,
)


@pytest.mark.parametrize(
    ("spec", "cls"),
    [
        ({"type": "token_bucket", "capacity": 10, "refill_rate": 2.5}, TokenBucket),
        ({"type": "leaky_bucket", "capacity": 10, "leak_rate": 1}, LeakyBucket),
        ({"type": "fixed_window", "limit": 10, "window_ms": 1000}, FixedWindow),
        ({"type": "sliding_window", "limit": 10, "window_ms": 1000}, SlidingWindow),
        (
            {"type": "composite", "parts": [{"type": "fixed_window", "limit": 1, "window_ms": 1}]},
            Composite,
        ),
    ],
)
def test_parse_strategy(spec, cls):
    assert isinstance(parse_strategy(spec), cls)


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "token_bucket", "capacity": 0, "refill_rate": 1},
        {"type": "token_bucket", "capacity": 10, "refill_rate": 0},
        {"type": "token_bucket", "capacity": -1, "refill_rate": 1},
        {"type": "token_bucket", "capacity": 10},
        {"type": "leaky_bucket", "capacity": 10, "leak_rate": -2},
        {"type": "fixed_window", "limit": 10, "window_ms": 0},
        {"type": "fixed_window", "limit": 1.5, "window_ms": 10},
        {"type": "sliding_window", "limit": 0, "window_ms": 1000},
        {"type": "composite", "parts": []},
        {"type": "composite", "parts": [{"type": "composite", "parts": []}]},
        {"type": "composite", "parts": [{"type": "fixed_window", "limit": 1, "window_ms": 1}] * 9},
        {"type": "fixed_window", "limit": 10, "window_ms": 1000, "extra": 1},
        {"type": "unknown"},
        {},
    ],
)
def test_invalid_specs(spec):
    with pytest.raises(ValidationError):
        parse_strategy(spec)


def test_components():
    assert TokenBucket(capacity=5, refill_rate=2).components() == (
        Component("token_bucket", 5, 2.0),
    )
    assert SlidingWindow(limit=3, window_ms=10).components()[0].algorithm == "sliding_window"
    assert (
        SlidingWindow(limit=3, window_ms=10, precise=True).components()[0].algorithm
        == "sliding_log"
    )


def test_composite_flattens_components_in_order():
    composite = Composite(
        parts=[LeakyBucket(capacity=2, leak_rate=1), FixedWindow(limit=9, window_ms=5)]
    )
    assert [c.algorithm for c in composite.components()] == ["leaky_bucket", "fixed_window"]


@pytest.mark.parametrize(
    ("strategy", "max_cost"),
    [
        (TokenBucket(capacity=7, refill_rate=1), 7),
        (FixedWindow(limit=3, window_ms=1), 3),
        (
            Composite(
                parts=[FixedWindow(limit=9, window_ms=1), TokenBucket(capacity=4, refill_rate=1)]
            ),
            4,
        ),
    ],
)
def test_max_cost_is_smallest_limit(strategy, max_cost):
    assert strategy.max_cost == max_cost


def test_strategies_are_immutable():
    bucket = TokenBucket(capacity=1, refill_rate=1)
    with pytest.raises(ValidationError):
        bucket.capacity = 2
