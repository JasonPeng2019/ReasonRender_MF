/**
 * Shared data models and validators for the storefront API.
 * Every handler depends on these shapes — read this file before auditing any handler.
 */

const USER_ROLES = ["customer", "seller", "admin", "support"];
const ORDER_STATUSES = ["pending", "paid", "shipped", "delivered", "cancelled", "refunded"];
const PRODUCT_CATEGORIES = ["electronics", "books", "clothing", "home", "toys", "grocery"];
const REVIEW_FLAGS = ["spam", "offensive", "off-topic", "fake"];

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const SKU_RE = /^[A-Z]{3}-\d{4,8}$/;
const CURRENCY_RE = /^(USD|EUR|GBP|INR|JPY)$/;
const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?Z?)?$/;

/**
 * User record shape:
 * { id, email, displayName, role, addresses: [Address], createdAt, passwordHash,
 *   loyaltyPoints, marketingOptIn, suspended }
 */
function validateUser(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["user payload must be an object"] };
  if (typeof input.email !== "string" || !EMAIL_RE.test(input.email)) {
    errors.push("email must be a valid address");
  }
  if (typeof input.displayName !== "string" || input.displayName.trim().length < 2) {
    errors.push("displayName must be at least 2 characters");
  }
  if (input.displayName && input.displayName.length > 64) {
    errors.push("displayName must be at most 64 characters");
  }
  if (input.role !== undefined && !USER_ROLES.includes(input.role)) {
    errors.push(`role must be one of ${USER_ROLES.join(", ")}`);
  }
  if (input.addresses !== undefined) {
    if (!Array.isArray(input.addresses)) {
      errors.push("addresses must be an array");
    } else {
      input.addresses.forEach((addr, i) => {
        const r = validateAddress(addr);
        if (!r.ok) errors.push(`addresses[${i}]: ${r.errors.join("; ")}`);
      });
    }
  }
  if (input.loyaltyPoints !== undefined) {
    if (!Number.isInteger(input.loyaltyPoints) || input.loyaltyPoints < 0) {
      errors.push("loyaltyPoints must be a non-negative integer");
    }
  }
  if (input.marketingOptIn !== undefined && typeof input.marketingOptIn !== "boolean") {
    errors.push("marketingOptIn must be a boolean");
  }
  return { ok: errors.length === 0, errors };
}

/**
 * Address shape: { line1, line2?, city, region?, postalCode, country }
 * country must be ISO-3166 alpha-2 (2 uppercase letters).
 */
function validateAddress(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["address must be an object"] };
  for (const field of ["line1", "city", "postalCode", "country"]) {
    if (typeof input[field] !== "string" || input[field].trim() === "") {
      errors.push(`${field} is required`);
    }
  }
  if (typeof input.country === "string" && !/^[A-Z]{2}$/.test(input.country)) {
    errors.push("country must be ISO-3166 alpha-2");
  }
  if (input.postalCode && String(input.postalCode).length > 12) {
    errors.push("postalCode too long");
  }
  return { ok: errors.length === 0, errors };
}

/**
 * Order shape:
 * { id, userId, items: [{ sku, quantity, unitPriceCents }], currency, status,
 *   shippingAddress: Address, placedAt, totalCents, couponCode? }
 * Invariant: totalCents MUST equal sum(items.quantity * items.unitPriceCents) minus coupon discount.
 */
function validateOrder(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["order payload must be an object"] };
  if (typeof input.userId !== "string" || input.userId.length === 0) {
    errors.push("userId is required");
  }
  if (!Array.isArray(input.items) || input.items.length === 0) {
    errors.push("items must be a non-empty array");
  } else {
    input.items.forEach((item, i) => {
      if (typeof item.sku !== "string" || !SKU_RE.test(item.sku)) {
        errors.push(`items[${i}].sku must match ${SKU_RE}`);
      }
      if (!Number.isInteger(item.quantity) || item.quantity < 1 || item.quantity > 999) {
        errors.push(`items[${i}].quantity must be an integer in [1, 999]`);
      }
      if (!Number.isInteger(item.unitPriceCents) || item.unitPriceCents < 0) {
        errors.push(`items[${i}].unitPriceCents must be a non-negative integer`);
      }
    });
  }
  if (typeof input.currency !== "string" || !CURRENCY_RE.test(input.currency)) {
    errors.push("currency must be one of USD, EUR, GBP, INR, JPY");
  }
  if (input.status !== undefined && !ORDER_STATUSES.includes(input.status)) {
    errors.push(`status must be one of ${ORDER_STATUSES.join(", ")}`);
  }
  if (input.shippingAddress !== undefined) {
    const r = validateAddress(input.shippingAddress);
    if (!r.ok) errors.push(`shippingAddress: ${r.errors.join("; ")}`);
  }
  if (input.placedAt !== undefined && !ISO_DATE_RE.test(String(input.placedAt))) {
    errors.push("placedAt must be an ISO-8601 date");
  }
  return { ok: errors.length === 0, errors };
}

