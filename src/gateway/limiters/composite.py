from typing import Annotated, Literal

from pydantic import Field

from .base import Component, StrategyBase
from .fixed_window import FixedWindow
from .leaky_bucket import LeakyBucket
from .sliding_window import SlidingWindow
from .token_bucket import TokenBucket

SimpleStrategy = Annotated[
    TokenBucket | LeakyBucket | FixedWindow | SlidingWindow,
    Field(discriminator="type"),
]


class Composite(StrategyBase):
    """Every part must allow the request; state is only consumed when all of them do.

    Typical use: a short burst limit plus a long sustained quota.
    """

    type: Literal["composite"] = "composite"
    parts: list[SimpleStrategy] = Field(min_length=1, max_length=8)

    def components(self) -> tuple[Component, ...]:
        return tuple(c for part in self.parts for c in part.components())
