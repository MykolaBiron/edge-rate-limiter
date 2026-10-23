# Edge Rate Limiter

A horizontally scalable rate-limiting and traffic-shaping gateway. It sits in front of an
upstream service as a reverse proxy, decides every request with **one atomic Redis Lua
call**, and shares limits across any number of stateless replicas.

- **5 strategies**: token bucket, leaky bucket (with traffic shaping), fixed window,
  sliding window (counter or exact log) and composite (all-or-nothing combinations)
- **Atomic and race-free**: all algorithms run inside a single Lua script on Redis's clock,
  with no read-modify-write races and no clock skew between replicas
- **Geo-aware**: parses CDN country headers (Cloudflare, CloudFront, Fastly, Vercel) and
  maps them to compliance zones that can block (451/403), tighten limits or route to a
  regional upstream
- **Production behaviour**: fail-open or fail-closed, a circuit breaker for Redis outages,
  spoof-resistant client identification, Prometheus metrics and a Grafana dashboard
- **507 tests**: unit, integration against real Redis, differential tests against reference
  models, concurrency/race tests, failure injection, load and end-to-end

## Architecture

```
                 ┌──────────────┐
  clients ──────▶│    nginx     │  least_conn, keep-alive pool
                 └──────┬───────┘
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
     ┌─────────┐   ┌─────────┐   ┌─────────┐      stateless; scale with
     │ gateway │   │ gateway │   │ gateway │ ...  GATEWAY_REPLICAS=N
     └────┬────┘   └────┬────┘   └────┬────┘
          │  EVALSHA    │             │          one round trip per request
          ▼             ▼             ▼
     ┌─────────────────────────────────────┐
     │                Redis                │  limiter state, all keys with TTL
     └─────────────────────────────────────┘
          │ allowed requests, by zone
          ├──────────────▶ upstream-us (default)
          └──────────────▶ upstream-eu (eu zone)

  Prometheus scrapes every replica  ──▶  Grafana dashboard
```

Per request: **identify** the client (API key, or IP via trusted proxies) → **parse geo**
(only from trusted peers) → **resolve** the policy (longest path prefix + method) and zone
→ **decide** in Redis → reject (429 / 451 / 403), **shape** (delay), or **proxy**.

## Quick start

```bash
docker compose up -d --wait          # nginx :8080, Prometheus :9090, Grafana :3000

curl -i localhost:8080/api/hello                        # 200 + X-RateLimit-* headers
curl -i -H "CF-IPCountry: DE" localhost:8080/api/hello  # routed to upstream-eu, limit 50
curl -i -H "CF-IPCountry: KP" localhost:8080/api/hello  # 451 Unavailable For Legal Reasons
for i in $(seq 7); do curl -s -o /dev/null -w "%{http_code} " -X POST localhost:8080/auth/login; done
# 200 200 200 200 200 429 429
```

Grafana's home dashboard (no login) shows throughput by outcome, decision latency
percentiles, Redis errors and circuit state.

## Strategies

| Strategy | State per client | Bursts | Best for |
|---|---|---|---|
| `token_bucket` | hash (tokens, ts) | up to `capacity`, refills at `refill_rate`/s | general APIs |
| `leaky_bucket` | one timestamp | queue of `capacity`, drains at `leak_rate`/s | smoothing; with `shape: true` requests are **delayed** rather than rejected |
| `fixed_window` | hash (start, count) | up to 2× at window edges | cheap quotas (per hour/day) |
| `sliding_window` | hash (3 fields) | weighted estimate, no edge burst | per-minute limits at scale |
| `sliding_window` + `precise: true` | sorted set, 1 entry/request | exact | low limits where exactness matters (login) |
| `composite` | its parts' | every part must allow | burst + sustained quota |

A **composite** check is all-or-nothing: each part evaluates first, and state is written
only if every part allows. A request rejected by the hourly quota does not drain the
burst bucket. Responses carry `X-RateLimit-Limit/Remaining/Reset/Policy`, plus
`Retry-After` on 429. `Retry-After` is exact: waiting that long is both enough and
necessary (tested for every strategy).

