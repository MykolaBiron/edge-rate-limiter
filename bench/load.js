// Throughput/latency benchmark against the compose stack.
//
//   docker compose --profile bench run --rm k6 run /bench/load.js
//   docker compose --profile bench run --rm -e SCENARIO=proxy -e RATE=20000 k6 run /bench/load.js
//
// SCENARIO=check  -> /_gw/check: limiter decision only (Redis round trip, no upstream)
// SCENARIO=proxy  -> /bench/...: full path, limiter + proxy to the upstream
// Each virtual client uses its own API key, so the load is spread over many buckets
// the way real traffic is, instead of hammering a single Redis key.
import http from "k6/http";
import { check } from "k6";

const TARGET = __ENV.TARGET || "http://localhost:8080";
const SCENARIO = __ENV.SCENARIO || "check";
const RATE = parseInt(__ENV.RATE || "10000", 10);
const DURATION = __ENV.DURATION || "30s";
const KEYS = parseInt(__ENV.KEYS || "10000", 10);

export const options = {
  discardResponseBodies: true,
  scenarios: {
    load: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: parseInt(__ENV.VUS || "400", 10),
      maxVUs: parseInt(__ENV.MAX_VUS || "2000", 10),
    },
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
  thresholds: {
    http_req_failed: ["rate<0.01"],
  },
};

export default function () {
  const key = `bench-${Math.floor(Math.random() * KEYS)}`;
  // A fixed `name` tag: otherwise every distinct URL becomes its own metric series,
  // and k6 spends more CPU on bookkeeping than the gateway spends on requests.
  const res =
    SCENARIO === "proxy"
      ? http.get(`${TARGET}/bench/item`, { headers: { "x-api-key": key }, tags: { name: "proxy" } })
      : http.get(`${TARGET}/_gw/check?policy=bench&key=${key}`, { tags: { name: "check" } });
  check(res, { "status is 200": (r) => r.status === 200 });
}
