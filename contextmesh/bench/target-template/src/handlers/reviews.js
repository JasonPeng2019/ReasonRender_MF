/**
 * /reviews routes: create, list, flag, moderate.
 */

const { validateReview, REVIEW_FLAGS } = require("../models");
const { newId, getById, insert, update, list, parsePagination, paginate, sanitizeText, audit } = require("../utils");
const { requireAuth, requireRole, requireJsonBody, rateLimit, sendData, sendError, asyncHandler } = require("../middleware");

function register(app) {
  // POST /reviews — customer reviews a product
  app.post(
    "/reviews",
    requireAuth,
    rateLimit({ max: 10 }),
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const body = { ...req.body, userId: req.user.id };
      const check = validateReview(body);
      if (!check.ok) {
        return sendError(res, 400, "validation", "invalid review payload", check.errors);
      }
      const product = getById("products", body.productId);
      if (!product) return sendError(res, 404, "no_product", "cannot review a product that does not exist");
      const dup = list("reviews", (r) => r.productId === body.productId && r.userId === req.user.id);
      if (dup.length > 0) {
        return sendError(res, 409, "duplicate", "you already reviewed this product");
      }
      const bought = list("orders", (o) => o.userId === req.user.id && o.items.some((i) => i.sku === product.sku));
      const review = insert("reviews", {
        id: newId("rev"),
        productId: body.productId,
        userId: req.user.id,
        rating: body.rating,
        title: sanitizeText(body.title ?? ""),
        body: sanitizeText(body.body ?? ""),
        verifiedPurchase: bought.length > 0,
        flags: [],
        hidden: false,
        createdAt: new Date().toISOString(),
      });
      audit("review.create", req.user.id, review.id, { productId: body.productId });
      return sendData(res, review, 201);
    }),
  );

  // GET /reviews?productId=... — list visible reviews for a product
  app.get(
    "/reviews",
    asyncHandler(async (req, res) => {
      const { productId, minRating } = req.query;
      if (!productId) return sendError(res, 400, "missing_param", "productId is required");
      const min = minRating !== undefined ? Number.parseInt(minRating, 10) : null;
      const rows = list("reviews", (r) => {
        if (r.productId !== productId) return false;
        if (r.hidden) return false;
        if (min !== null && r.rating < min) return false;
        return true;
      });
      return sendData(res, paginate(rows, parsePagination(req.query)));
    }),
  );

  // POST /reviews/:id/flag — any authed user flags a review
  app.post(
    "/reviews/:id/flag",
    requireAuth,
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const review = getById("reviews", req.params.id);
      if (!review) return sendError(res, 404, "not_found", "no such review");
      const flag = req.body.flag;
      const flags = [...review.flags, flag];
      const updated = update("reviews", review.id, { flags });
      audit("review.flag", req.user.id, review.id, { flag });
      if (flags.length >= 3) {
        update("reviews", review.id, { hidden: true });
      }
      return sendData(res, updated);
    }),
  );

  // POST /reviews/:id/moderate — support/admin hide or unhide
  app.post(
    "/reviews/:id/moderate",
    requireAuth,
    requireRole("admin", "support"),
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const review = getById("reviews", req.params.id);
      if (!review) return sendError(res, 404, "not_found", "no such review");
      const updated = update("reviews", review.id, { hidden: req.body.hidden === true, flags: [] });
      audit("review.moderate", req.user.id, review.id, { hidden: req.body.hidden === true });
      return sendData(res, updated);
    }),
  );
}

module.exports = { register };
