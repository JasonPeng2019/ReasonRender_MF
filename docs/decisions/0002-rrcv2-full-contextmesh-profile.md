# ADR 0002: Restore full RRCv2 with ContextMesh and optional EverOS

- **Status:** Accepted
- **Date:** 2026-08-10
- **Supersedes:** ADR 0001
- **Immutable source:** `docs/RRCv2.md` SHA-256
  `2a036584574a610ebcf1f32517249166d3f802b1026b83f3cfab4d41a1c5a80e`
- **Reviewed plan:** `PLAN.md` SHA-256
  `d50b81b37183ad9805147e4215792a43d801f424340464e3698a75d51e537c73`

## Context

The active implementation drifted from the original RRCv2 algorithm into a generic audit-packet
experiment. The reviewed convergence plan restores the original strong-SPEC/small-IMPLEMENT
hierarchy, PRIME/REUSE/MISS behavior, two repairs, fresh fallback, accept-before-store transaction,
and five-arm evaluation. It also retains three deliberate product changes requested by the operator.

## Decision

`docs/RRCv2.md` is the algorithmic source of truth, with only these retained differences:

1. **ContextMesh remains the exact-context and accepted-result transport.** A strong native Codex
   root coordinates one source-blind small native worker. SPEC/PRIME and implementation-blind tests
   are delivered without duplicate source delivery; the worker cannot reread repository source.
2. **EverOS is optional.** SQLite is the offline/default authoritative store and hybrid index.
   Optional EverOS is an eventually consistent similarity-index adapter using the original concrete
   task-text add/search semantics and SHA `external_ref`; it is never the store of record.
3. **Native Codex is the only provider runtime.** Ollama and OpenCode are not active dependencies.

All other RRCv2 behavior is restored: canonical generic Specs and test artifacts, deterministic
EXACT→REUSE / NEAR→PRIME / unmatched→MISS classification after the shared score floor/top-k,
independent tests for MISS/PRIME, two repairs, one fresh strong fallback sequence, conditional
verification, hidden-oracle scoring strictly after acceptance, store only after acceptance, and
complete per-stage usage evidence.

The public source-bearing seam is the sealed `TaskEnvelopeV1`; direct, benchmark, and ContextMesh
adapters must produce the same task/source/test/metadata bytes and mutually exclusive transport
target preimages. ContextMesh combined cells additionally require the durable M4 cell journal to
prove root permit, root-start, launch identity, observed spawn, attempt binding, worker completion,
and whole-root usage before a combined call is valid.

## Evidence and claim boundary

M0 evidence proves only pinned native-Codex component capabilities, static dispatch/sandbox
mechanisms, and reconciled provider-visible setup usage. It does not prove the combined product,
durability, request cardinality, semantic quality, billed cost, or savings. Those claims require the
M1–M6 implementation and final experiment gates in the reviewed plan.

## Consequences

- ADR 0001 and DES-0103 are superseded.
- `PlanSpecPacket` and `plan_spec_templates` remain historical/non-RRC compatibility artifacts only.
- The active demo, hook, engine, verifier, retrieval, and evaluation paths must converge on the
  canonical RRCv2 state machine before release.
- Any future deviation from the immutable source requires a new reviewed ADR.
