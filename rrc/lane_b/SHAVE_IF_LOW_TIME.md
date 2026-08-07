# Shave If Low Time

At two hours from the build timer, this file is authoritative. Never remove,
disable, rewrite, or roll back a working implemented feature. Shave only an
optional feature below that is still nonworking or unimplemented: do not start
it, do not expand its partial version, and do not put it in another coding
packet.

The minimum ship set remains: EverOS `external_ref` passthrough, SQLite generic
template storage, EverOS-to-SQLite retrieval, the hit/miss reuse branch, and a
two-task COLD/WARM proof with token/pass evidence.

## Cut first

- Expand the proof from one COLD/WARM pair to a 12-20 task workload or
  interleaved families.
- Token-curve queries, plotting, dashboards, or report polish.
- Snowflake query/reporting features beyond one required per-task insert.
- Model factories, extra model comparisons, or multiple provider paths.
- PRIME, extra experiment arms, retries/cascades, or query reconciliation.
- General-purpose workload generation, runner configuration, and abstractions.
- Async agents, multi-agent harness features, or unrelated context/memory work.
- Refactors, cleanup, documentation polish, and non-blocking edge cases.

A partially written feature that has not met its acceptance behavior is
unimplemented for this rule. Stop work on it; remove its partial code only when
it blocks the minimum ship set or can crash the normal path. This is a scope
cut for nonworking work, never a cleanup task for working features.
