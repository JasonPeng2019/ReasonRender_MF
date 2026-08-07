# ADR 0001: Freeze the full RRCv2 two-store contract

- **Status:** Accepted
- **Date:** 2026-08-07
- **Applies to:** Lane A and Lane B
- **Source baseline:** `c429fbe:rrc/contract.py`

## Decision

The full contract restored from `c429fbe:rrc/contract.py` is authoritative. The earlier
`Complete`/`Memory`/`Outcome` family-and-params shortcut is superseded. `rrc/contract.py` is the
only shared seam; either lane changing it is a coordination sync point.

The public callable is:

```python
solve(task: Task, *, mode: ArmMode, model: ModelPort,
      retrieval: RetrievalPort, cfg: Config) -> SolveOutcome
```

Lane A constructs `RunContext` internally. Only `COLD` and `WARM` are supported. Task text ends
with exactly one `RRC_SHAPE` line followed by exactly one `RRC_SLOT_VALUES` line. Both use strict,
duplicate-key-free JSON. SPEC output has exactly `plan`, `signature`, `contract`, `tests`, and the
nested `slots` object defined in the Lane A build sheet.

Metadata validation is fail-closed. Slot keys, structural fields, conventional name-valued slots,
and SPEC identifier/field categories use non-keyword ASCII Python identifiers. Type expressions
are limited to names, qualified names, nested generic subscriptions, unions, `None`, and supported
subscription arguments; executable expressions such as calls and lambdas are not types. Entity,
constant, and edge values remain arbitrary non-empty concrete strings.

Lane A permits zero or one repair (`Config.repair_cap_N` defaults to one and positive values are
capped at one). Failed reuse gets exactly one fresh-SPEC fallback and no fallback repair. Every
model call emits exactly one event using only these stage names: `spec`, `implement`, `repair`,
`fallback_spec`, and `fallback_implement`.

`SolveOutcome.passed` reports the final generated-spec pytest result. `pass_at_1` separately runs
the hidden oracle against the same final code. WARM stores exactly once and only after final
success. A store error raises `StoreFailure`, chains the original error, and retains the fully
assembled successful outcome and cost events.

RRC owns canonical generic `Template` artifacts addressed by the SHA-256 `external_ref`; EverOS
is only the similarity index. Candidates are resolved through `get_template(external_ref)` and
all stale, corrupt, malformed, or structurally non-exact candidates are ordinary misses.

## Consequences

- Lane B injects the typed ports and public solver without importing `rrc.pipeline` internals.
- Stored artifacts never contain rendered task instances.
- The superseded simple contract documentation must not be used for implementation.
