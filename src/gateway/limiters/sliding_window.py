from typing import Literal

from pydantic import PositiveInt

from .base import Component, StrategyBase


class SlidingWindow(StrategyBase):
    """Sliding window limit.

    The default is the weighted two-counter approximation (O(1) memory per client).
    `precise=True` switches to a sorted-set log, exact but O(limit) memory.
    """

    type: Literal["sliding_window"] = "sliding_window"
    limit: PositiveInt
    window_ms: PositiveInt
    precise: bool = False

    def components(self) -> tuple[Component, ...]:
        algorithm = "sliding_log" if self.precise else "sliding_window"
        return (Component(algorithm, self.limit, self.window_ms),)
