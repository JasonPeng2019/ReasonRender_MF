# Lane B Implementation Plan

**Build timer started:** 2026-08-07 12:29:11 -07:00

## Goal

Make a WARM task reuse an RRC-owned generic template that EverOS retrieves by
`external_ref`. The second instance must skip the expensive SPEC call while
using different dynamic slot values.

## Required path

```text
Template -> SQLite(external_ref) -> EverOS add/flush(external_ref)
Task -> EverOS search(external_ref) -> SQLite Template -> Lane A render
```

The template is the whole generic spec skeleton. EverOS stores only searchable
task-shape text plus correlation metadata.

## 0:00-2:00 - Build

1. Implement the minimal EverOS patch and an isolated patch test.
2. Implement SQLite `put/get` for the real `Template` contract type.
3. Implement the HTTP client and `EverOSRetrieval` join.
4. Implement one-shot Codex JSONL completion and the 12-20-task structured
   workload.
5. Prove the real retrieval round trip before adding the runner.

## At 2:00 - Cut

Drop PRIME, extra arms, extra models, any session-id fallback, plotting polish,
and query reconciliation. Keep only COLD, WARM, the metadata patch, SQLite,
and dynamic slot rendering.

## 2:00-3:00 - Consolidate

Run the COLD/WARM harness with an injected fake solver, then with Lane A when
available. The MVP is one MISS that stores a generic template followed by one
REUSE with different slot values and zero reuse SPEC tokens.

## 3:00-4:00 - Record and validate

Insert one Snowflake row per outcome/cost event, print the running token curve,
and preserve the EverOS response evidence. Do not add features after the
end-to-end warm path works.

## Handoff checks

1. EverOS search returns the same `external_ref` supplied to `/add`.
2. SQLite retrieves the exact generic `Template` for that ref.
3. Two slot-value sets render two distinct valid specs from that one template.
4. The WARM second instance has no SPEC cost and no EverOS content is used as
   the artifact.
5. Snowflake receives the per-task pass state and all model token counts.
