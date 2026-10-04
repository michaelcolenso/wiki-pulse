/**
 * WikiPulse Worker — serves the dashboard shell + live JSON data entirely
 * from KV. No local server, no tunnel, no port contention with other
 * services on this host. The Python collectors (collect.py, analyze.py,
 * realtime_monitor.py) push updated JSON into the same KV namespace via
 * the Cloudflare REST API after each refresh cycle — see
 * ../push_to_kv.py.
 */

export interface Env {
  DATA: KVNamespace;
}

const SECURITY_HEADERS: Record<string, string> = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "X-XSS-Protection": "1; mode=block",
  "Referrer-Policy": "no-referrer",
  "Permissions-Policy": "interest-cohort=()",
  "Content-Security-Policy":
    "default-src 'self' 'unsafe-inline' 'unsafe-eval' https://wikispike.xyz; img-src 'self' data: https:; connect-src 'self' https:; font-src 'self';",
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
};

const ROUTES: Record<string, { key: string; contentType: string; cache: string }> = {
  "/": { key: "index.html", contentType: "text/html; charset=utf-8", cache: "public, max-age=300" },
  "/index.html": { key: "index.html", contentType: "text/html; charset=utf-8", cache: "public, max-age=300" },
  "/realtime.json": { key: "realtime.json", contentType: "application/json; charset=utf-8", cache: "no-cache" },
  "/spikes.json": { key: "spikes.json", contentType: "application/json; charset=utf-8", cache: "no-cache" },
  "/history.json": { key: "history.json", contentType: "application/json; charset=utf-8", cache: "no-cache" },
  "/context-data.json": { key: "context-data.json", contentType: "application/json; charset=utf-8", cache: "no-cache" },
};

function withHeaders(body: BodyInit | null, status: number, contentType: string, cache: string): Response {
  const headers = new Headers(SECURITY_HEADERS);
  headers.set("Content-Type", contentType);
  headers.set("Cache-Control", cache);
  return new Response(body, { status, headers });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return withHeaders("Method Not Allowed", 405, "text/plain; charset=utf-8", "no-store");
    }

    const url = new URL(request.url);
    const route = ROUTES[url.pathname];

    if (!route) {
      return withHeaders("Not Found", 404, "text/plain; charset=utf-8", "no-store");
    }

    const value = await env.DATA.get(route.key, "text");
    if (value === null) {
      return withHeaders("Data not yet published", 503, "text/plain; charset=utf-8", "no-store");
    }

    if (request.method === "HEAD") {
      return withHeaders(null, 200, route.contentType, route.cache);
    }
    return withHeaders(value, 200, route.contentType, route.cache);
  },
};
