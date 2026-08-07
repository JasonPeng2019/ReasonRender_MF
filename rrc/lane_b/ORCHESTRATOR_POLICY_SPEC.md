# Lane B product-orchestrator policy contract

This is the fixed policy used by the **product runtime controller**.  It tells
the product planning orchestrator how to create a reusable Plan + Spec packet
for its worker.  It is not coding-orchestrator memory and is never sent to
EverOS.

## Inputs and privacy boundary

A product task supplies:

- `case_shape`: required stable task form, with named `{slot}` placeholders and
  no concrete per-task values.
- `slot_values`: required per-task mapping used only when rendering a worker
  packet.
- `oracle_tests`: optional supplied checks.

The planner receives `case_shape`, the slot **names**, the deterministic
oracle-check count, and the static policy.  It never receives `slot_values` or
raw `oracle_tests`.  EverOS receives/searches only `case_shape`, and indexing
also carries `external_ref`.  SQLite holds the generic template.  The worker
receives the rendered packet only.

## Deterministic complexity decision (policy version `v1`)

Count `slot_count` from declared slot names, `oracle_count` from occurrences of
`def test_` in supplied checks (use one when non-empty checks contain none), and
`shape_words` from whitespace-delimited `case_shape` words.

```text
slot_weight:   0 for 0-2, 1 for 3-4, 2 for 5+ slots
oracle_weight: 0 for 0,   1 for 1-2, 2 for 3+ checks
shape_weight:  0 for <=60, 1 for 61-160, 2 for 161+ words
complexity_score = slot_weight + oracle_weight + shape_weight
profile = lean when score <=2; detailed otherwise

estimated_implementation_tokens = clamp(
    600 + 100*slot_count + 150*oracle_count + 2*min(shape_words, 300),
    600,
    2400,
)
packet_token_budget = floor(estimated_implementation_tokens * 0.25)
```

The packet's token count is its whitespace-token count across every textual
field sent to the worker: task/case shape, signature, plan, specification,
acceptance criteria, non-goals, write paths, and read-first paths.  The runtime
rejects a packet above `packet_token_budget`; the planner may not choose its own
profile or budget.

## Required generic packet

The planning orchestrator returns one strict JSON object with exactly these
labelled fields:

```text
signature, slot_names, plan, specification, acceptance, non_goals,
write_paths, read_first
```

`slot_names` must exactly equal the task's declared slot-name set.  Any dynamic
reference in the generic template uses only `{slot_name}` placeholders.  The
generic packet must not contain literal slot values.  `read_first` has 3-5
relevant source paths; `write_paths` names the intended product paths.  The
runtime adds the fixed profile, estimate, and token budget rather than trusting
model output.

Every profile includes the task, signature/contract, named slots, acceptance
criteria, non-goals, write paths, and read-first paths.  It forbids boilerplate,
speculative edge matrices, and an implementation recipe.

- **Lean:** 1-2 direct plan steps; normal behavior; one task-relevant
  edge/error; 1-2 focused acceptance criteria.
- **Detailed:** 2-4 direct plan steps; material invariants; task-relevant
  edges/errors; hard constraints; 2-4 focused acceptance criteria.

## Runtime behavior

On a MISS the deterministic controller derives this decision, calls the planner
port once, strictly validates/parses the generic packet, renders only the
current `slot_values`, calls the worker port once, then persists the generic
packet in SQLite and indexes `case_shape` plus its `external_ref` in EverOS.

On a HIT EverOS returns only `external_ref` and score.  The controller loads
the generic SQLite packet, validates the packet, matching `case_shape`, exact
slot schema, current profile, estimate, and budget, then renders current
`slot_values` and calls the worker port.  A rejected row is a MISS and does not
suppress the planner call.

## Required deterministic proof

Use fake planner, worker, and EverOS ports.  Prove a lean MISS and detailed
MISS satisfy profile/content/budget rules.  Seed or create one generic template,
then run a same-shape task with different values and prove the HIT renders those
values without another planner call.  Capture EverOS requests and prove they
contain only case shape plus reference, never literal slot values or packet
body.
