# Lane B Workflow

## Topology

Lane B has one implementation owner. It does not need a multi-agent harness,
but it does have two concrete storage systems:

```text
Lane A accepted Template
    -> SQLite exact-template store (RRC-owned)
    -> EverOS task-shape index (external_ref metadata)

new Task
    -> EverOS search returns external_ref
    -> SQLite returns exact Template
    -> Lane A renders it with this task's slot values
```

EverOS is never the artifact store. It finds an `external_ref`; SQLite owns the
generic template skeleton. The full template contains the plan, signature,
contract, tests, and slot schema, not a rendered task instance.

## Ownership

Lane B owns:

- the small EverOS `external_ref` patch;
- SQLite storage by `Template.external_ref`;
- the EverOS/SQLite `RetrievalPort` implementation;
- the Codex `ModelPort`, workload, COLD/WARM runner, and Snowflake rows.

Lane A owns SPEC generation, slot values, `templatize()`, structural matching,
rendering, pytest, and the decision to store only an accepted template.

## Working sequence

1. Patch EverOS so `/add` accepts metadata and episode search returns it.
2. Prove `SQLite put -> EverOS add/flush -> search ref -> SQLite get` with one
   known template.
3. Build the structured dynamic-slot workload and test the runner with a fake
   solver.
4. Wire the real solver only after the retrieval round trip works.
5. Run COLD and WARM streams, then write the same per-task metrics to
   Snowflake.

## Fast decisions

- Use only exact template reuse. NEAR candidates are MISS; no PRIME.
- Use one Codex model for both roles if that is the available fast path.
- Ignore a search hit with no `external_ref` or no SQLite row; it is a MISS.
- Poll EverOS indexing only before a newly written template is expected to
  retrieve. Do not block every task on it.
- Keep the external-ref patch. The old family/dictionary shortcut is not a
  valid replacement for this architecture.

## Debugging

Save one request/response pair for every real EverOS failure and reproduce it
through the thin client. Check the path in order: add metadata, persisted
metadata, search response, SQLite lookup, slot extraction, render. Fix the
first broken boundary; do not compensate in another module.
