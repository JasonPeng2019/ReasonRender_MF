# Lane B - Template Store, EverOS Join, and Measurement (fast)

**Build timer started:** 2026-08-07 12:29:11 -07:00

Lane B preserves the RRCv2 two-store design while cutting every feature that
does not prove the warm-path saving. EverOS is the orchestrator's RRC
case-alignment index, not a generic project-memory store. RRC owns the exact
reusable artifact: a generic `Template` containing the complete templated spec
skeleton and its slot schema.

This fast profile intentionally overrides the family/session-id dictionary
shortcut in `RRCv2-plan.md`. That shortcut is useful only as a throwaway demo;
it does not preserve dynamic templates or the durable join required here.

## Non-negotiable architecture

```text
accepted fresh Spec / runtime coding case
  -> Lane A templatize() mints external_ref
  -> SQLite stores Template(external_ref, generic skeleton, slot names)
  -> EverOS RRC case index /add stores task-shape text with external_ref
     metadata
  -> EverOS /flush

new task
  -> EverOS RRC case-index /search finds the closest prior coding case
     and returns {external_ref, score}
  -> SQLite gets the exact Template by external_ref
  -> Lane A fills this task's slots and renders it
```

EverOS response content is never a spec artifact. Dynamic values are never
stored as a rendered plan. The own store holds a single generic skeleton that
can render many task instances.

## Namespaces and case alignment

Use two fixed EverOS spaces on the same server:

```text
RRC case index:       app=reasonrender, project=rrc-template-index,
                      user=rrc-runtime
project memory:       app=reasonrender, project=orchestrator-memory,
                      user=product-runtime
```

Only the first space participates in template retrieval. It contains compact
prior coding cases: task shape plus `external_ref`; it finds the best spec to
pull. General decisions, progress notes, and debugging history live only in
the second space and can never be searched by `EverOSRetrieval`.

For this fast build, the RRC case index uses EverOS's synchronous `episode`
track under the stable `rrc-runtime` identity. It is the case-alignment
MVP. Literal asynchronous `agent_case` / `agent_skill` formation remains out
of scope; it may later replace the index track without changing the
`external_ref -> SQLite Template` join.

## In scope

- A small EverOS `external_ref` metadata patch.
- SQLite template storage keyed only by `Template.external_ref`.
- `EverOSRetrieval(RetrievalPort)`: retrieve candidates, load templates, and
  store accepted templates in the `rrc-template-index` space only.
- One `CodexModel(ModelPort)` that runs one CLI completion and captures JSONL
  token usage. Strong and small may map to the same model for this build.
- A repeating dynamic-slot workload, COLD/WARM runner, token curve, and
  per-task Snowflake inserts.

## Execution topology

Use the project coding orchestrator and a fresh external DeepSeek V4 Flash
worker for each bounded code change. Exactly one is active by default. This
coding loop reads plans and files, writes code, and reviews diffs only. It does
not call EverOS, run tests, invoke a real model/subagent, start the RRC runner,
or write to Snowflake. The coding orchestrator is not the runtime identity that
will eventually use EverOS.

When code is ready for a real EverOS call, test, or real validation subagent,
the coding loop stops and returns the proposed validation work to the user. The
user decides whether and how to run it. No testing agent is launched
automatically. The operating contract is
[rrc/lane_b/SUBAGENT_PROTOCOL.md](../rrc/lane_b/SUBAGENT_PROTOCOL.md).

## Continuity policy

Every coding task finishes with a result and the model continues. Orchestrator,
workflow, packet, process-scaffold, tool, and evidence-collection errors are
non-blocking coding errors: record them, repair the delegated packet or
scaffold as needed, and continue the next useful worker slice. The coding loop
does not run tests.

Only user-authorized real validation can establish a product defect. A test or
process failure alone is test-infrastructure evidence. A defect is confirmed
only when the normal Lane B path — case-index write/search, `external_ref`
metadata, SQLite lookup, namespace isolation, or dynamic-template render — is
reproduced as wrong. It can withhold that exact validation claim, never stop
coding progress.

## Cut

- PRIME, literal async agent-case/skill tracks, OME triggers,
  session-id/id-capture fallback,
  raw-template embedding, model factories, Cortex, query-tag reconciliation,
  baseline/cascade arms, plotting polish, a multi-agent harness, and unplanned
  parallel subagents.
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
5. Prepare one focused EverOS validation case: `/add(external_ref)`, `/flush`,
   then search returns that same ref. Do not run it from the coding loop.

The RRC run sends the same `external_ref` on `/add` and `/flush`. A search hit
without a non-empty ref is ignored as a MISS. Lane B never invents an alternate
ID.

## Store and retrieve

`store(task, template, outcome)` does this exact sequence:

1. Write the canonical `Template` JSON to SQLite under `template.external_ref`.
2. Send one user episode containing the task-shape text and `external_ref` to
   the fixed `rrc-template-index` space.
3. Flush the EverOS session.

If the EverOS write fails after SQLite succeeds, leave the template in SQLite;
later lookup simply misses until EverOS has an index entry. Do not delete the
template and do not report a false success for the index write. Lane A exposes
that failure as `StoreFailure` with the already completed `SolveOutcome`, so
Lane B can persist all cost events without reporting normal solve success.

`retrieve(task, cfg)` searches only the RRC case-index episode track with its
fixed owner/scope,
`method="hybrid"`, `top_k=cfg.top_k`, and `min_score=cfg.tau_floor`. It returns
only `Candidate(external_ref, score)` values. `get_template(ref)` reads SQLite.
A stale EverOS hit whose SQLite row is absent is a normal MISS.

Before a just-written template is expected to hit, poll `/health` until
`cascade.pending == 0` twice. The workload interleaves families so the demo
does not depend on an immediate post-write hit.

## Fast coding order

1. Implement the Codex JSONL client without invoking it.
2. Implement SQLite template storage from the real `Template` contract.
3. Implement the EverOS patch and `EverOSRetrieval` join without starting the
   local server.
4. Implement the structured workload, COLD/WARM runner, and Snowflake sink
   without running them.
5. Stop at the user test gate with the exact proposed validation sequence,
   required services, expected model cost, and known risks.

At two hours, cut optional code. Do not spend remaining time on automatic
tests, fake runs, live EverOS, live models, or Snowflake. Those are separate,
user-approved validation work.

## User-approved completion validation

Only after the user approves a real validation run may a chosen testing
subagent call EverOS, execute tests, invoke a real model, run COLD/WARM, or
write Snowflake rows. The proposed checks are: real EverOS search returns the
patched `external_ref`; SQLite returns its exact generic template; that one
template renders two slot-value instances; the WARM second instance has no SPEC
call; and Snowflake receives per-task token/pass fields.

If the real test infrastructure breaks, report it without treating it as a
product defect. Only a demonstrated wrong normal-path Lane B result can prevent
claiming its affected validation condition.
