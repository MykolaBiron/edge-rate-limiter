"""Minimal echo service standing in for the protected backend.

Raw ASGI rather than a framework, so benchmarks measure the gateway and not this.
"""

import os

import orjson

REGION = os.environ.get("REGION", "local")
REGION_HEADER = (b"x-upstream-region", REGION.encode())


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    body_size = 0
    more = True
    while more:
        message = await receive()
        body_size += len(message.get("body", b""))
        more = message.get("more_body", False)

    headers = {k.decode(): v.decode() for k, v in scope["headers"]}
    payload = orjson.dumps(
        {
            "region": REGION,
            "method": scope["method"],
            "path": scope["path"],
            "query": scope["query_string"].decode(),
            "body_bytes": body_size,
            "x_forwarded_for": headers.get("x-forwarded-for"),
        }
    )
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                REGION_HEADER,
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