## Configuration

Policies live in [policies.yaml](policies.yaml) and are validated at startup (unknown
fields, bad limits, cost above the limit, duplicate names, a country in two zones and
unknown zone overrides all fail fast). A simplified excerpt:

```yaml
zones:
  eu:        {countries: [DE, FR, ...], upstream: http://upstream-eu:8001}
  embargoed: {countries: [CU, IR, KP, SY], action: block, status: 451}

policies:
  - name: auth-login
    match: {path_prefix: /auth/login, methods: [POST]}
    identity: ip              # ignore API keys, so rotating them doesn't help
    strategy: {type: sliding_window, limit: 5, window_ms: 60000, precise: true}

  - name: api
    match: {path_prefix: /api}
    strategy:
      type: composite
      parts:
        - {type: token_bucket, capacity: 100, refill_rate: 20}
        - {type: fixed_window, limit: 10000, window_ms: 3600000}
    zone_overrides:
      eu: {type: token_bucket, capacity: 50, refill_rate: 10}
```

Policy options: `cost` (weight per request), `shape` (hold requests for the leaky-bucket
delay), `identity` (`client` = API key else IP, or `ip`). Zone entries may be countries
(`US`) or country-regions (`US-CA`).

Runtime settings are environment variables (prefix `GW_`):

| Variable | Default | |
|---|---|---|
| `GW_REDIS_URL` | `redis://localhost:6379/0` | |
| `GW_REDIS_TIMEOUT_MS` | `100` | includes event-loop lag, so keep some headroom |
| `GW_UPSTREAM_URL` | `http://localhost:8001` | default upstream (zones can override) |
| `GW_FAIL_MODE` | `open` | `open`: pass traffic marked `X-RateLimit-Status: degraded`; `closed`: 503 |
| `GW_TRUSTED_PROXIES` | `127.0.0.1/32,::1/128` | CIDRs allowed to set forwarding and geo headers |
| `GW_IPV6_PREFIX` | `64` | IPv6 clients are limited per /64 |
| `GW_POLICIES_FILE` | `policies.yaml` | |
| `GW_ENABLE_CHECK_ENDPOINT` | `true` | `GET /_gw/check?policy=&key=`, decision only |

Internal endpoints: `/_gw/health`, `/_gw/ready` (pings Redis), `/_gw/metrics` (blocked at
nginx and scraped per replica), `/_gw/check`.

## Design notes

- **One round trip, one script.** Every algorithm is a Lua function, and a dispatcher runs
  any list of them ([src/gateway/limiters/lua/](src/gateway/limiters/lua/)), so composite
  limits cost the same single `EVALSHA` as simple ones. redis-py reloads the script
  transparently on `NOSCRIPT` (Redis restart or `SCRIPT FLUSH`).
- **Redis's clock, not the replicas'.** The script reads `TIME`, so replicas with skewed
  clocks still agree. Tests inject a clock through an optional script argument instead of
  sleeping.
- **Cluster-ready keys.** `rl:{policy:client}:i:algorithm`: the hash tag keeps a client's
  components in one slot, and the algorithm in the key name means a config change can't
  read state written in another format.
- **Denials are cheap.** Rejected requests write nothing (except pruning the sliding log),
  so an attacker hammering a limit doesn't amplify Redis writes.
- **Spoof resistance.** `X-Forwarded-For` is walked right to left and stops at the first
  untrusted hop. CDN headers (`CF-Connecting-IP`, `CF-IPCountry`, ...) are ignored unless
  the direct peer is a trusted proxy. API keys are hashed (BLAKE2b) before they reach
  Redis.
- **Failure handling.** Redis errors trigger fail-open or fail-closed. The circuit breaker
  trips on a failure *ratio* over a rolling window, not on consecutive failures: at
  thousands of requests per second a latency blip produces runs of timeouts with Redis
  healthy, and opening the circuit then would turn a few slow requests into a second of
  rejected traffic.
