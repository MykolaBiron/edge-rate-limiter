from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# Limiter checks are a single Redis round trip, so the interesting range is sub-ms.
_FAST_BUCKETS = (0.0001, 0.00025, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1)
_UPSTREAM_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


class Metrics:
    """All gateway metrics on a private registry, so several apps can coexist in tests."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "gateway_requests",
            "Requests by policy, compliance zone and outcome",
            ["policy", "zone", "outcome"],
            registry=self.registry,
        )
        self.limiter_latency = Histogram(
            "gateway_limiter_duration_seconds",
            "Time spent on the rate-limit decision (Redis round trip included)",
            ["policy"],
            buckets=_FAST_BUCKETS,
            registry=self.registry,
        )
        self.upstream_latency = Histogram(
            "gateway_upstream_duration_seconds",
            "Time until upstream response headers arrive",
            buckets=_UPSTREAM_BUCKETS,
            registry=self.registry,
        )
        self.upstream_errors = Counter(
            "gateway_upstream_errors",
            "Failed upstream requests",
            ["kind"],
            registry=self.registry,
        )
        self.shaping_delay = Histogram(
            "gateway_shaping_delay_seconds",
            "Delay added by traffic shaping",
            ["policy"],
            buckets=_UPSTREAM_BUCKETS,
            registry=self.registry,
        )
        self.redis_errors = Counter(
            "gateway_redis_errors", "Failed limiter calls to Redis", registry=self.registry
        )
        self.circuit_open = Gauge(
            "gateway_redis_circuit_open",
            "1 while the Redis circuit breaker is open",
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)