/** Compute the order total in cents from items; coupon handling is the caller's job. */
function computeOrderTotalCents(items) {
  if (!Array.isArray(items)) return 0;
  return items.reduce((sum, item) => {
    const q = Number.isInteger(item.quantity) ? item.quantity : 0;
    const p = Number.isInteger(item.unitPriceCents) ? item.unitPriceCents : 0;
    return sum + q * p;
  }, 0);
}

/**
 * Product shape:
 * { id, sku, title, description?, category, priceCents, stock, sellerId,
 *   attributes?: object, active, createdAt }
 */
function validateProduct(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["product payload must be an object"] };
  if (typeof input.sku !== "string" || !SKU_RE.test(input.sku)) {
    errors.push("sku must look like ABC-1234");
  }
  if (typeof input.title !== "string" || input.title.trim().length < 3 || input.title.length > 140) {
    errors.push("title must be 3-140 characters");
  }
  if (input.description !== undefined && typeof input.description !== "string") {
    errors.push("description must be a string");
  }
  if (input.description && input.description.length > 5000) {
    errors.push("description must be at most 5000 characters");
  }
  if (!PRODUCT_CATEGORIES.includes(input.category)) {
    errors.push(`category must be one of ${PRODUCT_CATEGORIES.join(", ")}`);
  }
  if (!Number.isInteger(input.priceCents) || input.priceCents < 0 || input.priceCents > 100000000) {
    errors.push("priceCents must be an integer in [0, 100000000]");
  }
  if (!Number.isInteger(input.stock) || input.stock < 0) {
    errors.push("stock must be a non-negative integer");
  }
  if (typeof input.sellerId !== "string" || input.sellerId.length === 0) {
    errors.push("sellerId is required");
  }
  if (input.active !== undefined && typeof input.active !== "boolean") {
    errors.push("active must be a boolean");
  }
  return { ok: errors.length === 0, errors };
}

/**
 * Review shape:
 * { id, productId, userId, rating, title?, body?, verifiedPurchase, flags?: [..], createdAt }
 * rating is an integer 1..5. body is at most 2000 chars. flags entries must be known.
 */
function validateReview(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["review payload must be an object"] };
  if (typeof input.productId !== "string" || input.productId.length === 0) {
    errors.push("productId is required");
  }
  if (typeof input.userId !== "string" || input.userId.length === 0) {
    errors.push("userId is required");
  }
  if (!Number.isInteger(input.rating) || input.rating < 1 || input.rating > 5) {
    errors.push("rating must be an integer in [1, 5]");
  }
  if (input.title !== undefined && (typeof input.title !== "string" || input.title.length > 120)) {
    errors.push("title must be a string of at most 120 characters");
  }
  if (input.body !== undefined && (typeof input.body !== "string" || input.body.length > 2000)) {
    errors.push("body must be a string of at most 2000 characters");
  }
  if (input.verifiedPurchase !== undefined && typeof input.verifiedPurchase !== "boolean") {
    errors.push("verifiedPurchase must be a boolean");
  }
  if (input.flags !== undefined) {
    if (!Array.isArray(input.flags)) {
      errors.push("flags must be an array");
    } else {
      for (const f of input.flags) {
        if (!REVIEW_FLAGS.includes(f)) errors.push(`unknown flag: ${f}`);
      }
    }
  }
  return { ok: errors.length === 0, errors };
}

/** Coupon shape: { code, percentOff (1..90), expiresAt, minTotalCents? } */
function validateCoupon(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["coupon must be an object"] };
  if (typeof input.code !== "string" || !/^[A-Z0-9_-]{4,24}$/.test(input.code)) {
    errors.push("code must be 4-24 chars of A-Z 0-9 _ -");
  }
  if (!Number.isInteger(input.percentOff) || input.percentOff < 1 || input.percentOff > 90) {
    errors.push("percentOff must be an integer in [1, 90]");
  }
  if (!ISO_DATE_RE.test(String(input.expiresAt))) {
    errors.push("expiresAt must be an ISO-8601 date");
  }
  if (input.minTotalCents !== undefined && (!Number.isInteger(input.minTotalCents) || input.minTotalCents < 0)) {
    errors.push("minTotalCents must be a non-negative integer");
  }
  return { ok: errors.length === 0, errors };
}

