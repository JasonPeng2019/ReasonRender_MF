/**
 * Shared HTTP middleware: auth, role gates, rate limiting, body handling,
 * and the error envelope every handler must use.
 *
 * Response envelope contract:
 *   success: res.json({ ok: true, data })
 *   failure: res.status(code).json({ ok: false, error: { code, message, details? } })
 */

const { getById, safeEqual, audit } = require("./utils");

/** Map of api tokens -> userId. Populated at login; standing in for sessions. */
const tokenTable = new Map();

/** Issue a bearer token for a user (called from the users handler on login). */
function issueToken(userId, token) {
  tokenTable.set(token, userId);
  return token;
}

function revokeToken(token) {
  tokenTable.delete(token);
}

/**
 * requireAuth: reads `Authorization: Bearer <token>`, resolves the user, and
 * attaches it as req.user. 401 on missing/unknown token, 403 if suspended.
 */
function requireAuth(req, res, next) {
  const header = req.headers["authorization"] ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : null;
  if (!token) {
    return sendError(res, 401, "auth_required", "missing bearer token");
  }
  let userId = null;
  for (const [t, uid] of tokenTable.entries()) {
    if (safeEqual(t, token)) {
      userId = uid;
      break;
    }
  }
  if (!userId) {
    return sendError(res, 401, "auth_invalid", "unknown token");
  }
  const user = getById("users", userId);
  if (!user) {
    return sendError(res, 401, "auth_invalid", "token user no longer exists");
  }
  if (user.suspended) {
    return sendError(res, 403, "account_suspended", "account is suspended");
  }
  req.user = user;
  return next();
}

/**
 * requireRole(...roles): 403 unless req.user.role is in the allow-list.
 * MUST run after requireAuth.
 */
function requireRole(...roles) {
  return (req, res, next) => {
    if (!req.user) {
      return sendError(res, 500, "middleware_order", "requireRole used without requireAuth");
    }
    if (!roles.includes(req.user.role)) {
      audit("authz.denied", req.user.id, req.path, { needed: roles });
      return sendError(res, 403, "forbidden", `requires role: ${roles.join(" or ")}`);
    }
    return next();
  };
}

/**
 * Fixed-window rate limiter per user-or-ip. 60 requests/minute default.
 * Windows reset lazily; memory is bounded by eviction of stale windows.
 */
const rlWindows = new Map();
function rateLimit(opts) {
  const max = opts?.max ?? 60;
  const windowMs = opts?.windowMs ?? 60000;
  return (req, res, next) => {
    const key = req.user?.id ?? req.ip ?? "unknown";
    const now = Date.now();
    let w = rlWindows.get(key);
    if (!w || now - w.start >= windowMs) {
      w = { start: now, count: 0 };
      rlWindows.set(key, w);
    }
    w.count += 1;
    if (w.count > max) {
      res.setHeader("Retry-After", Math.ceil((w.start + windowMs - now) / 1000));
      return sendError(res, 429, "rate_limited", "too many requests");
    }
    if (rlWindows.size > 10000) {
      for (const [k, win] of rlWindows.entries()) {
        if (now - win.start >= windowMs) rlWindows.delete(k);
      }
    }
    return next();
  };
}

/**
 * requireJsonBody: 415 unless content-type is application/json; 400 when the
 * body is missing or not an object. Attaches the parsed body as req.body
 * (the framework already parsed it; this normalizes edge cases).
 */
function requireJsonBody(req, res, next) {
  const ct = String(req.headers["content-type"] ?? "");
  if (!ct.toLowerCase().startsWith("application/json")) {
    return sendError(res, 415, "unsupported_media_type", "send application/json");
  }
  if (req.body === undefined || req.body === null || typeof req.body !== "object" || Array.isArray(req.body)) {
    return sendError(res, 400, "bad_body", "body must be a JSON object");
  }
  return next();
}

/** Uniform error envelope. Never leak stack traces or internal messages. */
function sendError(res, status, code, message, details) {
  const payload = { ok: false, error: { code, message } };
  if (details !== undefined) payload.error.details = details;
  return res.status(status).json(payload);
}

/** Uniform success envelope. */
function sendData(res, data, status) {
  return res.status(status ?? 200).json({ ok: true, data });
}

/**
 * asyncHandler: wraps an async route so rejections hit the error middleware
 * instead of crashing the process.
 */
function asyncHandler(fn) {
  return (req, res, next) => {
    Promise.resolve(fn(req, res, next)).catch(next);
  };
}

/** Terminal error middleware — logs and returns a scrubbed 500. */
function errorMiddleware(err, req, res, _next) {
  audit("error.unhandled", req.user?.id, req.path, { message: String(err?.message ?? err) });
  if (res.headersSent) return;
  sendError(res, 500, "internal", "internal server error");
}

/**
 * Extended middleware: CORS, request-id tagging, idempotency, ETag/conditional
 * responses, ownership guards, pagination-header emission, and a validation
 * runner. Handlers compose these alongside the core middleware above.
 */

const { recallIdempotent, rememberIdempotent } = require("./utils");

