# Lane B Implementation Plan

> **Historical / non-RRCv2 / superseded.** This plan is retained only for reproducibility; it is not
> current product guidance. ADR 0002 and `docs/RRCv2.md` govern the active implementation. The
> reduced or required-EverOS route below is fixture-only/non-product.

**Build timer started:** 2026-08-07 12:29:11 -07:00

## Goal

Write Lane A + B so the future RRC runtime can reuse a generic SQLite template
chosen from its EverOS case index by `external_ref`. A warm task must be able
to skip the expensive SPEC call while using different dynamic slot values.

The future runtime case index is `app=reasonrender`,
`project=rrc-template-index`, `user=rrc-runtime`. It is separate from general
runtime project memory at `project=orchestrator-memory`,
`user=product-runtime`. The coding orchestrator and Luna use neither space.

## Coding-only execution rule

The coding orchestrator delegates each implementation slice to a fresh
`luna-xhigh-fast` coding subagent, with exactly one active by default. Every
packet has a plan, implementation spec, explicit write paths, and at most five
initial files. Luna reads and edits code, returns a code-result packet, then
exits. It does not call EverOS, run tests, invoke a model/real subagent, start
the RRC runner, or write to Snowflake.

Coding errors never block the model. Each task returns `done`,
`done_degraded`, or `ready_for_user_test`; no coding agent reports a confirmed
product defect. See [Workflow.md](Workflow.md) and
[SUBAGENT_PROTOCOL.md](SUBAGENT_PROTOCOL.md).

## Required product path

```text
Template -> SQLite(external_ref) -> EverOS add/flush(external_ref)
Task -> EverOS case-alignment search(external_ref) -> SQLite Template
     -> Lane A render -> real runtime subagent receives selected spec
```

The template is the whole generic spec skeleton. EverOS stores only searchable
task-shape text plus correlation metadata in its RRC case-index space. General
project notes are not retrieval candidates.

## Minimum ship set

1. EverOS persists and returns `external_ref`.
2. SQLite stores and retrieves the exact generic `Template` by that ref.
3. The runtime joins an EverOS hit to the SQLite template and treats a missing
   ref/row as a MISS.
4. On HIT it renders new slot values and skips SPEC; on an accepted MISS it
   templatizes, stores, indexes, and flushes.
5. The proof is one COLD task followed by one WARM task with different slot
   values. It saves the returned ref, template lookup, rendered result, token
   counts, and pass outcome. One Snowflake row is included only if that record
   is mandatory.

## Coding sequence

1. Implement the minimal EverOS `external_ref` metadata path and write down
   its live round-trip validation case.
2. Implement SQLite `put/get` for the real `Template` contract type.
3. Implement the HTTP client and `EverOSRetrieval` join, fixed to the RRC case
   index namespace.
4. Wire the runtime HIT/MISS path: render and skip SPEC on HIT; store/index
   only an accepted generic template on MISS.
5. Implement the fixed two-task COLD/WARM proof and its token/pass evidence;
   add one required Snowflake row only when needed.
6. Prepare the exact validation handoff, then stop for the user test gate.

## Two-hour cut rule

At two hours, the active coding subagent reads
[SHAVE_IF_LOW_TIME.md](SHAVE_IF_LOW_TIME.md). Every incomplete item there is
cut from the remaining packets. The orchestrator then spends the remaining
time only on the minimum ship set and its validation handoff.

## Scope cuts

See [SHAVE_IF_LOW_TIME.md](SHAVE_IF_LOW_TIME.md). Keep the minimum ship set;
cut all incomplete optional work at the two-hour mark.

## User-authorized validation gate

The coding loop must stop before a real EverOS call, real model/subagent,
pytest run, COLD/WARM run, or Snowflake write. It gives the user the changed
files, proposed commands, required services/cost, and risks. The user chooses
whether to run validation and which real subagent, if any, runs it.

