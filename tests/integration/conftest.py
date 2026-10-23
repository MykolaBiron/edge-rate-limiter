import asyncio
import gzip
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx
import orjson
import pytest
from aiohttp import web
from multidict import CIMultiDict, CIMultiDictProxy

from gateway.config import Settings
from gateway.main import create_app
from gateway.policy import PolicyConfig, PolicyResolver

CDN_PEER = ("10.0.0.1", 40000)  # inside trusted_proxies
CLIENT_PEER = ("203.0.113.7", 40000)  # a direct, untrusted client


def gateway_config(eu_upstream: str) -> dict:
    return {
        "zones": {
            "eu": {"countries": ["DE", "FR"], "upstream": eu_upstream},
            "embargoed": {"countries": ["KP"], "action": "block", "status": 451},
            "tor": {"countries": ["T1"], "action": "block", "status": 403},
        },
        "policies": [
            {
                "name": "default",
                "match": {"path_prefix": "/"},
                "strategy": {"type": "fixed_window", "limit": 3, "window_ms": 60000},
            },
            {
                "name": "api",
                "match": {"path_prefix": "/api"},
                "strategy": {"type": "token_bucket", "capacity": 5, "refill_rate": 1},
                "zone_overrides": {"eu": {"type": "token_bucket", "capacity": 2, "refill_rate": 1}},
            },
            {
                "name": "login",
                "match": {"path_prefix": "/login", "methods": ["POST"]},
                "identity": "ip",
                "strategy": {"type": "sliding_window", "limit": 2, "window_ms": 60000},
            },
            {
                "name": "uploads",
                "match": {"path_prefix": "/upload"},
                "shape": True,
                "strategy": {"type": "leaky_bucket", "capacity": 3, "leak_rate": 10},
            },
            {
                "name": "heavy",
                "match": {"path_prefix": "/report"},
                "cost": 5,
                "strategy": {"type": "token_bucket", "capacity": 10, "refill_rate": 1},
            },
            {
                "name": "bulk",
                "match": {"path_prefix": "/bulk"},
                "strategy": {"type": "fixed_window", "limit": 100, "window_ms": 60000},
            },
        ],
    }


@dataclass
class Received:
    server: str
    method: str
    path: str
    headers: CIMultiDictProxy
    body: bytes


class Upstreams:
    """Two real HTTP servers ("default" and "eu") that record what they receive.

    Paths ending in /slow sleep past the gateway timeout, /gzip return a gzip body,
    /large a body too big to buffer, /chunked a body without a length and /cookie set a
    cookie.
    """

    GZIP_BODY = gzip.compress(b"compressed payload")
    LARGE_BODY = bytes(range(256)) * 800  # 200 KiB: above the proxy's buffering limit

    def __init__(self) -> None:
        self.requests: list[Received] = []
        self.urls: dict[str, str] = {}
        self._runners: list[web.AppRunner] = []
        self._drop_server: asyncio.Server | None = None

    @property
    def default_url(self) -> str:
        return self.urls["default"]

    async def start(self) -> None:
        for name in ("default", "eu"):
            app = web.Application()
            app.router.add_route("*", "/{tail:.*}", self._handler(name))
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            self.urls[name] = f"http://127.0.0.1:{port}"
            self._runners.append(runner)

        # Accepts connections and slams them shut: a crashing upstream.
        async def drop(reader, writer):
            writer.close()

        self._drop_server = await asyncio.start_server(drop, "127.0.0.1", 0)
        port = self._drop_server.sockets[0].getsockname()[1]
        self.urls["drop"] = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        for runner in self._runners:
            await runner.cleanup()
        if self._drop_server is not None:
            self._drop_server.close()
            await self._drop_server.wait_closed()

    def _handler(self, name: str):
        async def handle(request: web.Request) -> web.StreamResponse:
            body = await request.read()
            self.requests.append(
                Received(name, request.method, request.raw_path, request.headers, body)
            )
            headers = CIMultiDict(
                [
                    ("x-upstream", name),
                    ("set-cookie", "a=1"),
                    ("set-cookie", "b=2"),
                    ("proxy-authenticate", "Basic"),
                ]
            )
            if request.path.endswith("/slow"):
                await asyncio.sleep(2)
            if request.path.endswith("/gzip"):
                headers["content-encoding"] = "gzip"
                return web.Response(body=self.GZIP_BODY, headers=headers)
            if request.path.endswith("/large"):
                return web.Response(body=self.LARGE_BODY, headers=headers)
            if request.path.endswith("/chunked"):
                stream = web.StreamResponse(headers=headers)
                stream.enable_chunked_encoding()
                await stream.prepare(request)
                for part in (b"one,", b"two,", b"three"):
                    await stream.write(part)
                await stream.write_eof()
                return stream
            if request.path.endswith("/cookie"):
                headers["set-cookie"] = "session=secret; Path=/"
            payload = orjson.dumps({"server": name, "path": request.raw_path})
            return web.Response(body=payload, headers=headers, content_type="application/json")

        return handle


@pytest.fixture
async def upstream():
    upstreams = Upstreams()
    await upstreams.start()
    yield upstreams
    await upstreams.stop()


@pytest.fixture
async def gateway(redis, clock, upstream):
    """Factory returning (app, client_for); everything is torn down after the test."""
    stack = AsyncExitStack()

    async def build(config=None, redis_client=None, breaker=None, **settings):
        settings.setdefault("upstream_url", upstream.default_url)
        app = create_app(
            Settings(_env_file=None, trusted_proxies=["10.0.0.0/8"], **settings),
            resolver=PolicyResolver(
                PolicyConfig.model_validate(config or gateway_config(upstream.urls["eu"]))
            ),
            redis=redis_client or redis,
            clock=clock,
            breaker=breaker,
        )
        await stack.enter_async_context(app.router.lifespan_context(app))

        def client_for(peer=CLIENT_PEER) -> httpx.AsyncClient:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=peer), base_url="http://gw"
            )
            stack.push_async_callback(client.aclose)
            return client

        return app, client_for

    yield build
    await stack.aclose()
