# Native Codex ContextMesh + ReasonRenderCoding matrix — 2026-08-09

## Scope and interpretation

This is one controlled, sequential, final-protocol native-Codex replicate of the same HTTP-handler
audit at one, two, and four workers. It compares native Codex alone (`baseline`) with ContextMesh +
ReasonRenderCoding using local SQLite/sealed-file memory (`combined-local`) and storage-only EverOS
memory (`combined-everos`).

All cells used Codex CLI 0.147.0, model `gpt-5.5`, medium reasoning, identical target bytes, native
ChatGPT login, and a 12-minute liveness timeout. There was no token watchdog, per-cell token ceiling,
aggregate token budget, or token-triggered abort. Counts are provider-visible input + output tokens,
include cached input, and are observational rather than exact billed usage because hidden retries are
not exposed. Quality is deterministic **lexical rubric-match quality**, not semantic adjudication.

## Evidence

- Final run: `contextmesh/runs/native-matrix-final3-20260809T221833Z` (intentionally Git-ignored,
  mode 0700)
- Experiment SHA-256: `acd12c38e0fcb294cbaadc5e02035bd85dbe6107c9709b39d91626a8a05ed836`
- Independent 23-finding rubric SHA-256:
  `f2c3b64825230d1862cda33b82ea5697cebb2fb00e645fd381d29e76d77c91d2`
- Regenerated report SHA-256: `e9e28b09685553d1337679e4aa0fbed98ff1dabd63092071c5e71455ac8df904`
- Final aggregate SHA-256: `a75643059116d17179c8303fb3c8dd03cfc19a275bb443825a5aff0b9cbf1a92`
- Executed-source map SHA-256:
  `200f8ba2a81e34d0071fd58b93baaa48357619f4655f9a7b1130f7997974fa34`
- Executed runner SHA-256:
  `a386f095af188a2aa920711d823127cab92951ee47b56e998c052644c22a0981`
- Active-runtime manifest SHA-256:
  `b1a1b3e44f85b195aad6a999a0ef08dc66a49d8f4a3825340fc75e29d8063ea3`

The run contains nine sealed cell summaries and SHA-256 seals, 30 copied native transcripts, and 75
copied evidence files. It binds the exact runtime/config/hook/sandbox/target hashes, checks
cumulative native usage, exact root/worker/planner attribution, backend identity, worker overlap,
result delivery, target immutability, and source stability. All model-output artifacts are mode
0600. The hashes above describe the exact executed runtime. After the run, three active files were
hardened without changing the sealed outputs: the runner now rejects ambiguous planner-final
evidence, and EverOS host startup/teardown now handles orphaned or signal-ignoring process-group
descendants fail-closed. Their release-candidate SHA-256 values are `b4e70ccd96a078bd32583f4384b425a9809919987df46a6329a16dbaf7b7a2a0`
(`run_bench.py`), `e828938ad812d3c314f3b5e4efdf1b1e442d16cab392697abc1df95cdad562b1`
(`rrd_start_stack.sh`), and `fa3a5ef4c9d4e6c235536ae0854362e1e3d14d6eff34bed5c87f290f9e233241`
(`rrd_stop_stack.sh`). The stricter parser accepts all six final3 planner logs, each of which contains
one identity and one final usage row; lifecycle regressions verify the operational changes.

## Cell results

| scenario | implementation | workers | root | workers | planner | total | lexical recall | lexical precision | root Codex execution wall s |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| single users | baseline | 1 | 39,920 | 33,953 | 0 | 73,873 | 100.0% | 100.0% | 37.588 |
| single users | combined local | 1 | 39,827 | 64,843 | 8,244 | 112,914 | 66.7% | 66.7% | 53.384 |
| single users | combined EverOS | 1 | 39,881 | 42,319 | 8,142 | 90,342 | 100.0% | 100.0% | 47.132 |
| users + products | combined local | 2 | 63,590 | 107,355 | 8,261 | 179,206 | 75.0% | 85.7% | 68.450 |
| users + products | combined EverOS | 2 | 52,490 | 130,672 | 8,238 | 191,400 | 87.5% | 87.5% | 60.803 |
| users + products | baseline | 2 | 75,860 | 67,879 | 0 | 143,739 | 87.5% | 100.0% | 51.832 |
| all four handlers | combined EverOS | 4 | 68,541 | 244,122 | 8,486 | 321,149 | 65.2% | 88.2% | 88.988 |
| all four handlers | baseline | 4 | 69,412 | 135,376 | 0 | 204,788 | 47.8% | 78.6% | 59.748 |
| all four handlers | combined local | 4 | 81,749 | 221,091 | 8,262 | 311,102 | 73.9% | 89.5% | 89.588 |

