# Lane B - Template Store, EverOS Join, and Measurement (fast)

**Build timer started:** 2026-08-07 12:29:11 -07:00

Lane B preserves the RRCv2 two-store design while cutting every feature that
does not prove the warm-path saving. EverOS is only a similarity index. RRC
owns the exact reusable artifact: a generic `Template` containing the complete
templated spec skeleton and its slot schema.

This fast profile intentionally overrides the family/session-id dictionary
shortcut in `RRCv2-plan.md`. That shortcut is useful only as a throwaway demo;
it does not preserve dynamic templates or the durable join required here.

## Non-negotiable architecture

```text
accepted fresh Spec
  -> Lane A templatize() mints external_ref
  -> SQLite stores Template(external_ref, generic skeleton, slot names)
  -> EverOS /add indexes the task-shape text with external_ref metadata
  -> EverOS /flush

new task
  -> EverOS /search returns {external_ref, score}
  -> SQLite gets the exact Template by external_ref
  -> Lane A fills this task's slots and renders it
```

EverOS response content is never a spec artifact. Dynamic values are never
stored as a rendered plan. The own store holds a single generic skeleton that
can render many task instances.

## In scope

- A small EverOS `external_ref` metadata patch.
- SQLite template storage keyed only by `Template.external_ref`.
- `EverOSRetrieval(RetrievalPort)`: retrieve candidates, load templates, and
  store accepted templates in the required order.
- One `CodexModel(ModelPort)` that runs one CLI completion and captures JSONL
  token usage. Strong and small may map to the same model for this build.
- A repeating dynamic-slot workload, COLD/WARM runner, token curve, and
  per-task Snowflake inserts.

## Cut

- PRIME, agent-case/skill tracks, OME triggers, session-id/id-capture fallback,
  raw-template embedding, model factories, Cortex, query-tag reconciliation,
  baseline/cascade arms, plotting polish, and a multi-agent harness.
- Never replace the metadata patch with the family/dictionary shortcut. It
  proves a different architecture.

## Files

```text
rrc/model.py       # CodexModel: ModelPort.complete(), one JSONL completion
rrc/store.py       # SQLite put/get of exact Template JSON by external_ref
rrc/everos.py      # add, flush, search, health wait; response envelope parsing
rrc/memory.py      # EverOSRetrieval: RetrievalPort join of EverOS + SQLite
rrc/workload.py    # 12-20 tasks ending in strict RRC_SHAPE + RRC_SLOT_VALUES JSON
rrc/run.py         # COLD/WARM loop, fake solve during isolated development
rrc/sink.py        # one Snowflake row per SolveOutcome / CostEvent
tests/lane_b/...   # fake HTTP/SQLite tests plus explicit live smoke tests
```

The workload must emit the strict marker grammar frozen by ADR 0001 and the
Lane A build sheet: identifier-bearing values and structural fields are valid
ASCII Python identifiers, while argument types use the supported non-executable
annotation grammar. Lane B generates these values; Lane A remains the enforcing
boundary.

## The EverOS patch

Patch the checked-out `EverOS` submodule, not the RRC client around it.

1. Add optional `external_ref: str | None` to `MemorizeAddRequest` in
   `src/everos/entrypoints/api/routes/memorize.py`.
2. Thread it through `service/memorize.py` and the existing ingest/extraction
   persistence path without including it in model prompts.
3. Persist it with each produced memory's frontmatter and LanceDB metadata, so
   every memory fanned out from one `/add` carries the same ref.
4. Add it to `SearchEpisodeItem` in `memory/search/dto.py` and populate it in
   `memory/search/shaper.py` from candidate metadata.
5. Add one focused EverOS test: `/add(external_ref)` then `/flush`, followed by
   search, returns that same ref.

The RRC run sends `external_ref` on `/add`. A search hit without a non-empty
ref is ignored as a MISS. Lane B never invents an alternate ID.

## Store and retrieve

`store(task, template, outcome)` does this exact sequence:

1. Write the canonical `Template` JSON to SQLite under `template.external_ref`.
2. Send one user episode containing the task-shape text and `external_ref`.
3. Flush the EverOS session.

If the EverOS write fails after SQLite succeeds, leave the template in SQLite;
later lookup simply misses until EverOS has an index entry. Do not delete the
template and do not report a false success for the index write. Lane A exposes
that failure as `StoreFailure` with the already completed `SolveOutcome`, so
Lane B can persist all cost events without reporting normal solve success.

`retrieve(task, cfg)` searches the episode track with fixed owner/scope,
`method="hybrid"`, `top_k=cfg.top_k`, and `min_score=cfg.tau_floor`. It returns
only `Candidate(external_ref, score)` values. `get_template(ref)` reads SQLite.
A stale EverOS hit whose SQLite row is absent is a normal MISS.

Before a just-written template is expected to hit, poll `/health` until
`cascade.pending == 0` twice. The workload interleaves families so the demo
does not depend on an immediate post-write hit.

## Fast implementation order

1. Confirm `codex exec --json` produces final text plus `turn.completed` usage.
2. Implement and test SQLite template round trips using real `Template` values.
3. Implement the EverOS patch and prove the external-ref round trip against the
   local server.
4. Implement `EverOSRetrieval` with fake HTTP tests, then its live smoke test.
5. Generate a small structured dynamic-slot workload and run COLD/WARM through
   an injected fake `solve`.
6. Wire in Lane A's real `solve`, record every `CostEvent`, and insert outcome
   rows into Snowflake.

At two hours, cut all optional work. At three hours, the required MVP is a
real EverOS search returning a ref, SQLite returning its template, and Lane A
reusing it with new slot values. The final hour is Snowflake inserts and a
measured cold-versus-warm report.

## Completion gate

Lane B is ready when a real EverOS search returns the patched `external_ref`,
SQLite returns the exact generic template, the same template renders two task
instances with different slot values, and the WARM run records no SPEC call on
the second instance. Snowflake must receive the per-task token and pass fields.