Only after that authorization may a product defect be confirmed. A test or
process failure alone is test infrastructure evidence; it is not a product
defect. Only a minimal reproduction of a wrong normal Lane B behavior can
withhold its validation claim.

## Future validation checks

These are a proposed user-approved validation list, not automatic coding-loop
checks:

1. EverOS search returns the same `external_ref` supplied to `/add`.
2. SQLite retrieves the exact generic `Template` for that ref.
3. Two slot-value sets render two distinct valid specs from that one template.
4. The WARM second instance has no SPEC cost and no EverOS content is used as
   the artifact.
5. The retrieval client cannot query the general project-memory space.
6. Snowflake receives the per-task pass state and all model token counts.

## Next product slice — orchestrator policy and subagent packets

Build this only after the minimum Lane B proof has passed its user-authorized
validation gate. It turns the current direct SPEC/IMPLEMENT proof into the
intended product topology: a planning **product orchestrator** writes a bounded
Plan + Spec packet for one implementation subagent.

1. Write one versioned, static `OrchestratorPolicy` as the source of truth.
   It must define an explainable complexity score from task inputs/slots,
   supplied oracle checks, and task-description size; the score thresholds;
   and the exact `lean` and `detailed` packet profiles. It is product code or
   checked-in configuration, never EverOS memory and never a separate agent.
2. Make the policy constrain content as well as length. Both profiles always
   require the task, exact signature/contract, named dynamic slots, focused
   acceptance criteria, explicit non-goals, write paths, and 3–5 read-first
   files. `lean` permits only the normal path plus an explicitly required
   edge/error and 1–2 tests. `detailed` adds a 2–4-step plan, material
   invariants, task-specific edges/errors, hard constraints, and 2–4 tests.
   Neither permits generic boilerplate, speculative edge matrices, or an
   implementation recipe.
3. Make the policy estimate the implementation-token budget and cap the
   combined substantive **Plan + Spec** at 25% of that estimate. The policy
   provides the numeric estimate and ceiling to the product orchestrator. The
   cap includes plan detail so it cannot be bypassed by moving excess text out
   of the spec.
4. Implement an RRC-owned deterministic product runtime controller (for
   example `rrc/orchestrator_runtime.py`). For every MISS it loads the static
   policy, supplies it with the task to the product orchestrator, and receives
   a structured packet. It then gives exactly that packet to one implementation
   subagent. The controller is ordinary runtime code; it is not the planning
   model and it does not depend on EverOS for policy.
5. Define and validate a strict packet contract before the worker starts:
   `Task`, complexity/profile/budgets, Plan, Spec, non-goals, acceptance
   criteria, write paths, and read-first files. A malformed or over-budget
   packet is a normal packet-generation failure to record and route through
   the existing task policy; it must not create a second policy agent or a
   coordination loop.
6. Split every incoming product task into two explicit representations before
   planning or retrieval: stable `case_shape` and per-instance `slot_values`.
   `case_shape` contains only the reusable task structure; it may contain slot
   *names* or placeholders, but never literal dynamic values. `slot_values`
   contains those unique values and is kept in the RRC-owned task/packet path.
   Do not rely on a caller having manually removed values from free-form task
   text, as the current proof workload does.
7. Extend the RRC-owned reusable artifact deliberately from the current
   generic spec skeleton to a generic **Plan + Spec packet template** with
   labelled slots. On an EverOS/SQLite HIT, the controller retrieves the exact
   template, substitutes only the current task's `slot_values`, preserves the
   stored profile and budget metadata, and sends the rendered packet straight
   to the worker. On an accepted MISS, it stores only this generic template.
8. Index only `case_shape` plus `external_ref` in the EverOS RRC case-index
   space. Never send `slot_values`, a rendered Plan + Spec, template body,
   worker output, or general project memory to EverOS. Search uses the new
   task's `case_shape`; EverOS returns only `external_ref` and score; SQLite
   returns the generic packet template.