All nine cells passed the execution/evidence protocol. “Valid” does not mean that a cell passed the
separate savings-quality gate.

## Baseline comparisons

| scenario | combined implementation | token difference vs baseline | percent difference | root execution wall-time difference | quality eligible | savings label |
|---|---|---:|---:|---:|---|---|
| single users | local | +39,041 | +52.8% | +15.796 s | no | no |
| single users | EverOS | +16,469 | +22.3% | +9.544 s | yes | no |
| users + products | local | +35,467 | +24.7% | +16.618 s | no | no |
| users + products | EverOS | +47,661 | +33.2% | +8.971 s | no | no |
| all four handlers | local | +106,314 | +51.9% | +29.840 s | no | no |
| all four handlers | EverOS | +116,361 | +56.8% | +29.240 s | no | no |

No cell showed a token reduction. Baseline used 422,400 provider-visible tokens, combined local used
603,222 (+180,822, +42.8%), and combined EverOS used 602,891 (+180,491, +42.7%). Total measured
provider-visible usage across the nine final cells was 1,628,513 tokens. The one combined cell that
met the lexical non-inferiority gate still used more tokens, so every comparison is an observed
increase rather than a savings claim.

## Local versus EverOS

EverOS used 331 fewer tokens than local in aggregate (-0.05%), effectively a tie at this precision,
and the direction varied by task: EverOS used 20.0% fewer tokens for one worker, 6.8% more for two
workers, and 3.2% more for four workers. With one replicate, the difference cannot be attributed
causally to the memory backend. Both backends use the same deterministic digest and RRC contract;
model sampling, cache state, sequential order, and hidden retries remain plausible sources of
variation.

## Operational findings and limitations

- The combined product completed end to end on both memory backends with one, two, and four
  concurrent workers. EverOS required no embedding/model API key, and port 8000 plus ownership state
  were clean after teardown. Local mode started and checked no memory service.
- The current implementation did **not** demonstrate token efficiency on this workload. Every final
  combined total was observationally 22.3%–56.8% higher than its baseline.
- Measured planner rows were roughly 8.1–8.5k tokens per combined cell; subtracting all of them would
  not reverse any final delta. The run does not causally isolate planner versus context effects, and
  changing a planner prompt could also change downstream root/worker usage.
- Lexical quality varied. Only single-user EverOS met the automatic non-inferiority gate. Manual
  security review remains necessary because lexical matching cannot establish semantic correctness
  or safely score conjoined claims.
- One final-protocol replicate has no variance estimate. The next defensible experiment is at least
  three randomized-order replicates per cell and profiling of the enlarged combined worker context.
- The post-run parser/lifecycle hardening above means the release-candidate runtime is intentionally
  not byte-identical to the executed source map. The sealed final3 outputs and arithmetic were not
  regenerated or altered; exact executed hashes remain recorded for reproducibility.
- An excluded hardening run exposed that wrapping Codex in macOS Seatbelt while also requesting the
  native inner sandbox prevents subagents from applying a nested sandbox, and that lifecycle hooks
  observe final cumulative usage just before Codex appends `task_complete`. The launcher now uses
  Codex's documented external-sandbox mode only under a per-cell outer profile, and the hook treats
  its lifecycle callback as the finality discriminator. That excluded run measured two
  protocol-invalid attempted cells (172,978 provider-visible tokens) before interruption: baseline
  failed root-final/grammar validation and combined-local recorded a combined failure event. It is
  not included in the table.
- A prior complete run at `contextmesh/runs/native-matrix-final2-20260809T213635Z` produced nine of
  nine protocol-valid cells and 1,468,090 provider-visible tokens; all six combined totals were above
  baseline. It is excluded because the final protocol added per-cell target write denial, baseline
  cross-cell/context isolation, exact reasoning control, resumable-summary quarantine, and stronger
  host EverOS ownership. It corroborates direction only, not a second final-protocol replicate.
- An earlier complete pre-hardening matrix at
  `contextmesh/runs/native-matrix-20260809T202950Z` produced nine of nine protocol-valid cells and
  1,661,034 provider-visible tokens; all six combined totals were again above baseline. It is
  excluded because subsequent security, lifecycle, usage-finality, summary-sealing, and
  active-source controls changed the protocol and runtime source bytes. It is useful corroboration
  for direction only, not a second final-protocol replicate.
- An earlier preliminary run also exposed relative final-output and `local`/`sqlite` alias defects.
  Those incomplete calls are excluded. Both classes of defect have regression tests; only the fresh
  nine-cell final run above is used for comparisons.
