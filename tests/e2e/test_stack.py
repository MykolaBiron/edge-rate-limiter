"""End-to-end tests against the running docker compose stack.

    docker compose up -d --wait
    pytest -m e2e

Every test uses fresh API keys, so the suite can be re-run without waiting for windows
to expire. Policies come from the shipped policies.yaml.
"""

import asyncio
import math
import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.e2e

BASE_URL = os.environ.get("E2E_URL", "http://localhost:8080")
PROMETHEUS_URL = os.environ.get("E2E_PROMETHEUS_URL", "http://localhost:9090")


@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as c:
        try:
            await c.get("/_gw/health")
        except httpx.HTTPError as exc:
            pytest.skip(f"stack not reachable at {BASE_URL}: {exc}")
        yield c


def fresh_key() -> dict[str, str]:
    return {"x-api-key": f"e2e-{uuid.uuid4()}"}


async def test_health_and_readiness(client):
    assert (await client.get("/_gw/health")).json() == {"status": "ok"}
    assert (await client.get("/_gw/ready")).json() == {"status": "ready"}


async def test_request_is_proxied_with_headers(client):
    response = await client.get("/api/items?x=1", headers=fresh_key())
    assert response.status_code == 200
    assert response.json()["path"] == "/api/items"
    assert response.json()["query"] == "x=1"
    assert response.headers["x-ratelimit-policy"] == "api"
    assert response.headers["x-ratelimit-limit"] == "100"


async def test_limit_holds_across_replicas_behind_the_load_balancer(client):
    """The point of the shared Redis state: 150 concurrent requests, spread by nginx over
    every replica, still admit exactly the 100-token burst."""
    headers = fresh_key()
    start = time.perf_counter()
    responses = await asyncio.gather(*(client.get("/api/x", headers=headers) for _ in range(150)))
    elapsed = time.perf_counter() - start
    statuses = [r.status_code for r in responses]
    # The bucket refills at 20 tokens/s while the burst is in flight.
    assert 100 <= statuses.count(200) <= 100 + math.ceil(elapsed * 20)
    assert statuses.count(200) + statuses.count(429) == 150
    limited = next(r for r in responses if r.status_code == 429)
    assert int(limited.headers["retry-after"]) >= 1


@pytest.mark.parametrize(
    ("headers", "region", "zone"),
    [
        ({"cf-ipcountry": "DE"}, "eu", "eu"),
        ({"cloudfront-viewer-country": "FR"}, "eu", "eu"),
        ({"cf-ipcountry": "US"}, "us", "na"),
        ({}, "us", "default"),
    ],
)
async def test_geo_routing(client, headers, region, zone):
    response = await client.get("/api/geo", headers={**headers, **fresh_key()})
    assert response.headers["x-upstream-region"] == region
    assert response.headers["x-gateway-zone"] == zone


async def test_eu_zone_override(client):
    response = await client.get("/api/x", headers={"cf-ipcountry": "DE", **fresh_key()})
    assert response.headers["x-ratelimit-limit"] == "50"


@pytest.mark.parametrize(("country", "status"), [("KP", 451), ("IR", 451), ("T1", 403)])
async def test_blocked_zones(client, country, status):
    response = await client.get("/api/x", headers={"cf-ipcountry": country})
    assert response.status_code == status


async def test_traffic_shaping_smooths_bursts(client):
    headers = fresh_key()
    start = time.perf_counter()
    for _ in range(3):
        assert (await client.post("/upload", content=b"x", headers=headers)).status_code == 200
    # leak_rate 5/s: the third request waits ~400 ms for its slot
    assert time.perf_counter() - start >= 0.35


async def test_internal_metrics_are_not_exposed_at_the_edge(client):
    assert (await client.get("/_gw/metrics")).status_code == 404


async def test_check_endpoint(client):
    key = str(uuid.uuid4())
    response = await client.get("/_gw/check", params={"policy": "auth-login", "key": key})
    assert response.status_code == 200
    assert response.json()["remaining"] == 4


async def test_prometheus_scrapes_every_replica():
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            response = await c.get(f"{PROMETHEUS_URL}/api/v1/targets")
        except httpx.HTTPError:
            pytest.skip("prometheus not reachable")
    targets = response.json()["data"]["activeTargets"]
    assert targets, "no gateway targets discovered"
    assert all(t["health"] == "up" for t in targets)