- **Proxy correctness.** Hop-by-hop and `Connection`-listed headers are stripped both ways,
  repeated headers such as `Set-Cookie` survive, percent-encoding is forwarded byte for
  byte, compressed bodies pass through undecoded, and the shared HTTP session never stores
  cookies (one client's session would otherwise leak to the next).

> **Demo caveat:** in the compose stack nginx plays the CDN edge and passes client-sent geo
> headers through, so you can try zones with `curl -H "CF-IPCountry: DE"`. Behind a real
> CDN, trust only the CDN's published IP ranges (`GW_TRUSTED_PROXIES`) or have the edge
> overwrite those headers.

## Performance

Measured on one laptop (i7-1360P) running load generator, nginx, 4 replicas and Redis
together. Full method and numbers are in [bench/RESULTS.md](bench/RESULTS.md).

| Scenario | Sustained, < 1% errors | p95 end-to-end | Limiter decision p95 |
|---|---|---|---|
| Decision only (`/_gw/check`) | ~6,000 req/s | 5.4 ms at 3k/s | 3.4 ms at 3k/s |
| Full proxy path | ~3,000 req/s | 3.9 ms at 2k/s | **1.0 ms** at 2k/s |

The 50k req/s target was **not** reached on this hardware. One replica handles ~3.6k
decisions/s on its own, so 50k would need about 14 replicas on dedicated cores plus
separate load generators. The design scales that way, but it hasn't been measured. The
profiling log in RESULTS.md shows how the proxy path got 5.7× faster (httpx → aiohttp,
removing a thread-pool hop per request).

## Tests

```bash
docker run -d -p 6379:6379 redis:7.4-alpine   # tests use DB 15 on 127.0.0.1:6379
uv sync
uv run pytest                  # unit + integration (486 tests, ~1 min)
uv run pytest -m load -s       # in-process throughput/latency (6)
uv run pytest -m e2e           # against `docker compose up` (15)
uv run pytest --cov            # 98% line coverage
```

| Suite | What it covers |
|---|---|
| `tests/unit` (183) | identity and spoofing, geo parsing, policy matching and validation, strategy models, circuit breaker, headers |
| `tests/integration/limiters` | every algorithm against real Redis with an injected clock: bursts, refill, boundaries, cost, TTLs, `Retry-After` exactness, script flush, eviction, corrupt state |
| `.../test_reference_models.py` | **differential tests**: 150-step random traffic through Lua and independent Python models must agree on every decision |
| `tests/integration/test_concurrency.py` | 5 "replicas" with separate pools racing on one key never admit more than the limit, for every strategy |
| `tests/integration/test_gateway.py` | the full app: proxying, geo routing, blocking, shaping, identity, headers, streaming, metrics |
| `tests/integration/test_failure_modes.py` | Redis down, slow (`CLIENT PAUSE`) and recovering, circuit breaker, fail open/closed, upstream drop/timeout |
| `tests/load`, `tests/e2e` | throughput, and the composed stack (including limits holding across replicas behind nginx) |

## Layout

```
src/gateway/
  main.py           app factory, lifespan, routes
  service.py        request pipeline (identify → resolve → limit → shape → proxy)
  limiters/         strategy models, engine, lua/ scripts
  policy.py         YAML config models and resolver
  identity.py       client IP / API key resolution
  geo.py            CDN geo headers
  proxy.py          aiohttp upstream proxy
  redis_client.py   connection pool and circuit breaker
  metrics.py        Prometheus metrics
upstream/app.py     demo echo service
docker/             Dockerfile, nginx, Prometheus, Grafana
bench/              k6 scripts and RESULTS.md
```

Scaling: `GATEWAY_REPLICAS=8 docker compose up -d --wait && docker compose restart nginx`
(nginx can miss new replicas if Docker DNS hiccups; the restart makes it re-resolve).
