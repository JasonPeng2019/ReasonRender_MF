# Lane A - Template Pipeline (fast, preserves the two-store design)

> **Historical / non-RRCv2 / superseded.** This record is retained only for reproducibility; it is
> not current product guidance. ADR 0002 and `docs/RRCv2.md` govern the active implementation. Any
> fast-profile, one-repair, required-EverOS, or other reduced route below is fixture-only/non-product.

**Build timer started:** 2026-08-07 12:29:11 -07:00

This is the fast implementation profile. It keeps the load-bearing RRCv2
design: a SPEC produces labelled dynamic slots; RRC stores only a generic
`Template`; EverOS finds that template through `external_ref`; a new task
renders the template with its own slot values. It deliberately cuts the
unrelated competition arms and refinement features in `RRCv2.md`.

`rrc/contract.py` is frozen from `c429fbe` plus the public `Solver` and typed
`StoreFailure` decisions in ADR 0001. Use its `Task`, `Spec`, `Slots`,
`Template`, `RetrievalPort`, `ModelPort`, and `SolveOutcome` exactly as written.
The public entry point is `solve(task, *, mode, model, retrieval, cfg)`; it
constructs `RunContext` internally.

## In scope

- COLD and WARM `solve()` paths only.
- Fresh SPEC JSON with `plan`, `signature`, `contract`, `tests`, and `slots`.
- Deterministic `templatize()`, `parse_task_metadata()`, `render()`, exact
  structural matching, and a loose rendered-spec sanity check.
- One cheap implementation attempt, at most one cheap repair, then one fresh-SPEC
  fallback when a reused template fails.
- `pytest` in a timed subprocess and complete `CostEvent` capture.

## Cut

- PRIME: a NEAR template is a MISS. Keep `BranchDecision.PRIME` unused.
- BASELINE, CHEAP_ALONE, CASCADE, independent test generation, ruff, pyright,
  model-side repair loops, and more than one repair.
- Any EverOS, SQLite, Codex CLI, Snowflake, or harness code. Those are Lane B.

## Dynamic-template convention

The fast workload must make slot extraction deterministic. Each generated
`Task.text` ends with these two lines, in this order:

```text
Build a repository lookup for an Order.
RRC_SHAPE: {"arity":1,"arg_types":["int"],"fields":["id"]}
RRC_SLOT_VALUES: {"entity":"Order","function":"get_order","field":"id"}
```

The parser rejects missing or duplicate markers, duplicate JSON keys, unknown
shape keys, invalid names or types, arity/type disagreement, duplicate fields,
and colliding concrete values. It never asks a model to infer slot values. A
missing required value makes the candidate a MISS instead of guessing.

Name grammar is fail-closed: slot keys, `RRC_SHAPE.fields`, conventional
`function`/`field`/`identifier` slot values, and SPEC `identifiers`/`fields`
must be non-keyword ASCII Python identifiers matching
`[A-Za-z_][A-Za-z0-9_]*`. Entity, constant, and edge values remain arbitrary
non-empty strings. Type grammar permits ASCII names, dotted names, nested
subscriptions/generics, `|` unions, `None`, and ellipsis/list arguments inside
subscriptions; calls, lambdas, arithmetic, and other executable expressions
are rejected.

SPEC JSON has exactly `plan`, `signature`, `contract`, `tests`, and `slots`;
`slots` has exactly `entity`, `identifiers`, `types`, `fields`, `constants`,
`edge_values`, and `values`. `templatize()` replaces every concrete slot value
across all textual and nested slot fields. It rejects collisions, leakage, and
non-round-trips and returns a defensive `Template` whose `external_ref` is the
canonical SHA-256 of the full generic skeleton plus sorted `slot_names`. The
stored artifact is never a rendered instance.

## Minimal solve flow

```text
WARM task
  -> retrieval.retrieve(task)
  -> retrieval.get_template(external_ref) for each candidate
  -> exact structural match + deterministic render + loose direct-call sanity check
  -> REUSE, or MISS

MISS / COLD
  -> strong SPEC with slots

REUSE or fresh SPEC
  -> small IMPLEMENT
  -> pytest
  -> zero or one small repair + pytest
  -> reused failure only: one fresh SPEC fallback, then implement + pytest
  -> pass: templatize and return Template in SolveOutcome
```

On WARM success, `solve()` calls `retrieval.store(task, template, outcome)`
exactly once. The store call happens only after tests pass. A store exception is
raised as `StoreFailure` carrying the completed outcome and chained cause. A
missing own-store template, fingerprint mismatch, empty slot, malformed render,
or any non-EXACT candidate is a MISS; it is not an exception and it never reads
spec text from EverOS.

`Config.repair_cap_N` defaults to one: zero disables repair, positive values are
capped at one, and negative values fail before any port call. Reuse failure gets
one fresh-SPEC fallback and one fallback implementation, with no fallback
repair. Cost stages are frozen as `spec`, `implement`, `repair`,
`fallback_spec`, and `fallback_implement`.

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