9. Validate this slice separately with deterministic/fake planner and worker
   fixtures first: one lean MISS, one detailed MISS, and one rendered HIT.
   Prove each packet obeys its content contract and Plan + Spec budget, while
   the HIT makes no new planner/SPEC call and renders different values from the
   same template. Assert the recorded EverOS request contains `case_shape` and
   `external_ref` but no literal `slot_values`. Real planner/subagent
   validation remains a user-authorized gate.

### Fast delivery override (authoritative for this slice)

Target source delivery plus deterministic smoke/review in about one hour. The
optional live two-task run happens only if time remains and the user authorizes
real EverOS/model/subagent cost.

```text
this orchestrator: packet scheduling and result summaries only
    -> fresh Luna fast/xhigh: one serial coding slice, then exits
    -> fresh Luna fast/xhigh: next serial coding slice, then exits
    -> fresh Luna fast/xhigh: smoke + product-focused review, then exits
    -> optional fresh Luna fast/xhigh: one live two-task check, then exits
```

Every worker is `gpt-5.6-luna` with `xhigh` reasoning and priority service.
There is exactly one active worker because all slices share the task/packet
contract. The orchestrator does not edit, test, or separately review code.

1. **Policy and packet contract — 15 minutes.** Add one versioned static
   `OrchestratorPolicy` and the smallest typed Plan + Spec packet contract.
   Score only declared inputs/slots, supplied oracle checks, and task-text
   size. Select `lean` or `detailed`; estimate implementation tokens; cap the
   substantive combined Plan + Spec at 25% of that estimate. Both profiles
   require the task, signature/contract, named slots, acceptance criteria,
   non-goals, write paths, and 3–5 read-first files. Lean has normal behavior,
   one explicitly required edge/error, and 1–2 tests. Detailed adds a 2–4-step
   plan, material invariants, task-specific edges/errors, hard constraints,
   and 2–4 tests. Both forbid boilerplate, speculative edge matrices, and an
   implementation recipe.
2. **Case boundary and reusable template — 20 minutes.** Make the product
   input explicit: stable `case_shape` plus instance `slot_values`.
   `case_shape` may contain slot names/placeholders, never literal values.
   Store a labelled generic Plan + Spec packet template and substitute only
   current `slot_values` on reuse. EverOS add/search receives only
   `case_shape` plus `external_ref`, never slot values, rendered packet text,
   template body, worker output, or project memory.
3. **Product runtime controller — 20 minutes.** Add the smallest deterministic
   controller. On MISS it loads the static policy, asks the product planning
   orchestrator for a packet, validates it, and gives it to one worker. On HIT
   it gets only `external_ref`/score from EverOS, loads the generic packet from
   SQLite, renders `slot_values`, and gives that packet to the worker without a
   new planner/SPEC call. On accepted MISS it stores the generic template and
   indexes `case_shape`.
4. **Required smoke and review — 10 minutes.** A fresh Luna test worker first
   checks the changed paths against items 1–3, then executes only two
   deterministic smoke checks: capture an EverOS request and prove it contains
   `case_shape` plus `external_ref`, never literal slot values; then seed a
   generic packet, retrieve by ref, render different values, and prove a HIT
   made no planner call. It logs results and does not repair. This is the only
   review; do not add a separate review worker unless it reports one concrete
   ambiguity.

**Optional follow-up test — only if time remains.** One fresh Luna fast/xhigh
test worker runs exactly one live two-task path: first task MISS -> planner
packet -> accepted generic template -> SQLite + EverOS; second task has the
same `case_shape` and different `slot_values` -> EverOS ref -> SQLite template
-> rendered worker packet with no new planner/SPEC call. Capture one EverOS
payload, refs, profile/budget, planner-call count, and worker packet. No
retries, expanded workload, Snowflake, second reviewer, or automated repair.

At the one-hour target, hand off after item 4. Cut the optional live test and
all automatic slot inference beyond declared values, near-match/PRIME behavior,
retry/compression loops, new stores, generalized agent management, broad
regression testing, Snowflake, and documentation polish.
