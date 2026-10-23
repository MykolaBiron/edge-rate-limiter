import re
import time

import pytest

from tests.integration.conftest import CDN_PEER, CLIENT_PEER, Upstreams


async def test_allowed_request_is_proxied_with_rate_limit_headers(gateway, upstream):
    _, client_for = await gateway()
    response = await client_for().get("/api/users?page=2")
    assert response.status_code == 200
    assert response.json() == {"server": "default", "path": "/api/users?page=2"}
    assert response.headers["x-ratelimit-limit"] == "5"
    assert response.headers["x-ratelimit-remaining"] == "4"
    assert response.headers["x-ratelimit-policy"] == "api"
    assert response.headers["x-gateway-zone"] == "default"
    assert "retry-after" not in response.headers
    assert len(upstream.requests) == 1


async def test_exceeding_the_limit_returns_429_without_touching_upstream(gateway, upstream):
    _, client_for = await gateway()
    client = client_for()
    statuses = [(await client.get("/api/x")).status_code for _ in range(5)]
    denied = await client.get("/api/x")
    assert statuses == [200] * 5
    assert denied.status_code == 429
    assert denied.headers["retry-after"] == "1"
    assert denied.headers["x-ratelimit-remaining"] == "0"
    assert denied.json() == {"error": "rate limit exceeded", "policy": "api"}
    assert len(upstream.requests) == 5


async def test_limit_recovers_after_retry_after(gateway, clock):
    _, client_for = await gateway()
    client = client_for()
    for _ in range(5):
        await client.get("/api/x")
    retry = int((await client.get("/api/x")).headers["retry-after"])
    clock.advance(retry * 1000)
    assert (await client.get("/api/x")).status_code == 200


async def test_policies_keep_separate_state(gateway):
    _, client_for = await gateway()
    client = client_for()
    for _ in range(5):
        await client.get("/api/x")
    assert (await client.get("/api/x")).status_code == 429
    assert (await client.get("/home")).status_code == 200


