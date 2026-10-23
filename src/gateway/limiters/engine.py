from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files

from redis.asyncio import Redis

from .base import Component, Decision, StrategyBase

_LUA_ORDER = (
    "_prelude",
    "token_bucket",
    "leaky_bucket",
    "fixed_window",
    "sliding_window",
    "sliding_log",
    "_dispatch",
)


def load_script() -> str:
    lua_dir = files(__package__) / "lua"
    return "\n".join((lua_dir / f"{name}.lua").read_text(encoding="utf-8") for name in _LUA_ORDER)


SCRIPT = load_script()


@dataclass(frozen=True, slots=True)
class _Plan:
    components: tuple[Component, ...]
    suffixes: tuple[str, ...]
    tail: tuple[str | int | float, ...]
    max_cost: int


class RateLimiter:
    """Runs strategies against Redis with a single atomic EVALSHA per check.

    `clock` returns epoch milliseconds and exists for deterministic tests; in production
    it is None and the script reads the Redis server clock so replicas can't skew.
    """

    def __init__(
        self,
        redis: Redis,
        prefix: str = "rl",
        clock: Callable[[], float] | None = None,
    ) -> None:
        # redis-py's Script retries with SCRIPT LOAD on NOSCRIPT (e.g. after a
        # restart or SCRIPT FLUSH), so callers never see that error.
        self._script = redis.register_script(SCRIPT)
        self._prefix = prefix
        self._clock = clock
        # Strategies come from config and live for the process, so their script
        # arguments are built once. Entries hold the strategy itself, which keeps its
        # id() from being reused while cached.
        self._plans: dict[int, tuple[StrategyBase, _Plan]] = {}

    def _plan(self, strategy: StrategyBase) -> "_Plan":
        entry = self._plans.get(id(strategy))
        if entry is not None and entry[0] is strategy:
            return entry[1]
        components = strategy.components()
        plan = _Plan(
            components=components,
            # The {hash tag} keeps all of a client's components in one cluster slot.
            suffixes=tuple(f":{i}:{c.algorithm}" for i, c in enumerate(components)),
            tail=(len(components), *(v for c in components for v in c)),
            max_cost=strategy.max_cost,
        )
        if len(self._plans) >= 1024:
            self._plans.clear()
        self._plans[id(strategy)] = (strategy, plan)
        return plan

    def keys_for(self, key: str, strategy: StrategyBase) -> list[str]:
        return [f"{self._prefix}:{{{key}}}{s}" for s in self._plan(strategy).suffixes]

    async def check(self, key: str, strategy: StrategyBase, cost: int = 1) -> Decision:
        plan = self._plan(strategy)
        if not 1 <= cost <= plan.max_cost:
            raise ValueError(f"cost must be between 1 and {plan.max_cost}, got {cost}")

        now = "" if self._clock is None else repr(float(self._clock()))
        allowed, remaining, retry, reset, delay, binding = await self._script(
            keys=[f"{self._prefix}:{{{key}}}{s}" for s in plan.suffixes],
            args=(now, cost, *plan.tail),
        )
        return Decision(
            allowed=bool(allowed),
            limit=plan.components[binding - 1].limit,
            remaining=int(remaining),
            retry_after_ms=int(retry),
            reset_ms=int(reset),
            delay_ms=int(delay),
        )
