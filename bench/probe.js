import http from "k6/http";
export const options = { discardResponseBodies: true, vus: 64, duration: __ENV.DURATION || "10s",
  summaryTrendStats: ["med", "p(95)", "p(99)"] };
export default function () { http.get(__ENV.URL); }
