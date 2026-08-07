# Lane A - Template Pipeline (fast, preserves the two-store design)

**Build timer started:** 2026-08-07 12:29:11 -07:00

This is the fast implementation profile. It keeps the load-bearing RRCv2
design: a SPEC produces labelled dynamic slots; RRC stores only a generic
`Template`; EverOS finds that template through `external_ref`; a new task
renders the template with its own slot values. It deliberately cuts the
unrelated competition arms and refinement features in `RRCv2.md`.

`rrc/contract.py` is frozen. Use its `Task`, `Spec`, `Slots`, `Template`,
`RetrievalPort`, `ModelPort`, and `SolveOutcome` exactly as written.

## In scope

- COLD and WARM `solve()` paths only.
- Fresh SPEC JSON with `plan`, `signature`, `contract`, `tests`, and `slots`.
- Deterministic `templatize()`, `extract_slot_values()`, `render()`, exact
  structural matching, and a loose rendered-spec sanity check.
- One cheap implementation attempt, one cheap repair, then one fresh-SPEC
  fallback when a reused template fails.
- `pytest` in a timed subprocess and complete `CostEvent` capture.

## Cut

- PRIME: a NEAR template is a MISS. Keep `BranchDecision.PRIME` unused.
- BASELINE, CHEAP_ALONE, CASCADE, independent test generation, ruff, pyright,
  model-side repair loops, and more than one repair.
- Any EverOS, SQLite, Codex CLI, Snowflake, or harness code. Those are Lane B.

## Dynamic-template convention

The fast workload must make slot extraction deterministic. Each generated
`Task.text` ends with an `RRC_SLOT_VALUES` JSON object, for example:

```text
Build a repository lookup for an Order.
RRC_SLOT_VALUES: {"entity":"Order","function":"get_order","field":"id"}
```

`extract_slot_values()` reads that object. It never asks a model to infer slot
values. A missing required value makes the candidate a MISS instead of guessing.

`templatize()` replaces every value listed in `Spec.slots.values` in the plan,
signature, contract, and tests with named placeholders. It returns a `Template`
whose `external_ref` is a stable fingerprint over the generic skeleton and
`slot_names`. The stored artifact is this full templated spec skeleton, not just
the plan field and never a rendered instance.

## Minimal solve flow

```text
WARM task
  -> retrieval.retrieve(task)
  -> retrieval.get_template(external_ref) for each candidate
  -> exact structural match + deterministic render + loose sanity check
  -> REUSE, or MISS

MISS / COLD
  -> strong SPEC with slots

REUSE or fresh SPEC
  -> small IMPLEMENT
  -> pytest
  -> one small repair + pytest
  -> reused failure only: one fresh SPEC fallback, then implement + pytest
  -> pass: templatize and return Template in SolveOutcome
```

On WARM success, `solve()` calls `retrieval.store(task, template, outcome)`.
The store call happens only after tests pass. A missing own-store template, an
empty slot, malformed rendering, or any non-EXACT candidate is a MISS; it is
not an exception and it never reads spec text from EverOS.

## Module ownership

```text
rrc/pipeline/solve.py      # COLD/WARM routing, fallback, cost-event assembly
rrc/pipeline/stages.py     # SPEC, IMPLEMENT, repair prompts and JSON parsing
rrc/pipeline/template.py   # template fingerprint, slots, match, render, sanity
rrc/pipeline/verify.py     # timed pytest only
rrc/pipeline/prompts.py    # short single-shot prompts
rrc/pipeline/stubs.py      # deterministic fake ModelPort for unit tests
```

All model calls use `model.complete(...)` once. Every prompt says: output only
the requested artifact; do not run commands, edit files, or explain.

## Tests before Lane B exists

Use `FakeModel` plus an in-memory `RetrievalPort` fixture. Prove:

1. A fresh SPEC becomes a generic template with placeholders and a stable ref.
2. A matching task renders a different instance with zero SPEC calls.
3. A missing value or shape change is a MISS.
4. A reused-spec failure triggers one fresh-SPEC fallback.
5. Only passing WARM tasks ask the retrieval port to store a template.

## Fast completion gate

Lane A is ready when a deterministic fake run shows one MISS that stores a
template followed by one REUSE with different slot values, zero reuse SPEC
tokens, and passing pytest. Lane B can then replace the in-memory retrieval
fixture without any Lane A change.
