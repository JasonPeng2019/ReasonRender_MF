/**
 * /products routes: catalog CRUD for sellers, browsing for everyone.
 */

const { validateProduct, PRODUCT_CATEGORIES } = require("../models");
const { newId, getById, insert, update, remove, list, parsePagination, paginate, sanitizeText, audit } = require("../utils");
const { requireAuth, requireRole, requireJsonBody, sendData, sendError, asyncHandler } = require("../middleware");

function register(app) {
  // POST /products — seller creates a listing
  app.post(
    "/products",
    requireAuth,
    requireRole("seller", "admin"),
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const body = { ...req.body, sellerId: req.user.id };
      const check = validateProduct(body);
      if (!check.ok) {
        return sendError(res, 400, "validation", "invalid product payload", check.errors);
      }
      const skuTaken = list("products", (p) => p.sku === body.sku).length > 0;
      if (skuTaken) return sendError(res, 409, "sku_taken", "sku already exists");
      const product = insert("products", {
        id: newId("prd"),
        sku: body.sku,
        title: sanitizeText(body.title),
        description: sanitizeText(body.description ?? ""),
        category: body.category,
        priceCents: body.priceCents,
        stock: body.stock,
        sellerId: req.user.id,
        attributes: body.attributes ?? {},
        active: true,
        createdAt: new Date().toISOString(),
      });
      audit("product.create", req.user.id, product.id);
      return sendData(res, product, 201);
    }),
  );

  // GET /products — browse with optional category/q filters
  app.get(
    "/products",
    asyncHandler(async (req, res) => {
      const { category, q, minPrice, maxPrice } = req.query;
      if (category !== undefined && !PRODUCT_CATEGORIES.includes(category)) {
        return sendError(res, 400, "bad_category", "unknown category");
      }
      const min = minPrice !== undefined ? Number(minPrice) : null;
      const max = maxPrice !== undefined ? Number(maxPrice) : null;
      const needle = typeof q === "string" ? q.toLowerCase() : null;
      const rows = list("products", (p) => {
        if (!p.active) return false;
        if (category && p.category !== category) return false;
        if (min !== null && p.priceCents < min) return false;
        if (max !== null && p.priceCents > max) return false;
        if (needle && !p.title.toLowerCase().includes(needle)) return false;
        return true;
      });
      return sendData(res, paginate(rows, parsePagination(req.query)));
    }),
  );

  // GET /products/:id
  app.get(
    "/products/:id",
    asyncHandler(async (req, res) => {
      const product = getById("products", req.params.id);
      if (!product || !product.active) return sendError(res, 404, "not_found", "no such product");
      return sendData(res, product);
    }),
  );

  // PATCH /products/:id — seller edits own listing
  app.patch(
    "/products/:id",
    requireAuth,
    requireRole("seller", "admin"),
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const product = getById("products", req.params.id);
      if (!product) return sendError(res, 404, "not_found", "no such product");
      if (product.sellerId !== req.user.id && req.user.role !== "admin") {
        return sendError(res, 403, "forbidden", "not your listing");
      }
      const patch = {};
      for (const field of ["title", "description", "priceCents", "stock", "active", "attributes", "category"]) {
        if (req.body[field] !== undefined) patch[field] = req.body[field];
      }
      const updated = update("products", product.id, patch);
      audit("product.update", req.user.id, product.id, { fields: Object.keys(patch) });
      return sendData(res, updated);
    }),
  );

  // DELETE /products/:id — seller retires own listing
  app.delete(
    "/products/:id",
    requireAuth,
    requireRole("seller", "admin"),
    asyncHandler(async (req, res) => {
      const product = getById("products", req.params.id);
      if (!product) return sendError(res, 404, "not_found", "no such product");
      remove("products", product.id);
      audit("product.delete", req.user.id, product.id);
      return sendData(res, { deleted: true });
    }),
  );

  // POST /products/:id/restock — seller adjusts stock
  app.post(
    "/products/:id/restock",
    requireAuth,
    requireRole("seller", "admin"),
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const product = getById("products", req.params.id);
      if (!product) return sendError(res, 404, "not_found", "no such product");
      const delta = req.body.delta;
      const updated = update("products", product.id, { stock: product.stock + delta });
      audit("product.restock", req.user.id, product.id, { delta });
      return sendData(res, updated);
    }),
  );
}

module.exports = { register };
