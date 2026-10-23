from typing import Literal

from pydantic import PositiveFloat, PositiveInt

from .base import Component, StrategyBase


class LeakyBucket(StrategyBase):
    """Queue of at most `capacity` requests draining at `leak_rate` requests/second.

    Admitted requests carry a delay; a shaping policy holds them for that long so the
    upstream sees a constant rate.
    """

    type: Literal["leaky_bucket"] = "leaky_bucket"
    capacity: PositiveInt
    leak_rate: PositiveFloat

    def components(self) -> tuple[Component, ...]:
        return (Component("leaky_bucket", self.capacity, self.leak_rate),)
