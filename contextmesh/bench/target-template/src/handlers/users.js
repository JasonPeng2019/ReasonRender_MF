/**
 * /users routes: registration, login, profile, address book.
 */

const crypto = require("crypto");
const { validateUser, validateAddress, publicUser } = require("../models");
const { store, newId, getById, insert, update, list, sanitizeText, hashPassword, verifyPassword, audit } = require("../utils");
const { issueToken, revokeToken, requireAuth, requireJsonBody, sendData, sendError, asyncHandler } = require("../middleware");

function register(app) {
  // POST /users — create an account
  app.post(
    "/users",
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const body = req.body;
      const check = validateUser(body);
      if (!check.ok) {
        return sendError(res, 400, "validation", "invalid user payload", check.errors);
      }
      const emailTaken = list("users", (u) => u.email === body.email).length > 0;
      if (emailTaken) {
        return sendError(res, 409, "email_taken", "an account with this email already exists");
      }
      const user = insert("users", {
        id: newId("usr"),
        email: body.email,
        displayName: sanitizeText(body.displayName),
        role: "customer",
        addresses: [],
        passwordHash: hashPassword(body.password),
        loyaltyPoints: 0,
        marketingOptIn: body.marketingOptIn === true,
        suspended: false,
        createdAt: new Date().toISOString(),
      });
      audit("user.create", user.id, user.id);
      return sendData(res, publicUser(user), 201);
    }),
  );

  // POST /users/login — exchange credentials for a bearer token
  app.post(
    "/users/login",
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const { email, password } = req.body;
      const matches = list("users", (u) => u.email === email);
      const user = matches[0];
      if (!user || !verifyPassword(password, user.passwordHash)) {
        return sendError(res, 401, "bad_credentials", "email or password is incorrect");
      }
      const token = crypto.randomBytes(24).toString("hex");
      issueToken(user.id, token);
      audit("user.login", user.id, user.id);
      return sendData(res, { token, user: publicUser(user) });
    }),
  );

  // POST /users/logout
  app.post(
    "/users/logout",
    requireAuth,
    asyncHandler(async (req, res) => {
      const header = req.headers["authorization"] ?? "";
      revokeToken(header.slice(7));
      return sendData(res, { loggedOut: true });
    }),
  );

  // GET /users/:id — public profile
  app.get(
    "/users/:id",
    asyncHandler(async (req, res) => {
      const user = getById("users", req.params.id);
      if (!user) return sendError(res, 404, "not_found", "no such user");
      return sendData(res, publicUser(user));
    }),
  );

  // PATCH /users/:id — edit own profile
  app.patch(
    "/users/:id",
    requireAuth,
    requireJsonBody,
    asyncHandler(async (req, res) => {
      const target = getById("users", req.params.id);
      if (!target) return sendError(res, 404, "not_found", "no such user");
      const patch = {};
      if (req.body.displayName !== undefined) patch.displayName = sanitizeText(req.body.displayName);
      if (req.body.marketingOptIn !== undefined) patch.marketingOptIn = req.body.marketingOptIn;
      if (req.body.email !== undefined) patch.email = req.body.email;
      const updated = update("users", target.id, patch);
      audit("user.update", req.user.id, target.id, { fields: Object.keys(patch) });
      return sendData(res, publicUser(updated));
    }),
  );

  // POST /users/:id/addresses — add an address to own address book
  app.post(
    "/users/:id/addresses",
    requireAuth,
    requireJsonBody,
    asyncHandler(async (req, res) => {
      if (req.user.id !== req.params.id) {
        return sendError(res, 403, "forbidden", "cannot edit another user's addresses");
      }
      const check = validateAddress(req.body);
      if (!check.ok) return sendError(res, 400, "validation", "invalid address", check.errors);
      const addresses = [...req.user.addresses, req.body];
      update("users", req.user.id, { addresses });
      return sendData(res, { addresses }, 201);
    }),
  );
}

module.exports = { register };
