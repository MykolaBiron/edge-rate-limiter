from typing import Literal

from pydantic import PositiveInt

from .base import Component, StrategyBase


class FixedWindow(StrategyBase):
    type: Literal["fixed_window"] = "fixed_window"
    limit: PositiveInt
    window_ms: PositiveInt

    def components(self) -> tuple[Component, ...]:
        return (Component("fixed_window", self.limit, self.window_ms),)
