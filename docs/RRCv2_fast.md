# RRCv2 Fast

> **Historical / non-RRCv2 / superseded.** This record is retained only for reproducibility; it is
> not current product guidance. ADR 0002 and `docs/RRCv2.md` govern the active implementation. The
> reduced fast profile below is fixture-only/non-product.

## What this version is for

RRCv2 Fast is the smallest RRCv2 implementation that can prove the product
claim: generate a generic spec once, then reuse it for a similar new task with
different dynamic values and no new SPEC cost.

It keeps the RRCv2 two-store design. RRC owns the reusable artifact; EverOS is
only a case-alignment index. This is source-level implementation and a planned
proof path. Live EverOS/model validation remains user-authorized and is not
claimed complete until the evidence exists.

## Product path

```text
fresh task
  -> Lane A generates SPEC with labelled slots
  -> Lane A templatizes it into one generic Template(external_ref)
  -> Lane B writes Template to SQLite
  -> Lane B indexes task shape + external_ref in EverOS

similar new task with new slot values
  -> EverOS returns external_ref only
  -> SQLite returns the exact generic Template
  -> Lane A renders current slot values
  -> implementation runs with zero new SPEC tokens
```

Dynamic values are never stored as a rendered plan. EverOS content is never
used as a spec artifact. A missing ref, missing SQLite row, missing slot, or
non-exact candidate is a normal MISS.

## Lane A: generic-template pipeline

- COLD and WARM solve paths only.
- SPEC contains plan, signature, contract, tests, and labelled slots.
- Task slot values are deterministic from `RRC_SLOT_VALUES` JSON; no model is
  asked to infer them.
- `templatize()` replaces slot values across the whole spec and produces a
  stable `external_ref` fingerprint.
- WARM reuse requires exact structural matching, deterministic render, and a
  sanity check. It gets one cheap repair; a failed reused template gets one
  fresh-SPEC fallback.
- Only a passing result is stored as a reusable template. Cost events capture
  SPEC, implementation, and repair tokens.

## Lane B: durable retrieval and proof

- EverOS is patched so `/add` accepts `external_ref`, persistence keeps it,
  and episode search returns it.
- SQLite stores the full generic `Template` by `external_ref`.
- `EverOSRetrieval` searches only the fixed RRC case-index namespace, then
  joins the returned ref to SQLite. It never searches general project memory.
- The case index stores compact task/case shape plus `external_ref`; it does
  not store template text, rendered plans, slot values, or worker output.
- Store order is SQLite, EverOS add, then flush. A failed EverOS write leaves
  the SQLite template intact and later retrieval simply misses.

The minimum proof is exactly one COLD/MISS seed followed by one WARM/HIT with
different slot values. It must preserve the returned ref, SQLite lookup,
rendered second spec, token counts, and pass outcomes. The WARM task must have
`spec_tokens: 0`. One Snowflake row is included only when that record is a
requirement.

## Product namespaces

```text
RRC case index: app=reasonrender, project=rrc-template-index, user=rrc-runtime
Project memory:  app=reasonrender, project=orchestrator-memory, user=product-runtime
```

Only the RRC case index can participate in template retrieval. General project
notes and debugging history cannot become reuse candidates.

## Product packet reuse controller

The fast source also contains the next product slice for worker packets. It
extends the same reuse design to product planning and implementation packets:

- split input into stable `case_shape` and per-instance `slot_values`;
- store a generic Plan + Spec packet template with labelled slots;
- index only `case_shape` and `external_ref` in EverOS;
- on HIT, render current slots and send the packet to one implementation worker
  without a new planning/SPEC call;
- on MISS, a static versioned policy selects a lean or detailed packet profile,
  enforces its Plan + Spec token budget, and stores only the accepted generic
  template.

The policy is checked-in static code/configuration, not EverOS memory and not
another agent. The controller has deterministic fake planner/worker/EverOS
coverage for lean MISS, detailed MISS, same-shape HIT, privacy boundaries, and
the no-new-planner-call HIT. Under the coding protocol, those checks have not
been executed and no live path has been validated. Deployment remains sequenced
after the minimum two-task proof.

## Fast delivery rules

- One fresh coding subagent works on one bounded slice at a time; the coding
  orchestrator supplies a plan, spec, explicit write paths, and 3-5 initial
  files.
- Source changes and static review happen first. Real EverOS, model, pytest,
  COLD/WARM, and Snowflake operations require user approval.
- At two hours, preserve every working implemented feature and cut only
  nonworking/unimplemented optional items listed in
  [SHAVE_IF_LOW_TIME.md](../rrc/lane_b/SHAVE_IF_LOW_TIME.md).

## Explicit cuts

No PRIME or NEAR reuse, alternative experiment arms, session-id fallback,
raw-template embedding, model factories, extra model comparisons, broad
workloads, token dashboards, plotting, generalized agent management, or
automatic retries. The two-task proof is enough to establish the claimed
compute saving; larger measurement is follow-up work.