/** Attach a stable request id (from header or generated) as req.id + response header. */
function requestId(req, res, next) {
  const incoming = req.headers["x-request-id"];
  const id = typeof incoming === "string" && /^[\w-]{8,64}$/.test(incoming) ? incoming : `req_${Math.random().toString(16).slice(2, 14)}`;
  req.id = id;
  res.setHeader("X-Request-Id", id);
  return next();
}

/**
 * CORS for the storefront's browser clients. Reflects an allow-listed origin;
 * never uses a wildcard together with credentials.
 */
function cors(opts) {
  const allow = new Set(opts?.origins ?? []);
  const methods = opts?.methods ?? ["GET", "POST", "PATCH", "DELETE", "OPTIONS"];
  return (req, res, next) => {
    const origin = req.headers["origin"];
    if (typeof origin === "string" && allow.has(origin)) {
      res.setHeader("Access-Control-Allow-Origin", origin);
      res.setHeader("Vary", "Origin");
      res.setHeader("Access-Control-Allow-Credentials", "true");
    }
    res.setHeader("Access-Control-Allow-Methods", methods.join(", "));
    res.setHeader("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key, X-Request-Id");
    if (req.method === "OPTIONS") {
      res.statusCode = 204;
      return res.end();
    }
    return next();
  };
}

/**
 * Idempotency: for unsafe methods, replay the stored response when the same
 * Idempotency-Key is seen again. MUST run after requireAuth so the key is
 * scoped to the caller.
 */
function idempotency(req, res, next) {
  if (req.method === "GET" || req.method === "HEAD") return next();
  const raw = req.headers["idempotency-key"];
  if (typeof raw !== "string" || raw.length < 8) return next();
  const key = `${req.user?.id ?? "anon"}:${req.method}:${req.path}:${raw}`;
  const cached = recallIdempotent(key);
  if (cached) {
    res.setHeader("Idempotent-Replayed", "true");
    return res.status(cached.status).json(cached.body);
  }
  const origJson = res.json.bind(res);
  res.json = (body) => {
    rememberIdempotent(key, { status: res.statusCode || 200, body });
    return origJson(body);
  };
  return next();
}

/** Emit a weak ETag for a JSON body and short-circuit with 304 on a match. */
function withETag(req, res, computeBody) {
  const body = computeBody();
  const json = JSON.stringify(body);
  let hash = 0;
  for (let i = 0; i < json.length; i += 1) {
    hash = (hash * 31 + json.charCodeAt(i)) | 0;
  }
  const etag = `W/"${(hash >>> 0).toString(16)}"`;
  res.setHeader("ETag", etag);
  if (req.headers["if-none-match"] === etag) {
    res.statusCode = 304;
    return res.end();
  }
  return res.status(200).json({ ok: true, data: body });
}

/**
 * requireOwnership(loadFn, ownerField): 404 if the resource is missing, 403 if
 * req.user is neither the owner nor an admin. Attaches the resource as req.resource.
 */
function requireOwnership(loadFn, ownerField) {
  return (req, res, next) => {
    const resource = loadFn(req);
    if (!resource) {
      return sendError(res, 404, "not_found", "resource does not exist");
    }
    const isOwner = resource[ownerField] === req.user?.id;
    const isAdmin = req.user?.role === "admin";
    if (!isOwner && !isAdmin) {
      return sendError(res, 403, "forbidden", "you do not own this resource");
    }
    req.resource = resource;
    return next();
  };
}

/** Emit RFC-5988-style pagination headers from a { total, offset, limit } page. */
function paginationHeaders(res, { total, offset, limit }, basePath) {
  res.setHeader("X-Total-Count", String(total));
  const links = [];
  if (offset + limit < total) {
    links.push(`<${basePath}?offset=${offset + limit}&limit=${limit}>; rel="next"`);
  }
  if (offset > 0) {
    links.push(`<${basePath}?offset=${Math.max(0, offset - limit)}&limit=${limit}>; rel="prev"`);
  }
  if (links.length) res.setHeader("Link", links.join(", "));
}

/**
 * validateBody(validator): run a models.js-style validator over req.body and
 * respond 400 with the collected errors before the handler runs.
 */
function validateBody(validator) {
  return (req, res, next) => {
    const result = validator(req.body);
    if (!result.ok) {
      return sendError(res, 400, "validation", "request body failed validation", result.errors);
    }
    return next();
  };
}

/** Enforce a maximum JSON body size (in bytes) using the Content-Length header. */
function maxBodyBytes(limit) {
  return (req, res, next) => {
    const len = Number.parseInt(req.headers["content-length"] ?? "0", 10);
    if (Number.isFinite(len) && len > limit) {
      return sendError(res, 413, "payload_too_large", `body exceeds ${limit} bytes`);
    }
    return next();
  };
}

module.exports = {
  issueToken, revokeToken, requireAuth, requireRole, rateLimit, requireJsonBody,
  requestId, cors, idempotency, withETag, requireOwnership, paginationHeaders,
  validateBody, maxBodyBytes,
  sendError, sendData, asyncHandler, errorMiddleware, tokenTable,
};
