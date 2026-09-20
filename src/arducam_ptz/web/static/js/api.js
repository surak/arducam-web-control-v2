/*
 * Thin API client for arducam-ptz.
 *
 * Everything the UI does goes through here, and nothing here knows about cameras,
 * servos or focus rails - it is HTTP and JSON. Keeping it that way is what lets the
 * same backend serve this page, the `arducam` CLI and anything else.
 *
 * Error handling is deliberately blunt: the server answers with
 * `{"error": {"code": ..., "detail": ...}}` (see api/errors.py), so the client surfaces
 * `code` and `detail` and lets the caller decide. The old page treated every response
 * as a success unless the JSON said `succ == False`, which is how it silently ignored
 * hardware failures.
 */

export const API = "/api/v1";

export class ApiError extends Error {
  constructor(status, code, detail, payload) {
    super(detail || `${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.payload = payload;
  }
}

/**
 * Optional shared token.
 *
 * The backend can require a token (server.token in the config). A browser cannot set
 * headers on an <img>, so the token is accepted as a query parameter too; this reads it
 * from the page URL once and keeps it for the session, then appends it to every request
 * that browsers make on their own (the MJPEG stream, thumbnails, downloads).
 */
const TOKEN_KEY = "arducam.token";

function initToken() {
  const fromUrl = new URLSearchParams(location.search).get("token");
  if (fromUrl) {
    sessionStorage.setItem(TOKEN_KEY, fromUrl);
    // Drop it from the address bar: URLs get copied into chats and screenshots.
    const clean = new URL(location.href);
    clean.searchParams.delete("token");
    history.replaceState(null, "", clean);
  }
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

export const token = initToken();

/** Append the token to a URL that the browser will fetch by itself. */
export function withToken(url) {
  if (!token) return url;
  const u = new URL(url, location.origin);
  u.searchParams.set("token", token);
  return u.pathname + u.search + u.hash;
}

/**
 * Turn FastAPI's validation payload into something a human can act on.
 *
 * `{"detail": [{"loc": ["body", "region", "width"], "msg": "Field required"}]}` becomes
 * "region.width: Field required". Returns null when the payload is not that shape, so
 * the caller can fall back to the raw body.
 */
function describeValidation(payload) {
  const items = payload && Array.isArray(payload.detail) ? payload.detail : null;
  if (!items || items.length === 0) return null;
  return items
    .map((item) => {
      const where = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
      const what = item.msg || item.type || "invalid";
      return where ? `${where}: ${what}` : what;
    })
    .join("; ");
}

async function request(path, { method = "GET", body, timeoutMs = 15000, signal } = {}) {
  const headers = {};
  if (token) headers["X-API-Token"] = token;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  if (signal) signal.addEventListener("abort", () => controller.abort(), { once: true });

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (cause) {
    clearTimeout(timer);
    throw new ApiError(0, "network", "cannot reach the server", cause);
  }
  clearTimeout(timer);

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const error = (payload && payload.error) || {};
    // Everything this API raises itself arrives as {"error": {code, detail}}. A
    // *validation* failure, however, is FastAPI's own shape: {"detail": [ {loc, msg} ]}
    // - a list. Falling through to `text.slice()` for that is how a field-name typo
    // reached the user as the raw JSON blob "missing field required input" instead of a
    // sentence naming the field.
    const detail = error.detail || describeValidation(payload) || text.slice(0, 200);
    throw new ApiError(response.status, error.code || "http_error", detail, payload);
  }
  return payload;
}

export const api = {
  version: () => request("/api/version"),
  health: () => request(`${API}/health`),
  state: () => request(`${API}/state`),

  ptz: {
    get: () => request(`${API}/ptz`),
    move: (pan, tilt, opts = {}) =>
      request(`${API}/ptz/move`, { method: "POST", body: { pan, tilt, ...opts } }),
    nudge: (pan, tilt) => request(`${API}/ptz/nudge`, { method: "POST", body: { pan, tilt } }),
    center: () => request(`${API}/ptz/center`, { method: "POST" }),
    release: () => request(`${API}/ptz/release`, { method: "POST" }),
  },

  focus: {
    get: () => request(`${API}/focus`),
    set: (position) => request(`${API}/focus`, { method: "POST", body: { position } }),
    nudge: (steps) => request(`${API}/focus/nudge`, { method: "POST", body: { steps } }),
    autofocus: (region) =>
      request(`${API}/focus/autofocus`, { method: "POST", body: region ? { region } : {} }),
    job: (id) => request(`${API}/focus/autofocus/${id}`),
    cancel: (id) => request(`${API}/focus/autofocus/${id}/cancel`, { method: "POST" }),
  },

  captures: {
    list: (limit = 60) => request(`${API}/captures?limit=${limit}`),
    take: () => request(`${API}/captures`, { method: "POST", timeoutMs: 30000 }),
    remove: (name) => request(`${API}/captures/${encodeURIComponent(name)}`, { method: "DELETE" }),
    url: (name, maxDim) =>
      withToken(`${API}/captures/${encodeURIComponent(name)}${maxDim ? `?max_dim=${maxDim}` : ""}`),
  },

  /**
   * URL for the live preview <img>.
   *
   * No resize parameters on purpose: the preview is encoded once, by the hub, and every
   * viewer gets that same stream (that is what keeps one slow phone from dragging the
   * frame rate down for everybody). The size therefore comes from the server config,
   * not from the client - see docs/CONFIGURATION.md.
   */
  streamUrl: () => withToken(`${API}/stream.mjpg`),

  /** One frame as a JPEG, for a pull-to-refresh style screenshot. */
  snapshotUrl: () => withToken(`${API}/stream/snapshot.jpg`),
};

/**
 * Poll a background job until it finishes.
 *
 * The autofocus sweep takes seconds; the server answers with 202 and a job id rather
 * than holding a request open (see api/routes/focus.py for why). This turns that back
 * into something the UI can await.
 */
export async function pollJob(id, { intervalMs = 350, onUpdate, signal } = {}) {
  for (;;) {
    if (signal?.aborted) throw new ApiError(0, "aborted", "cancelled");
    const job = await api.focus.job(id);
    if (onUpdate) onUpdate(job);
    if (job.status !== "running") return job;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}