/** Apply a coupon to a total. Assumes the coupon has already been validated AND checked for expiry. */
function applyCoupon(totalCents, coupon) {
  const discounted = Math.round(totalCents * (1 - coupon.percentOff / 100));
  return Math.max(0, discounted);
}

/** Public projection of a user record — strips credentials and internal fields. */
function publicUser(user) {
  return {
    id: user.id,
    displayName: user.displayName,
    role: user.role,
    loyaltyPoints: user.loyaltyPoints,
    createdAt: user.createdAt,
  };
}

/** Public projection of an order for the owning customer. */
function publicOrder(order) {
  return {
    id: order.id,
    items: order.items,
    currency: order.currency,
    status: order.status,
    totalCents: order.totalCents,
    placedAt: order.placedAt,
    shippingAddress: order.shippingAddress,
  };
}

/**
 * Extended domain validators — invoices, shipments, subscriptions, tickets,
 * inventory adjustments, payouts, and webhooks. Handlers cross-reference these
 * exactly as they do the core validators above.
 */

const INVOICE_STATUSES = ["draft", "open", "paid", "void", "uncollectible"];
const SHIPMENT_CARRIERS = ["ups", "fedex", "usps", "dhl", "ontrac"];
const SUBSCRIPTION_INTERVALS = ["day", "week", "month", "quarter", "year"];
const TICKET_PRIORITIES = ["low", "normal", "high", "urgent"];
const PAYOUT_METHODS = ["ach", "wire", "paypal", "check"];
const WEBHOOK_EVENTS = [
  "order.created", "order.paid", "order.shipped", "order.refunded",
  "product.created", "product.updated", "review.flagged", "user.suspended",
];

/**
 * Invoice shape:
 * { id, orderId, customerId, lineItems: [{ description, quantityUnits, unitAmountCents, taxRateBps }],
 *   currency, status, dueAt, issuedAt, subtotalCents, taxCents, totalCents, notes? }
 * Invariant: totalCents === subtotalCents + taxCents; each tax line uses taxRateBps (basis points).
 */
function validateInvoice(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["invoice payload must be an object"] };
  if (typeof input.orderId !== "string" || input.orderId.length === 0) errors.push("orderId is required");
  if (typeof input.customerId !== "string" || input.customerId.length === 0) errors.push("customerId is required");
  if (!Array.isArray(input.lineItems) || input.lineItems.length === 0) {
    errors.push("lineItems must be a non-empty array");
  } else {
    input.lineItems.forEach((li, i) => {
      if (typeof li.description !== "string" || li.description.trim().length < 1) {
        errors.push(`lineItems[${i}].description is required`);
      }
      if (!Number.isInteger(li.quantityUnits) || li.quantityUnits < 1) {
        errors.push(`lineItems[${i}].quantityUnits must be a positive integer`);
      }
      if (!Number.isInteger(li.unitAmountCents) || li.unitAmountCents < 0) {
        errors.push(`lineItems[${i}].unitAmountCents must be a non-negative integer`);
      }
      if (!Number.isInteger(li.taxRateBps) || li.taxRateBps < 0 || li.taxRateBps > 10000) {
        errors.push(`lineItems[${i}].taxRateBps must be an integer in [0, 10000] basis points`);
      }
    });
  }
  if (input.status !== undefined && !INVOICE_STATUSES.includes(input.status)) {
    errors.push(`status must be one of ${INVOICE_STATUSES.join(", ")}`);
  }
  if (input.dueAt !== undefined && !ISO_DATE_RE.test(String(input.dueAt))) {
    errors.push("dueAt must be an ISO-8601 date");
  }
  return { ok: errors.length === 0, errors };
}

/** Compute an invoice's subtotal, tax, and total in cents from its line items. */
function computeInvoiceTotals(lineItems) {
  let subtotal = 0;
  let tax = 0;
  if (Array.isArray(lineItems)) {
    for (const li of lineItems) {
      const q = Number.isInteger(li.quantityUnits) ? li.quantityUnits : 0;
      const u = Number.isInteger(li.unitAmountCents) ? li.unitAmountCents : 0;
      const lineSubtotal = q * u;
      const bps = Number.isInteger(li.taxRateBps) ? li.taxRateBps : 0;
      subtotal += lineSubtotal;
      tax += Math.round((lineSubtotal * bps) / 10000);
    }
  }
  return { subtotalCents: subtotal, taxCents: tax, totalCents: subtotal + tax };
}

