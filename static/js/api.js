/* JSON requests to this Clip Maker server, for the Autopilot screens
   (design §15, task 12.2).

     import { api, ApiError } from "./api.js";
     const counts = await api.get("/api/autopilot/counts");
     await api.post("/api/autopilot/posts/abc/approve");

   Requests go to this server only: paths must start with /api/. A request
   that fails rejects with an ApiError carrying the HTTP status (0 when the
   server didn't answer), the server's message, and for a 422 the problems
   by field, [{field, message}], so a form can show each one next to its
   control. */

export class ApiError extends Error {
  constructor(status, message, problems = [], body = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.problems = problems;
    this.body = body;
  }
}

const FALLBACK = {
  401: "Sign in again to continue.",
  403: "That isn't allowed.",
  404: "It isn't there any more. Reload to see the latest.",
  409: "Something changed meanwhile. Reload to see the latest.",
  413: "That's too large.",
};

function problemsOf(body) {
  const detail = body && body.detail;
  if (!Array.isArray(detail)) return [];
  return detail.filter(d => d && typeof d === "object").map(d => ({
    // Our own 422s name `field`; FastAPI's validation errors give `loc`.
    field: d.field || (Array.isArray(d.loc) ? d.loc.filter(p => p !== "body").join(".") : "") || "body",
    message: d.message || d.msg || "This isn't valid.",
  }));
}

function messageOf(status, body, problems) {
  const detail = body && body.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (problems.length) return problems.map(p => p.message).join(" ");
  return FALLBACK[status] || `The request failed (${status}).`;
}

/** One request. `body` is sent as JSON, or as multipart when it's a
    FormData (a file upload); `query` values that are null, undefined or ""
    are left out. Returns the parsed JSON reply, or null for an empty one. */
export async function request(method, path, { body, query, signal } = {}) {
  if (typeof path !== "string" || !path.startsWith("/api/")) {
    throw new Error(`api: the path must start with /api/ (got ${path})`);
  }
  let url = path;
  if (query) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
    }
    const qs = params.toString();
    if (qs) url += (url.includes("?") ? "&" : "?") + qs;
  }
  const init = { method, credentials: "same-origin", headers: { Accept: "application/json" }, signal };
  if (body instanceof FormData) {
    init.body = body;                 // the browser sets the multipart boundary
  } else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new ApiError(0, "Clip Maker isn't answering. Check that it's still running, then try again.");
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!response.ok) {
    const problems = problemsOf(data);
    throw new ApiError(response.status, messageOf(response.status, data, problems), problems, data);
  }
  return data;
}

export const api = {
  get: (path, query, options) => request("GET", path, { ...options, query }),
  post: (path, body, options) => request("POST", path, { ...options, body }),
  patch: (path, body, options) => request("PATCH", path, { ...options, body }),
  del: (path, query, options) => request("DELETE", path, { ...options, query }),
};
