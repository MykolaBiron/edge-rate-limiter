from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from starlette.requests import Request
from starlette.responses import Response

from .config import Settings
from .limiters import RateLimiter
from .metrics import Metrics
from .policy import PolicyResolver
from .proxy import UpstreamProxy
from .redis_client import CircuitBreaker, create_redis
from .service import Gateway, json_response

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_app(
    settings: Settings | None = None,
    *,
    resolver: PolicyResolver | None = None,
    redis: Redis | None = None,
    clock: Callable[[], float] | None = None,
    breaker: CircuitBreaker | None = None,
) -> FastAPI:
    """Build the gateway. The keyword arguments exist so tests can swap in fakes."""
    settings = settings or Settings()
    resolver = resolver or PolicyResolver.from_file(settings.policies_file)
    owns_redis = redis is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = redis if redis is not None else create_redis(settings)
        proxy = UpstreamProxy(
            settings.upstream_url,
            settings.upstream_timeout_s,
            settings.upstream_max_connections,
        )
        await proxy.start()
        app.state.redis = client
        app.state.gateway = Gateway(
            settings,
            resolver,
            RateLimiter(client, prefix=settings.key_prefix, clock=clock),
            proxy,
            Metrics(),
            breaker,
        )
        try:
            yield
        finally:
            await proxy.aclose()
            if owns_redis:
                await client.aclose()

    app = FastAPI(
        title="Edge Rate Limiter",
        lifespan=lifespan,
        docs_url="/_gw/docs",
        openapi_url="/_gw/openapi.json",
        redoc_url=None,
    )

    @app.get("/_gw/health")
    async def health() -> Response:
        return json_response({"status": "ok"})

    @app.get("/_gw/ready")
    async def ready(request: Request) -> Response:
        try:
            await request.app.state.redis.ping()
        except Exception:
            return json_response({"status": "redis unavailable"}, 503)
        return json_response({"status": "ready"})

    @app.get("/_gw/metrics")
    async def metrics(request: Request) -> Response:
        return Response(
            request.app.state.gateway.metrics.render(),
            media_type="text/plain; version=0.0.4",
        )

    # The hot paths are plain Starlette routes, skipping FastAPI's per-request
    # dependency resolution and validation.
    if settings.enable_check_endpoint:

        async def check(request: Request) -> Response:
            return await request.app.state.gateway.check(request)

        app.router.add_route("/_gw/check", check, methods=["GET"])

    async def proxy_endpoint(request: Request) -> Response:
        return await request.app.state.gateway.handle(request)

    app.router.add_route("/{path:path}", proxy_endpoint, methods=PROXY_METHODS)
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory gateway.main:app_factory`."""
    return create_app()