/**
 * Shipment shape:
 * { id, orderId, carrier, trackingNumber, weightGrams, dimensionsCm: {l,w,h},
 *   status, shippedAt?, deliveredAt?, insuredValueCents? }
 */
function validateShipment(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["shipment payload must be an object"] };
  if (typeof input.orderId !== "string" || input.orderId.length === 0) errors.push("orderId is required");
  if (!SHIPMENT_CARRIERS.includes(input.carrier)) {
    errors.push(`carrier must be one of ${SHIPMENT_CARRIERS.join(", ")}`);
  }
  if (typeof input.trackingNumber !== "string" || !/^[A-Z0-9]{8,40}$/.test(input.trackingNumber)) {
    errors.push("trackingNumber must be 8-40 uppercase alphanumerics");
  }
  if (!Number.isInteger(input.weightGrams) || input.weightGrams <= 0 || input.weightGrams > 50000000) {
    errors.push("weightGrams must be a positive integer up to 50000000");
  }
  if (input.dimensionsCm !== undefined) {
    const d = input.dimensionsCm;
    if (!d || typeof d !== "object" || ["l", "w", "h"].some((k) => !Number.isFinite(d[k]) || d[k] <= 0)) {
      errors.push("dimensionsCm must have positive numeric l, w, h");
    }
  }
  if (input.insuredValueCents !== undefined && (!Number.isInteger(input.insuredValueCents) || input.insuredValueCents < 0)) {
    errors.push("insuredValueCents must be a non-negative integer");
  }
  return { ok: errors.length === 0, errors };
}

/** Estimate a billable shipping weight (max of actual vs dimensional weight). */
function billableWeightGrams(weightGrams, dimensionsCm) {
  if (!dimensionsCm) return weightGrams;
  const { l, w, h } = dimensionsCm;
  const dimensional = Math.round(((l || 0) * (w || 0) * (h || 0)) / 5) * 1000 / 1000;
  const dimGrams = Math.round(dimensional * 200);
  return Math.max(Number.isInteger(weightGrams) ? weightGrams : 0, dimGrams);
}

/**
 * Subscription shape:
 * { id, customerId, productId, interval, intervalCount, priceCents, currency,
 *   status, startedAt, currentPeriodEnd, cancelAtPeriodEnd, trialEndsAt? }
 */
function validateSubscription(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["subscription payload must be an object"] };
  if (typeof input.customerId !== "string" || input.customerId.length === 0) errors.push("customerId is required");
  if (typeof input.productId !== "string" || input.productId.length === 0) errors.push("productId is required");
  if (!SUBSCRIPTION_INTERVALS.includes(input.interval)) {
    errors.push(`interval must be one of ${SUBSCRIPTION_INTERVALS.join(", ")}`);
  }
  if (!Number.isInteger(input.intervalCount) || input.intervalCount < 1 || input.intervalCount > 52) {
    errors.push("intervalCount must be an integer in [1, 52]");
  }
  if (!Number.isInteger(input.priceCents) || input.priceCents < 0) {
    errors.push("priceCents must be a non-negative integer");
  }
  if (typeof input.currency !== "string" || !CURRENCY_RE.test(input.currency)) {
    errors.push("currency must be one of USD, EUR, GBP, INR, JPY");
  }
  if (input.cancelAtPeriodEnd !== undefined && typeof input.cancelAtPeriodEnd !== "boolean") {
    errors.push("cancelAtPeriodEnd must be a boolean");
  }
  return { ok: errors.length === 0, errors };
}

/** Advance a subscription period end by its interval; returns a new ISO date string. */
function nextPeriodEnd(startIso, interval, intervalCount) {
  const d = new Date(startIso);
  if (Number.isNaN(d.getTime())) return null;
  const n = Number.isInteger(intervalCount) ? intervalCount : 1;
  switch (interval) {
    case "day": d.setUTCDate(d.getUTCDate() + n); break;
    case "week": d.setUTCDate(d.getUTCDate() + 7 * n); break;
    case "month": d.setUTCMonth(d.getUTCMonth() + n); break;
    case "quarter": d.setUTCMonth(d.getUTCMonth() + 3 * n); break;
    case "year": d.setUTCFullYear(d.getUTCFullYear() + n); break;
    default: return null;
  }
  return d.toISOString();
}

