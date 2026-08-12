# RRCv2 convergence report

Date: 2026-08-11

## Scope

The canonical design is `docs/RRCv2.md` at SHA-256
`2a036584574a610ebcf1f32517249166d3f802b1026b83f3cfab4d41a1c5a80e`.
The implementation converges on that design with only the three requested product changes:

1. ContextMesh transports exact inputs to native workers and returns bounded receipts.
2. EverOS is optional; SQLite is the store of record and supplies a complete offline path.
3. Native Codex replaces Snowflake Cortex as the model transport and usage source.

The detailed section-by-section mapping is in `docs/rrcv2-requirement-map.md`.

## Implemented pipeline

The canonical path is `TaskEnvelope -> RETRIEVE -> EXACT/NEAR/MISS ->
REUSE/PRIME/SPEC -> IMPLEMENT -> VERIFY -> repair/fresh fallback -> ACCEPT -> store/index`.
`rrc/pipeline/solve.py` owns the state machine. `rrc/retrieval.py` owns ranked local retrieval and
structural classification. `rrc/pipeline/template.py` owns render and Tier-minus-one checks.
`rrc/pipeline/verify.py` and `rrc/pipeline/sandbox.py` own the sealed Ruff, signature, Pyright, and
pytest sequence. `rrc/journal.py` makes attempts, calls, evidence, acceptance, templates, the local
index, and the optional EverOS outbox crash-recoverable.

The ContextMesh product path is not a second RRC implementation. The native root can coordinate,
but `contextmesh/scripts/rrd_codex_hook.py` converts the exact coding assignment into a canonical
attempt before the worker starts. The worker receives the prepared Spec/source bundle allowed for
its role, has `fork_context=false`, and is denied file/shell rereads. The finisher returns only a
bounded receipt; `rrd_result_reader.py` performs the confined apply.

## Validation status

The passing zero-provider implementation gates cover contracts, retrieval, templates, the full
state matrix, journals, optional EverOS behavior, verifier contracts, product permits, ContextMesh
correlation, native hook behavior, lifecycle cleanup, the five-arm harness, and installed Codex
fake-server integration. The post-v20 hook import regression executes the real hook from a foreign
working directory with no `PYTHONPATH`, and with a poisoned `PYTHONPATH` containing a shadow `rrc`
package. Both now reach the hook policy and produce the expected JSON response.

The final verification snapshot is intentionally split rather than reported as one inflated total:

- the ContextMesh zero-provider partition passed 143 tests;
- installed Codex fake-server integration passed 15 tests, with 8 live/capability tests deselected;
- the offline smoke partition passed 13 tests;
- the guarded ordinary RRC partition passed 100 tests;
- the complete native lifecycle/runtime file test passed 18 tests when run in its required
  unwrapped macOS topology;
- the installed capability authority reopened in `validate-sealed` mode and passed all 6 tests;
- the hook suite passed 33 tests, the product-guard suite passed 14, and the real zero-provider
  product-permit test passed;
- Ruff 0.16.2, Ruff formatting, Pyright, ShellCheck, plan/design checks, reference-refresh
  validation, and `git diff --check` passed.

The first full Docker-backed `sandbox_real` verification finished with 18 passes and 22 fail-closed
backend-attestation failures in 1,140.15 seconds. Each failure occurred because
`colima status rrcv2-verifier --json` exceeded its 30-second wall limit while Colima's optional
`system_profiler` lookup waited repeatedly for macOS Activation Lock status. The owned status stdout
was byte-identical with `/usr/sbin` excluded from the observer `PATH`, so the verifier now uses only
the capability-probed `/usr/local/bin:/usr/bin:/bin` tool roots and cannot inherit a poisoned or
Activation-Lock-bearing host path. This does not increase the reviewed timeout or weaken any
identity comparison. On the corrected settled verifier bytes the exact full `sandbox_real`
partition passed all 40 tests (178 deselected) in 959.67 seconds.

The current reviewed PLAN authority is:

- PLAN SHA-256: `d50b81b37183ad9805147e4215792a43d801f424340464e3698a75d51e537c73`
- review transcript SHA-256: `d1685d4d706e89589c38f4e7306ffb23a160de05cc6bce67c6b617c0b5a595b8`
- review seal SHA-256: `ad87bbc6086e333a38cc9b7c77f31e472d4770c5600227a3b755b47079cbb0e2`
- reference refresh SHA-256: `f5f002bff90324637dfc5ea55b396a729e1c1adf3645da1f2d9a321e6270c959`

## Live credibility attempts

No successful end-to-end live RRCv2 credibility result is claimed.

V19 was consumed once and failed before any provider call because the isolated product smoke entry
could not import `rrc`. Its producer and terminal receipts have SHA-256
`7b96ad27236e5f3d1254c16bf54f6ef7da570e5ee007d98200620e4ffb0f4f06` and
`fd3d673c9b1594fe368c33be0e275bae5de5fc8005e59ef821fda34b28d5344e`.

V20 fixed the product-smoke bootstrap and was consumed once. It reached authenticated native Codex,
but the directly launched Codex hook could not import `rrc` from the target working directory. Native
Codex therefore ran as an ordinary root plus worker, while the RRC database recorded zero attempts,
calls, receipts, accepted rows, and product tool events. The run terminated with
`cell has no exact combined-session authority`. Its producer and terminal receipt SHA-256 values are
`278355527f0eb60b6ed9291ef5985bca54f974faa63d77ef9e3c0003bad60f51` and
`d39d506d7ecaa473b2ccc4d4001cf400311fd00dc9f3e10b0e298db4a6331549`.

The v20 root used 626,246 provider-visible tokens. The worker's final cumulative two-turn usage was
56,632, so the failed attempt consumed 682,878 provider-visible logical-session tokens. Cached input
was 556,032 for the root and 33,792 for the worker. These are transcript-reported usage values, not
billed cost or provider-request cardinality. The sealed copies and derivation are in
`rollouts/usage-manifest.v1.json` under the v20 producer evidence root; that manifest has SHA-256
`aa2a6146034fe0358bdba818d1b408c34d56cd3a13eb138d5ea72e0e3f17ad12`.

This failed run also explains why an apparent RRC arm can have nearly native token usage: if the
hook does not run, no Spec is reused and no large-model instruction packet reaches the small worker.
The native root keeps exploring and the worker rereads/edits directly, which is exactly the behavior
RRCv2 was meant to replace.

## Corrected boundary and remaining evidence gap

`rrd_codex_hook.py` now derives the repository solely from its own resolved file path, removes any
duplicate copy of that exact path from `sys.path`, and inserts it once at index zero before importing
`rrc`. It does not use cwd, `RRD_REPO_ROOT`, `PYTHONPATH`, HOME, or provider state to select code.
The current review references in the two product consumers and ADR 0002 were refreshed atomically;
the real zero-provider product permit test passes against that tuple.

The correction was deliberately not followed by another paid live attempt. V20 is immutable and the
reviewed M6 scope authorizes neither a rerun nor a v21 producer. Consequently, code and zero-provider
tests establish the corrected import boundary, but a future separately reviewed live run is still
required to demonstrate the three HIT/MISS/NEAR credibility beats and to support any token-savings,
quality, or economic conclusion.
