from typing import Annotated

from pydantic import Field, TypeAdapter

from .base import Component, Decision, StrategyBase
from .composite import Composite
from .engine import RateLimiter
from .fixed_window import FixedWindow
from .leaky_bucket import LeakyBucket
from .sliding_window import SlidingWindow
from .token_bucket import TokenBucket

Strategy = Annotated[
    TokenBucket | LeakyBucket | FixedWindow | SlidingWindow | Composite,
    Field(discriminator="type"),
]

_strategy_adapter: TypeAdapter[Strategy] = TypeAdapter(Strategy)


def parse_strategy(spec: dict) -> StrategyBase:
    return _strategy_adapter.validate_python(spec)


__all__ = [
    "Component",
    "Composite",
    "Decision",
    "FixedWindow",
    "LeakyBucket",
    "RateLimiter",
    "SlidingWindow",
    "Strategy",
    "StrategyBase",
    "TokenBucket",
    "parse_strategy",
]
