# Benchmark results

Measured 2026-10-23. All numbers are from one laptop running **everything** (load
generator, nginx, gateway replicas, Redis, upstream) at once, so they understate what the
gateway does on dedicated hardware. They are reported as measured.

## Setup

| | |
|---|---|
| CPU | Intel Core i7-1360P (4 performance + 8 efficiency cores, 16 threads, mobile power limits) |
| Memory | 16 GB, Docker Desktop on Windows 11 (WSL2 VM, 16 vCPUs) |
| Gateway | Python 3.13, uvicorn + uvloop + httptools, 1 worker per container |
| Redis | 7.4, single instance, no persistence |
| Load | k6 1.0, `constant-arrival-rate`, 10,000 distinct API keys |
| Topology | k6 → nginx → 4 gateway replicas → Redis / upstream (all in Docker) |

Scenarios (`bench/load.js`):

- **check**: `GET /_gw/check?policy=bench&key=…`, i.e. the rate-limit decision only (one
  Redis round trip), no upstream.
- **proxy**: `GET /bench/item` with an API key, i.e. the full path: identify, resolve
  policy, limit, forward to the upstream, stream back.

## Results (4 replicas)

| Scenario | Offered rate | Achieved | p50 | p95 | p99 | Errors | Limiter p95 (server side) |
|---|---|---|---|---|---|---|---|
| check | 3,000/s | 3,000/s | 1.2 ms | 5.4 ms | 16.9 ms | 0% | 3.4 ms |
| check | 5,500/s | 5,500/s | 1.6 ms | 13.6 ms | 28.8 ms | 0% | 8.0 ms |
| check | 6,000/s | 5,990/s | 2.1 ms | 25.0 ms | 55.7 ms | 0.01% | |
| check | 8,000/s | 7,590/s | 65 ms | 345 ms | 752 ms | 57% | |
| proxy | 2,000/s | 2,000/s | 1.7 ms | 3.9 ms | 9.7 ms | 0% | **1.0 ms** |
| proxy | 3,500/s | 3,450/s | 18 ms | 293 ms | 474 ms | 0% | 73 ms |

"Limiter p95" is `histogram_quantile(0.95, gateway_limiter_duration_seconds)` from
Prometheus: the time a replica spends on the decision, Redis round trip included.

**Sustained capacity on this machine: about 6,000 req/s for decisions and about 3,000 req/s
for the full proxy path, at under 1% errors.** The limiter decision stays under 2 ms p95
at moderate load (1.0 ms at 2,000 req/s on the proxy path).

Past the knee (between 6k and 8k for `check`), the replicas' event loops lag behind, so
Redis replies arrive after the 100 ms client timeout even though Redis is only around 50%
busy. Those calls count as failures, the circuit breaker opens, and `/check` answers 503.
On the proxy path the gateway fails open instead, so the run above shows latency rising
but no errors.

### Single replica, isolated

One replica driven directly (no nginx), 64 concurrent connections:

| Endpoint | req/s |
|---|---|
| `/_gw/health` (framework baseline) | 8,340 |
| `/_gw/check` | 3,630 |
| full proxy path | 1,830 |

8 replicas did no better than 4: with k6 (~3.5 cores), nginx (~1.6) and Redis on the same
12-core mobile CPU, extra replicas only compete for the same cores.

## The 50,000 req/s target

**Not reached on this hardware.** Linear extrapolation from the isolated numbers gives
about 14 replicas on dedicated cores for 50k decisions/s (roughly 28 for the full proxy
path), with the load generator on separate machines. The design scales horizontally
(stateless replicas, one shared Redis round trip per request, cluster-safe hash-tagged
keys, and `tests/e2e` shows limits holding across replicas behind the load balancer), but
that extrapolation has not been measured.

## What the profiling changed

| Change | Effect (single replica, proxy path) |
|---|---|
| Starting point: httpx `AsyncClient` for upstream calls | 322 req/s, p95 535 ms |
| httpx → aiohttp. In isolation httpx fell from 1,345 to 570 req/s as concurrency rose from 1 to 64, while aiohttp went to 10,500 | 1,061 req/s |
| Release the upstream connection from an async function (a sync `BackgroundTask` cost a thread-pool hop per request) and buffer bodies ≤ 64 KiB instead of streaming them | 1,834 req/s, p95 48 ms |
| Cache per-strategy Lua arguments and Prometheus label children; plain Starlette routes on hot paths | smaller gains on `/check` (3,410 → 3,630) |

Benchmark-harness problems found along the way, each of which first looked like a gateway
problem:

- **k6 metric cardinality.** Every distinct URL became its own time series (200k+), and k6
  burned about 6.7 cores on bookkeeping while the gateways sat near idle. Fixed with a
  constant `name` tag.
- **nginx ejecting replicas.** The default `max_fails=1 fail_timeout=10s` removed a
  replica for 10 s after a single slow connect, cascading load onto the others. Now
  `max_fails=5 fail_timeout=2s`.
- **Keep-alive race.** nginx kept idle upstream connections 60 s, uvicorn closed them at
  30 s, which gave "connection reset by peer". The nginx side is now 20 s.
- **Stale DNS after scaling.** nginx kept the old replica set when Docker DNS answered
  SERVFAIL. Restart nginx after changing `GATEWAY_REPLICAS`.

## Reproducing

```bash
GATEWAY_REPLICAS=4 docker compose up -d --wait
docker compose restart nginx          # pick up the replica set

docker compose --profile bench run --rm -e SCENARIO=check -e RATE=3000 k6 run /bench/load.js
docker compose --profile bench run --rm -e SCENARIO=proxy -e RATE=2000 k6 run /bench/load.js

# One replica, bypassing nginx (bench/probe.js, closed loop with 64 VUs):
docker compose --profile bench run --rm -e URL=http://<replica-ip>:8000/_gw/check?policy=bench\&key=a \
  k6 run /bench/probe.js
```

In Git Bash on Windows, prefix these with `MSYS_NO_PATHCONV=1` so `/bench/load.js` is not
rewritten into a Windows path. Other knobs: `DURATION`, `VUS`, `MAX_VUS`, `KEYS`.
