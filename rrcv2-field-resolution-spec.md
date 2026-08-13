# RRCv2 Field Resolution Specification

## Purpose

On a successful reusable Plan + Spec template retrieval, fill the template's
dynamic fields without asking the planner to reread the repository or recreate
the specification. Resolve obvious values cheaply, defer uncertain values to
the coding worker's already-curated code context, and make lookup cost
predictable.

## Terms

- **Template:** a retrieved generic Plan + Spec packet containing named
  placeholders.
- **Field:** one declared placeholder/slot in that template.
- **Resolved value:** a concrete value for a field.
- **Unresolved value:** JSON `null`; never the string `"NULL"`.
- **Resolver:** a constrained, inexpensive inference step that may resolve
  selected fields but may not revise the template.
- **Doer:** the coding worker that receives the rendered packet and its normal
  curated `Read first` list.

## Field classes

Each template field declares exactly one resolution class:

| Class | Source | Resolver access |
| --- | --- | --- |
| `request` | Current user request, task metadata, or existing controller context | No repository reads |
| `lookup` | A narrowly discoverable repository fact | Fixed, limited search/read budget |
| `worker` | A conclusion requiring the doer's code context or implementation judgment | Leave as JSON `null` for the doer |

Fields must not be generic, untyped blanks. A field that requires architectural
planning is not a `lookup` field; it is `worker` or the task must take the
normal planner/MISS path.

## Resolution flow

```text
retrieved template
  -> fill request fields from existing context (no extra reads)
  -> bounded resolver fills unresolved lookup fields
  -> leave remaining allowed fields as JSON null
  -> render the packet and give it to the doer
  -> doer resolves nulls while reading its existing curated files
```

The retrieved template's plan, specification, acceptance criteria, write
paths, read-first list, field schema, and slot set are immutable during this
flow. Resolution may only populate declared fields.

## Resolver contract

The resolver receives only:

- the one field (or a small independent batch) being resolved;
- its class and expected value type;
- the stable task shape and current request/task metadata;
- the fixed, permitted search/read scope; and
- prior resolved values only when they are explicitly declared dependencies.

It must return JSON only. One single-field response has this shape:

```json
{
  "field": "target_symbol",
  "value": null,
  "confidence": "low",
  "evidence_paths": ["src/parser.py"]
}
```

`field` must equal the requested field. `value` is either a value of the
declared type or JSON `null`. `confidence` is one of `high`, `medium`, or
`low`. `evidence_paths` contains only paths actually examined by the resolver.
The host rejects malformed JSON, undeclared fields, new fields, template edits,
or values outside the declared type/schema.

## Budgeting and scheduling

Resolution uses an explicit global token budget `B`, separate from the doer's
implementation budget. It also has fixed tool limits, such as one allowed
search and at most three file reads per resolver attempt.

For each pass, with `N` unresolved lookup fields remaining, the host sets the
per-attempt generation ceiling to:

```text
min(per_field_max, floor(2 * remaining_global_budget / N))
```

Resolve fields in round-robin order. Each field gets one bounded first attempt
before any field receives a second attempt. If a field returns `null`, it is
terminal for that pass and goes to the end of the queue only when a newly
resolved declared dependency could make another attempt useful. The host stops
when the global budget or tool budget is exhausted.

Small independent fields may be resolved in batches of two or three to avoid
per-call prompt overhead. Dependent fields remain one-field-at-a-time.

## Rendering and doer handoff

After the bounded resolver ends, the host renders all resolved values into the
packet. Any allowed unresolved fields remain JSON `null` in a structured
`unresolved_fields` section for the doer; they must not be string-substituted
into prose as `"NULL"`.

The doer may resolve only those fields while reading the packet's supplied
`Read first` files. It must report the concrete value and supporting file/path
in its result. It may not reinterpret `null` as permission to redesign the
retrieved plan or scan the repository broadly.

## Reuse rejection and fallback

Do not use the retrieved template when any of these hold:

- a required field is missing from the template schema;
- a required field needs architectural/planning judgment before coding;
- too many required fields are unresolved after the bounded lookup pass;
- resolving a required field would exceed the global token or tool budget; or
- the template cannot be rendered while preserving its field/type contract.

In those cases, treat retrieval as a reuse rejection and take the normal
planner/MISS path. This prevents a cheap resolver or the doer from becoming an
unbounded second planner.

## Acceptance criteria

1. A retrieval HIT does not invoke the expensive planner/SPEC stage.
2. Request fields are filled without repository reads.
3. Resolver output is parseable JSON matching the requested declared fields.
4. Resolver work cannot exceed its global token budget or fixed tool limits.
5. No field receives a second attempt before every eligible unresolved field
   has had a first attempt.
6. Unresolved values are represented as JSON `null`, never a sentinel string.
7. The resolver cannot modify the generic template or create new fields.
8. A task needing unresolved architectural judgment rejects reuse and falls
   back to normal planning.
