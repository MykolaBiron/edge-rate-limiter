from typing import Literal

from pydantic import PositiveFloat, PositiveInt

from .base import Component, StrategyBase


class TokenBucket(StrategyBase):
    """Allows bursts up to `capacity`, refilling at `refill_rate` tokens/second."""

    type: Literal["token_bucket"] = "token_bucket"
    capacity: PositiveInt
    refill_rate: PositiveFloat

    def components(self) -> tuple[Component, ...]:
        return (Component("token_bucket", self.capacity, self.refill_rate),)
