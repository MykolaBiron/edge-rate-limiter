from dataclasses import dataclass
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict


class Component(NamedTuple):
    """One algorithm instance as the Lua dispatcher sees it."""

    algorithm: str
    limit: int
    param: float


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_ms: int
    reset_ms: int
    delay_ms: int = 0
    degraded: bool = False


class StrategyBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def components(self) -> tuple[Component, ...]:
        raise NotImplementedError

    @property
    def max_cost(self) -> int:
        return min(c.limit for c in self.components())
