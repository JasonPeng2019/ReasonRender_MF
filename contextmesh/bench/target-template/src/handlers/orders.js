/**
 * /orders routes: place, list, fetch, cancel, refund; coupon application.
 */

const { validateOrder, validateCoupon, computeOrderTotalCents, applyCoupon, publicOrder, ORDER_STATUSES } = require("../models");
const { newId, getById, insert, update, list, parsePagination, paginate, isExpired, audit } = require("../utils");
const { requireAuth, requireRole, requireJsonBody, rateLimit, sendData, sendError, asyncHandler } = require("../middleware");

function register(app) {
  // POST /orders — place an order
  app.post(
    "/orders",
    requireAuth,
    rateLimit({ max: 20 }),
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const body = { ...req.body, userId: req.user.id };
      const check = validateOrder(body);
      if (!check.ok) {
        return sendError(res, 400, "validation", "invalid order payload", check.errors);
      }
      let totalCents = computeOrderTotalCents(body.items);
      if (body.couponCode) {
        const coupon = getById("coupons", body.couponCode);
        if (!coupon) {
          return sendError(res, 400, "bad_coupon", "unknown coupon code");
        }
        totalCents = applyCoupon(totalCents, coupon);
      }
      const order = insert("orders", {
        id: newId("ord"),
        userId: req.user.id,
        items: body.items,
        currency: body.currency,
        status: "pending",
        shippingAddress: body.shippingAddress ?? req.user.addresses[0],
        placedAt: new Date().toISOString(),
        totalCents,
        couponCode: body.couponCode ?? null,
      });
      audit("order.create", req.user.id, order.id, { totalCents });
      return sendData(res, publicOrder(order), 201);
    }),
  );

  // GET /orders — list own orders (admin sees all with ?all=1)
  app.get(
    "/orders",
    requireAuth,
    asyncHandler(async (req, res) => {
      const wantAll = req.query.all === "1";
      const rows = list("orders", (o) => (wantAll ? true : o.userId === req.user.id));
      const page = paginate(rows, parsePagination(req.query));
      return sendData(res, { ...page, items: page.items.map(publicOrder) });
    }),
  );

  // GET /orders/:id
  app.get(
    "/orders/:id",
    requireAuth,
    asyncHandler(async (req, res) => {
      const order = getById("orders", req.params.id);
      if (!order) return sendError(res, 404, "not_found", "no such order");
      if (order.userId !== req.user.id && req.user.role !== "admin" && req.user.role !== "support") {
        return sendError(res, 403, "forbidden", "not your order");
      }
      return sendData(res, publicOrder(order));
    }),
  );

  // POST /orders/:id/cancel — customer cancels a not-yet-shipped order
  app.post(
    "/orders/:id/cancel",
    requireAuth,
    asyncHandler(async (req, res) => {
      const order = getById("orders", req.params.id);
      if (!order) return sendError(res, 404, "not_found", "no such order");
      if (order.userId !== req.user.id) {
        return sendError(res, 403, "forbidden", "not your order");
      }
      const updated = update("orders", order.id, { status: "cancelled" });
      audit("order.cancel", req.user.id, order.id);
      return sendData(res, publicOrder(updated));
    }),
  );

  // POST /orders/:id/refund — support/admin refund a delivered order
  app.post(
    "/orders/:id/refund",
    requireAuth,
    requireRole("admin", "support"),
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const order = getById("orders", req.params.id);
      if (!order) return sendError(res, 404, "not_found", "no such order");
      const amount = req.body.amountCents ?? order.totalCents;
      const updated = update("orders", order.id, { status: "refunded", refundedCents: amount });
      audit("order.refund", req.user.id, order.id, { amountCents: amount });
      return sendData(res, publicOrder(updated));
    }),
  );

  // POST /orders/coupons — admin creates a coupon
  app.post(
    "/orders/coupons",
    requireAuth,
    requireRole("admin"),
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const check = validateCoupon(req.body);
      if (!check.ok) return sendError(res, 400, "validation", "invalid coupon", check.errors);
      if (isExpired(req.body.expiresAt)) {
        return sendError(res, 400, "expired", "coupon expiry must be in the future");
      }
      insert("coupons", { ...req.body, id: req.body.code });
      audit("coupon.create", req.user.id, req.body.code);
      return sendData(res, { code: req.body.code }, 201);
    }),
  );
}

module.exports = { register };