/**
 * Support ticket shape:
 * { id, requesterId, subject, body, priority, assigneeId?, tags?: [], status, createdAt }
 */
function validateTicket(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["ticket payload must be an object"] };
  if (typeof input.requesterId !== "string" || input.requesterId.length === 0) errors.push("requesterId is required");
  if (typeof input.subject !== "string" || input.subject.trim().length < 3 || input.subject.length > 160) {
    errors.push("subject must be 3-160 characters");
  }
  if (typeof input.body !== "string" || input.body.trim().length < 1 || input.body.length > 10000) {
    errors.push("body must be 1-10000 characters");
  }
  if (!TICKET_PRIORITIES.includes(input.priority)) {
    errors.push(`priority must be one of ${TICKET_PRIORITIES.join(", ")}`);
  }
  if (input.tags !== undefined) {
    if (!Array.isArray(input.tags) || input.tags.some((t) => typeof t !== "string")) {
      errors.push("tags must be an array of strings");
    } else if (input.tags.length > 20) {
      errors.push("at most 20 tags");
    }
  }
  return { ok: errors.length === 0, errors };
}

/** Payout shape: { id, sellerId, amountCents, currency, method, scheduledAt, memo? } */
function validatePayout(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["payout payload must be an object"] };
  if (typeof input.sellerId !== "string" || input.sellerId.length === 0) errors.push("sellerId is required");
  if (!Number.isInteger(input.amountCents) || input.amountCents <= 0) {
    errors.push("amountCents must be a positive integer");
  }
  if (typeof input.currency !== "string" || !CURRENCY_RE.test(input.currency)) {
    errors.push("currency must be one of USD, EUR, GBP, INR, JPY");
  }
  if (!PAYOUT_METHODS.includes(input.method)) {
    errors.push(`method must be one of ${PAYOUT_METHODS.join(", ")}`);
  }
  if (input.memo !== undefined && (typeof input.memo !== "string" || input.memo.length > 140)) {
    errors.push("memo must be a string of at most 140 characters");
  }
  return { ok: errors.length === 0, errors };
}

/** Webhook subscription shape: { id, url, events: [..], secret, active } */
function validateWebhook(input) {
  const errors = [];
  if (!input || typeof input !== "object") return { ok: false, errors: ["webhook payload must be an object"] };
  if (typeof input.url !== "string" || !/^https:\/\/.+/.test(input.url)) {
    errors.push("url must be an https URL");
  }
  if (!Array.isArray(input.events) || input.events.length === 0) {
    errors.push("events must be a non-empty array");
  } else {
    for (const e of input.events) {
      if (!WEBHOOK_EVENTS.includes(e)) errors.push(`unknown webhook event: ${e}`);
    }
  }
  if (typeof input.secret !== "string" || input.secret.length < 16) {
    errors.push("secret must be at least 16 characters");
  }
  if (input.active !== undefined && typeof input.active !== "boolean") {
    errors.push("active must be a boolean");
  }
  return { ok: errors.length === 0, errors };
}

/** Public projection of an invoice for the billed customer. */
function publicInvoice(inv) {
  return {
    id: inv.id, orderId: inv.orderId, status: inv.status,
    currency: inv.currency, totalCents: inv.totalCents, dueAt: inv.dueAt, issuedAt: inv.issuedAt,
  };
}

/** Public projection of a shipment for order tracking. */
function publicShipment(s) {
  return {
    id: s.id, carrier: s.carrier, trackingNumber: s.trackingNumber,
    status: s.status, shippedAt: s.shippedAt, deliveredAt: s.deliveredAt,
  };
}

module.exports = {
  USER_ROLES, ORDER_STATUSES, PRODUCT_CATEGORIES, REVIEW_FLAGS,
  INVOICE_STATUSES, SHIPMENT_CARRIERS, SUBSCRIPTION_INTERVALS, TICKET_PRIORITIES,
  PAYOUT_METHODS, WEBHOOK_EVENTS,
  EMAIL_RE, SKU_RE, CURRENCY_RE, ISO_DATE_RE,
  validateUser, validateAddress, validateOrder, validateProduct, validateReview, validateCoupon,
  validateInvoice, validateShipment, validateSubscription, validateTicket, validatePayout, validateWebhook,
  computeOrderTotalCents, applyCoupon, computeInvoiceTotals, billableWeightGrams, nextPeriodEnd,
  publicUser, publicOrder, publicInvoice, publicShipment,
};
