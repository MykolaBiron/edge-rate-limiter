import aiohttp
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from yarl import URL

# RFC 9110 section 7.6.1: connection-specific headers are never forwarded.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


BUFFER_LIMIT = 64 * 1024


class UpstreamError(Exception):
    def __init__(self, kind: str, status: int) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status


def _strip(headers, extra: frozenset[str] = frozenset()) -> list[tuple[bytes, bytes]]:
    connection_tokens: set[str] = set()
    for name, value in headers:
        if name.lower() == b"connection":
            connection_tokens.update(t.strip().lower() for t in value.decode("latin-1").split(","))
    drop = HOP_BY_HOP | extra | connection_tokens
    return [(n, v) for n, v in headers if n.decode("latin-1").lower() not in drop]


class UpstreamProxy:
    """Streams requests to the upstream over a pooled aiohttp session.

    aiohttp rather than httpx: under 64 concurrent requests httpx's pool fell to ~570
    req/s in our benchmark while aiohttp sustained ~10k (see bench/RESULTS.md).
    """

    def __init__(self, default_upstream: str, timeout_s: float, max_connections: int) -> None:
        self.default_upstream = default_upstream.rstrip("/")
        self._timeout_s = timeout_s
        self._max_connections = max_connections
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        # Must run inside the event loop, hence not in __init__.
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=self._max_connections, ttl_dns_cache=10),
            timeout=aiohttp.ClientTimeout(total=self._timeout_s),
            # Pass bodies through untouched; the client negotiated the encoding.
            auto_decompress=False,
            # A shared session must never remember one client's cookies for the next.
            cookie_jar=aiohttp.DummyCookieJar(),
            # Don't add headers the client didn't send (an added Accept-Encoding would
            # get the client a compressed body it never asked for).
            skip_auto_headers=("User-Agent", "Accept-Encoding", "Accept"),
        )

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def forward(self, request: Request, upstream: str | None, peer: str | None) -> Response:
        assert self._session is not None, "start() was not awaited"
        raw_path = request.scope.get("raw_path") or request.url.path.encode()
        query = request.scope.get("query_string", b"")
        target = (upstream or self.default_upstream).rstrip("/") + raw_path.decode("latin-1")
        if query:
            target += "?" + query.decode("latin-1")

        headers = [
            (n.decode("latin-1"), v.decode("latin-1"))
            for n, v in _strip(request.headers.raw, frozenset({"host", "x-forwarded-for"}))
        ]
        prior = request.headers.get("x-forwarded-for")
        if peer is not None:
            headers.append(("X-Forwarded-For", f"{prior}, {peer}" if prior else peer))
        elif prior:
            headers.append(("X-Forwarded-For", prior))
        headers.append(("X-Forwarded-Proto", request.url.scheme))

        has_body = "content-length" in request.headers or "transfer-encoding" in request.headers
        try:
            response = await self._session.request(
                request.method,
                URL(target, encoded=True),  # keep %2F and friends exactly as sent
                headers=headers,
                data=request.stream() if has_body else None,
                allow_redirects=False,
            )
        except TimeoutError as exc:
            raise UpstreamError("timeout", 504) from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise UpstreamError("unavailable", 502) from exc

        headers = [(n.lower(), v) for n, v in _strip(response.raw_headers)]
        length = response.content_length
        if length is not None and length <= BUFFER_LIMIT:
            # Small bodies (the common API case) are cheaper to buffer than to stream.
            try:
                body = await response.read()
            except TimeoutError as exc:
                raise UpstreamError("timeout", 504) from exc
            except (aiohttp.ClientError, OSError) as exc:
                raise UpstreamError("unavailable", 502) from exc
            finally:
                response.release()
            proxied = Response(body, status_code=response.status)
        else:

            async def chunks():
                try:
                    async for chunk in response.content.iter_any():
                        yield chunk
                finally:
                    response.release()

            async def release():
                # Async on purpose: Starlette runs sync background tasks in a thread.
                response.release()

            # The background task also covers bodies that are never iterated (HEAD).
            proxied = StreamingResponse(
                chunks(), status_code=response.status, background=BackgroundTask(release)
            )
        # Assigned raw so repeated headers such as Set-Cookie survive.
        proxied.raw_headers = headers
        return proxied
