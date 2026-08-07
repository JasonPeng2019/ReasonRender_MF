/**
 * Storefront API entrypoint: wires handlers onto the app and installs the
 * terminal error middleware. Handlers self-register their routes.
 */

const users = require("./handlers/users");
const orders = require("./handlers/orders");
const products = require("./handlers/products");
const reviews = require("./handlers/reviews");
const { errorMiddleware } = require("./middleware");

function buildApp(app) {
  users.register(app);
  orders.register(app);
  products.register(app);
  reviews.register(app);
  app.use(errorMiddleware);
  return app;
}

module.exports = { buildApp };