async def test_cost_weighted_policy(gateway):
    _, client_for = await gateway()
    client = client_for()
    statuses = [(await client.get("/report")).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


@pytest.mark.parametrize(("country", "server"), [("DE", "eu"), ("FR", "eu"), ("US", "default")])
async def test_geo_routing_from_trusted_cdn(gateway, country, server):
    _, client_for = await gateway()
    response = await client_for(CDN_PEER).get("/home", headers={"cf-ipcountry": country})
    assert response.headers["x-upstream"] == server


async def test_eu_zone_gets_stricter_override(gateway):
    _, client_for = await gateway()
    client = client_for(CDN_PEER)
    headers = {"cf-ipcountry": "DE", "x-forwarded-for": "198.51.100.1"}
    statuses = [(await client.get("/api/x", headers=headers)).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    response = await client.get("/api/x", headers={**headers, "x-forwarded-for": "198.51.100.2"})
    assert response.headers["x-ratelimit-limit"] == "2"
    assert response.headers["x-gateway-zone"] == "eu"


async def test_geo_headers_from_untrusted_clients_are_ignored(gateway):
    _, client_for = await gateway()
    response = await client_for(CLIENT_PEER).get("/home", headers={"cf-ipcountry": "DE"})
    assert response.headers["x-gateway-zone"] == "default"
    assert response.headers["x-upstream"] == "default"


async def test_spoofed_geo_cannot_escape_a_block(gateway):
    _, client_for = await gateway()
    response = await client_for(CLIENT_PEER).get("/home", headers={"cf-ipcountry": "KP"})
    assert response.status_code == 200  # untrusted: header ignored, so not blocked either


@pytest.mark.parametrize(("country", "status"), [("KP", 451), ("T1", 403)])
async def test_blocked_zones(gateway, upstream, country, status):
    _, client_for = await gateway()
    response = await client_for(CDN_PEER).get("/api/x", headers={"cf-ipcountry": country})
    assert response.status_code == status
    assert upstream.requests == []


@pytest.mark.parametrize("header", ["cloudfront-viewer-country", "x-vercel-ip-country"])
async def test_other_cdn_headers_route_too(gateway, header):
    _, client_for = await gateway()
    response = await client_for(CDN_PEER).get("/home", headers={header: "FR"})
    assert response.headers["x-gateway-zone"] == "eu"


async def test_forwarded_for_from_untrusted_peer_cannot_dodge_limit(gateway):
    _, client_for = await gateway()
    client = client_for(CLIENT_PEER)
    statuses = [
        (await client.get("/home", headers={"x-forwarded-for": f"1.1.1.{i}"})).status_code
        for i in range(4)
    ]
    assert statuses == [200, 200, 200, 429]


async def test_trusted_proxy_separates_real_clients(gateway):
    _, client_for = await gateway()
    client = client_for(CDN_PEER)
    for _ in range(3):
        await client.get("/home", headers={"x-forwarded-for": "198.51.100.1"})
    assert (
        await client.get("/home", headers={"x-forwarded-for": "198.51.100.1"})
    ).status_code == 429
    assert (
        await client.get("/home", headers={"x-forwarded-for": "198.51.100.2"})
    ).status_code == 200


async def test_api_keys_get_their_own_buckets(gateway):
    _, client_for = await gateway()
    client = client_for()
    for _ in range(3):
        await client.get("/home", headers={"x-api-key": "alice"})
    assert (await client.get("/home", headers={"x-api-key": "alice"})).status_code == 429
    assert (await client.get("/home", headers={"x-api-key": "bob"})).status_code == 200
    assert (await client.get("/home")).status_code == 200


async def test_ip_identity_policy_ignores_rotating_api_keys(gateway):
    _, client_for = await gateway()
    client = client_for()
    statuses = [
        (await client.post("/login", headers={"x-api-key": f"key-{i}"})).status_code
        for i in range(3)
    ]
    assert statuses == [200, 200, 429]


async def test_traffic_shaping_delays_instead_of_rejecting(gateway):
    app, client_for = await gateway()
    client = client_for()
    start = time.perf_counter()
    statuses = [(await client.post("/upload", content=b"x")).status_code for _ in range(3)]
    elapsed = time.perf_counter() - start
    assert statuses == [200, 200, 200]
    assert elapsed >= 0.28  # delays of 0, 100 and 200 ms
    assert (await client.post("/upload", content=b"x")).status_code == 429
    metrics = app.state.gateway.metrics.render().decode()
    assert 'gateway_shaping_delay_seconds_count{policy="uploads"} 2.0' in metrics


async def test_request_body_and_method_forwarded(gateway, upstream):
    _, client_for = await gateway()
    payload = b'{"name": "x"}' * 1000
    response = await client_for().put(
        "/bulk/items", content=payload, headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert upstream.requests[0].method == "PUT"
    assert upstream.requests[0].body == payload
    assert upstream.requests[0].headers["content-type"] == "application/json"


async def test_get_without_body_sends_no_body(gateway, upstream):
    _, client_for = await gateway()
    await client_for().get("/bulk")
    assert upstream.requests[0].body == b""
    assert "transfer-encoding" not in upstream.requests[0].headers


async def test_forwarding_headers(gateway, upstream):
    _, client_for = await gateway()
    await client_for(CDN_PEER).get(
        "/bulk",
        headers={
            "x-forwarded-for": "198.51.100.1",
            "connection": "keep-alive, x-hop",
            "x-hop": "secret",
            "accept": "text/plain",
        },
    )
    sent = upstream.requests[0].headers
    assert sent["x-forwarded-for"] == "198.51.100.1, 10.0.0.1"
    assert sent["x-forwarded-proto"] == "http"
    assert sent["accept"] == "text/plain"
    assert sent["host"] == upstream.default_url.removeprefix("http://")
    assert "x-hop" not in sent


async def test_response_headers_filtered_and_duplicates_kept(gateway):
    _, client_for = await gateway()
    response = await client_for().get("/bulk")
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert "proxy-authenticate" not in response.headers


async def test_cookies_never_leak_between_clients(gateway, upstream):
    _, client_for = await gateway()
    await client_for(("198.51.100.1", 1)).get("/bulk/cookie")
    await client_for(("198.51.100.2", 1)).get("/bulk")
    assert "cookie" not in upstream.requests[1].headers


async def test_compressed_bodies_pass_through_untouched(gateway):
    _, client_for = await gateway()
    response = await client_for().get("/bulk/gzip", headers={"accept-encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert response.content == b"compressed payload"  # httpx decodes on our side
    assert int(response.headers["content-length"]) == len(Upstreams.GZIP_BODY)


async def test_large_bodies_are_streamed_intact(gateway):
    _, client_for = await gateway()
    response = await client_for().get("/bulk/large")
    assert response.content == Upstreams.LARGE_BODY
    assert int(response.headers["content-length"]) == len(Upstreams.LARGE_BODY)


async def test_bodies_without_length_are_streamed(gateway):
    _, client_for = await gateway()
    response = await client_for().get("/bulk/chunked")
    assert response.content == b"one,two,three"
    assert "content-length" not in response.headers


async def test_head_keeps_upstream_content_length(gateway):
    _, client_for = await gateway()
    response = await client_for().head("/bulk/large")
    assert response.content == b""
    assert int(response.headers["content-length"]) == len(Upstreams.LARGE_BODY)


async def test_gateway_adds_no_headers_the_client_did_not_send(gateway, upstream):
    _, client_for = await gateway()
    await client_for().get("/bulk", headers={"accept-encoding": "", "user-agent": ""})
    sent = upstream.requests[0].headers
    assert sent.get("accept-encoding", "") == ""
    assert "Python" not in sent.get("user-agent", "")


async def test_head_and_options_are_proxied(gateway, upstream):
    _, client_for = await gateway()
    client = client_for()
    assert (await client.head("/bulk")).status_code == 200
    assert (await client.options("/bulk")).status_code == 200
    assert [r.method for r in upstream.requests] == ["HEAD", "OPTIONS"]


async def test_percent_encoded_path_is_preserved(gateway, upstream):
    _, client_for = await gateway()
    await client_for().get("/bulk/a%2Fb/caf%C3%A9?q=a%26b")
    assert upstream.requests[0].path == "/bulk/a%2Fb/caf%C3%A9?q=a%26b"


async def test_unmatched_route_is_unlimited(gateway, upstream):
    config = {
        "policies": [
            {
                "name": "api",
                "match": {"path_prefix": "/api"},
                "strategy": {"type": "fixed_window", "limit": 1, "window_ms": 1000},
            }
        ]
    }
    _, client_for = await gateway(config)
    client = client_for()
    statuses = [(await client.get("/public")).status_code for _ in range(10)]
    assert statuses == [200] * 10
    assert "x-ratelimit-limit" not in (await client.get("/public")).headers


async def test_upstream_connection_dropped_is_502(gateway, upstream):
    app, client_for = await gateway(upstream_url=upstream.urls["drop"])
    response = await client_for().get("/bulk")
    assert response.status_code == 502
    assert response.headers["x-ratelimit-policy"] == "bulk"
    metrics = app.state.gateway.metrics.render().decode()
    assert 'gateway_upstream_errors_total{kind="unavailable"} 1.0' in metrics


async def test_upstream_timeout_is_504(gateway):
    app, client_for = await gateway(upstream_timeout_s=0.2)
    start = time.perf_counter()
    response = await client_for().get("/bulk/slow")
    assert response.status_code == 504
    assert time.perf_counter() - start < 1.5
    metrics = app.state.gateway.metrics.render().decode()
    assert 'gateway_upstream_errors_total{kind="timeout"} 1.0' in metrics


async def test_health(gateway):
    _, client_for = await gateway()
    response = await client_for().get("/_gw/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_ready_when_redis_is_up(gateway):
    _, client_for = await gateway()
    assert (await client_for().get("/_gw/ready")).status_code == 200


async def test_metrics_endpoint(gateway):
    _, client_for = await gateway()
    client = client_for()
    await client.get("/api/x")
    for _ in range(3):
        await client.get("/home")
    await client.get("/home")
    text = (await client.get("/_gw/metrics")).text
    assert 'gateway_requests_total{outcome="allowed",policy="api",zone="default"} 1.0' in _sorted(
        text
    )
    assert (
        'gateway_requests_total{outcome="limited",policy="default",zone="default"} 1.0'
        in _sorted(text)
    )
    assert "gateway_limiter_duration_seconds_bucket" in text
    assert "gateway_upstream_duration_seconds_count 4.0" in text


async def test_check_endpoint(gateway, upstream):
    _, client_for = await gateway()
    client = client_for()
    results = [
        await client.get("/_gw/check", params={"policy": "login", "key": "u1"}) for _ in range(3)
    ]
    assert [r.status_code for r in results] == [200, 200, 429]
    assert results[0].json()["remaining"] == 1
    assert results[2].headers["retry-after"]
    assert upstream.requests == []


@pytest.mark.parametrize(
    "params", [{}, {"policy": "login"}, {"key": "k"}, {"policy": "nope", "key": "k"}]
)
async def test_check_endpoint_validation(gateway, params):
    _, client_for = await gateway()
    assert (await client_for().get("/_gw/check", params=params)).status_code == 400


async def test_check_endpoint_can_be_disabled(gateway, upstream):
    _, client_for = await gateway(enable_check_endpoint=False)
    response = await client_for().get("/_gw/check", params={"policy": "login", "key": "k"})
    # falls through to the proxy route like any other path
    assert response.headers["x-upstream"] == "default"


def _sorted(text: str) -> str:
    """Rewrite each metric's labels in sorted order so assertions don't depend on it."""

    def fix(match: re.Match) -> str:
        labels = sorted(match.group(2).split(","))
        return f"{match.group(1)}{{{','.join(labels)}}}"

    return re.sub(r"(\w+)\{([^}]*)\}", fix, text)
