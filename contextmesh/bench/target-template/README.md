# Storefront API

REST API for a small storefront. Route handlers live in `src/handlers/` (one file
per resource: users, orders, products, reviews). All handlers share the core
modules:

- `src/models.js` — data shapes and validators
- `src/utils.js` — persistence, pagination, money, password, and audit helpers
- `src/middleware.js` — auth, roles, rate limiting, body handling, response envelope

Handlers self-register routes via `register(app)` and are wired in `src/app.js`.
