import asyncio
import logging
import math
import time

import orjson
from redis.exceptions import RedisError
from starlette.requests import Request
from starlette.responses import Response

from .config import Settings
from .geo import UNKNOWN, parse_geo
from .identity import TrustedProxies, identify, parse_ip
from .limiters import Decision, RateLimiter, StrategyBase
from .metrics import Metrics
from .policy import PolicyResolver
from .proxy import UpstreamError, UpstreamProxy
from .redis_client import CircuitBreaker

UNLIMITED = "-"
log = logging.getLogger("gateway")


def json_response(body: dict, status: int = 200, headers: dict | None = None) -> Response:
    return Response(orjson.dumps(body), status, headers, media_type="application/json")


def rate_limit_headers(decision: Decision, policy: str) -> dict[str, str]:
    headers = {
        "x-ratelimit-limit": str(decision.limit),
        "x-ratelimit-remaining": str(max(0, decision.remaining)),
        "x-ratelimit-reset": str(math.ceil(decision.reset_ms / 1000)),
        "x-ratelimit-policy": policy,
    }
    if not decision.allowed:
        headers["retry-after"] = str(max(1, math.ceil(decision.retry_after_ms / 1000)))
    return headers


class LimiterUnavailable(Exception):
    pass


class Gateway:
    """The per-request pipeline: identify -> resolve policy -> limit -> shape -> proxy."""

    def __init__(
        self,
        settings: Settings,
        resolver: PolicyResolver,
        limiter: RateLimiter,
        proxy: UpstreamProxy,
        metrics: Metrics,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.limiter = limiter
        self.proxy = proxy
        self.metrics = metrics
        self.breaker = breaker or CircuitBreaker()
        self.trusted = TrustedProxies(settings.trusted_proxies)
        self._last_error_log = float("-inf")
        # .labels() takes a lock and hashes the label tuple on every call.
        self._latency_by_policy: dict[str, object] = {}

    async def decide(self, key: str, strategy: StrategyBase, cost: int, policy: str) -> Decision:
        was_open = self.breaker.is_open
        if not self.breaker.allow():
            raise LimiterUnavailable("circuit open")
        if was_open:
            self.metrics.circuit_open.set(0)
        start = time.perf_counter()
        try:
            decision = await self.limiter.check(key, strategy, cost)
        except (RedisError, OSError, TimeoutError) as exc:
            self.metrics.redis_errors.inc()
            self.breaker.record_failure()
            self.metrics.circuit_open.set(int(self.breaker.is_open))
            # Throttled: during an outage this would otherwise log on every request.
            now = time.monotonic()
            if now - self._last_error_log >= 5:
                self._last_error_log = now
                log.warning("rate limiter unavailable: %r", exc)
            raise LimiterUnavailable(str(exc)) from exc
        elapsed = time.perf_counter() - start
        histogram = self._latency_by_policy.get(policy)
        if histogram is None:
            histogram = self._latency_by_policy[policy] = self.metrics.limiter_latency.labels(
                policy
            )
        histogram.observe(elapsed)
        self.breaker.record_success()
        return decision

    async def handle(self, request: Request) -> Response:
        peer = request.client.host if request.client else None
        headers = request.headers
        geo = parse_geo(headers) if parse_ip(peer) in self.trusted else UNKNOWN
        client = identify(peer, headers, self.trusted, self.settings.ipv6_prefix)
        res = self.resolver.resolve(request.url.path, request.method, geo, client)

        if res.blocked:
            self.metrics.requests.labels(UNLIMITED, res.zone, "blocked").inc()
            return json_response(
                {"error": "unavailable in your region", "zone": res.zone}, res.block_status
            )

        extra_headers: dict[str, str] = {}
        if res.policy is not None:
            name = res.policy.name
            try:
                decision = await self.decide(res.limiter_key, res.strategy, res.policy.cost, name)
            except LimiterUnavailable:
                if self.settings.fail_mode == "closed":
                    self.metrics.requests.labels(name, res.zone, "unavailable").inc()
                    return json_response({"error": "rate limiter unavailable"}, 503)
                self.metrics.requests.labels(name, res.zone, "degraded").inc()
                extra_headers["x-ratelimit-status"] = "degraded"
            else:
                extra_headers = rate_limit_headers(decision, name)
                if not decision.allowed:
                    self.metrics.requests.labels(name, res.zone, "limited").inc()
                    return json_response(
                        {"error": "rate limit exceeded", "policy": name},
                        429,
                        extra_headers,
                    )
                if res.policy.shape and decision.delay_ms > 0:
                    self.metrics.shaping_delay.labels(name).observe(decision.delay_ms / 1000)
                    await asyncio.sleep(decision.delay_ms / 1000)
                self.metrics.requests.labels(name, res.zone, "allowed").inc()
        else:
            self.metrics.requests.labels(UNLIMITED, res.zone, "unlimited").inc()

        extra_headers["x-gateway-zone"] = res.zone
        start = time.perf_counter()
        try:
            response = await self.proxy.forward(request, res.upstream, peer)
        except UpstreamError as exc:
            self.metrics.upstream_errors.labels(exc.kind).inc()
            return json_response({"error": f"upstream {exc.kind}"}, exc.status, extra_headers)
        self.metrics.upstream_latency.observe(time.perf_counter() - start)
        response.headers.update(extra_headers)
        return response

    async def check(self, request: Request) -> Response:
        """Decision-only endpoint: runs a policy's limiter without proxying.

        Useful for measuring the limiter itself and for services that want to ask the
        gateway before doing expensive work.
        """
        params = request.query_params
        policy = self.resolver.by_name.get(params.get("policy", ""))
        key = params.get("key")
        if policy is None or not key:
            return json_response({"error": "known 'policy' and non-empty 'key' required"}, 400)
        try:
            decision = await self.decide(
                f"{policy.name}:check:{key}", policy.strategy, 1, policy.name
            )
        except LimiterUnavailable:
            return json_response({"error": "rate limiter unavailable"}, 503)
        return json_response(
            {
                "allowed": decision.allowed,
                "limit": decision.limit,
                "remaining": decision.remaining,
                "retry_after_ms": decision.retry_after_ms,
                "reset_ms": decision.reset_ms,
                "delay_ms": decision.delay_ms,
            },
            200 if decision.allowed else 429,
            rate_limit_headers(decision, policy.name),
        )
