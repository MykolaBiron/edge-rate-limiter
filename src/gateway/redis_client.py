import time
from collections.abc import Callable

from redis.asyncio import BlockingConnectionPool, Redis

from .config import Settings


def create_redis(settings: Settings) -> Redis:
    timeout = settings.redis_timeout_ms / 1000
    # Blocking pool: under a burst, callers wait briefly for a connection instead of
    # opening thousands of sockets to Redis.
    pool = BlockingConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        timeout=timeout,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        health_check_interval=30,
    )
    return Redis(connection_pool=pool)


class CircuitBreaker:
    """Stops calling Redis for `cooldown_s` once it is clearly down.

    Without it, every request during an outage would wait out the Redis timeout before
    the fail-open/fail-closed policy kicks in.

    It trips when, within the current `window_s`, at least `min_failures` calls failed
    and they make up at least `failure_ratio` of all calls. A ratio rather than a run of
    consecutive failures: at thousands of requests per second a brief latency spike
    yields a handful of timeouts in a row with Redis perfectly healthy, and opening the
    circuit then turns a few slow requests into a whole cooldown of rejected traffic.
    After the cooldown the circuit closes and counting starts over.
    """

    def __init__(
        self,
        min_failures: int = 20,
        failure_ratio: float = 0.5,
        window_s: float = 1.0,
        cooldown_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_failures = min_failures
        self.failure_ratio = failure_ratio
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._window_start = clock()
        self._calls = 0
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        now = self._clock()
        if now - self._opened_at < self.cooldown_s:
            return False
        self._opened_at = None
        self._reset(now)
        return True

    def record_success(self) -> None:
        self._roll()
        self._calls += 1

    def record_failure(self) -> None:
        now = self._roll()
        self._calls += 1
        self._failures += 1
        if (
            self._failures >= self.min_failures
            and self._failures >= self.failure_ratio * self._calls
        ):
            self._opened_at = now

    def _roll(self) -> float:
        now = self._clock()
        if now - self._window_start >= self.window_s:
            self._reset(now)
        return now

    def _reset(self, now: float) -> None:
        self._window_start = now
        self._calls = 0
        self._failures = 0
