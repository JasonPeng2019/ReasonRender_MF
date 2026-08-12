# PLAN — Hierarchical ReasonRender four-way ablation

> **Historical / non-RRCv2 / superseded.** This experiment record is retained only for
> reproducibility; it is not current product guidance. ADR 0002 and `docs/RRCv2.md` govern the active
> implementation. The handler-audit ablation below is fixture-only/non-product.

> Written by `/plan` on 2026-08-09. This plan is attacked by `/adversarial` before
> implementation. Keep it live: check off milestones and record deviations.

## Goal

Make the native-Codex experiment use a strong coordinator/planner and a cheaper worker model,
eliminate ContextMesh's duplicate delivery of source bytes, test live versus zero-token
ReasonRenderCoding (RRC) packets, and then run a randomized four-replicate 2x2 ablation of RRC and
ContextMesh at one, two, and four workers with token, API-equivalent cost, and blind semantic-quality
evidence.

## Non-goals

- Do not claim that the current generic Plan+Spec planner has audited source it has never received.
  In this milestone the planner decomposes and constrains work; the assigned worker still evaluates
  its source evidence exactly once. A future source-aware planner brief is a separate experiment.
- Do not treat ChatGPT/Codex subscription usage as an actual API invoice. Dollar values are labeled
  API-equivalent estimates from a frozen official price table.
- Do not compare SQLite with EverOS in the factorial. The four-way ablation uses local SQLite so the
  memory backend cannot confound the two intervention factors. EverOS keeps a deterministic smoke
  test only.
- Do not commit raw Codex transcripts, credentials, generated homes, or ignored run bundles.
- Do not restore Ollama, OpenCode, Tollgate, or an external embedding/model endpoint.

## Constraints

- The operator explicitly requested: no duplicate source delivery; live-planner versus
  `RRC_CONTROL=deterministic`; three to five randomized replicates; a four-way ablation; one-, two-,
  and four-worker setups; and separate provider-visible, uncached-input, cached-input,
  output/reasoning, estimated-cost, and semantic-quality metrics.
- Use four replicates, within the requested three-to-five range, so every variant can occupy every
  ordinal position exactly once per scenario. Freeze the order seed and every cell before the first
  product call; do not choose or omit cells based on interim tokens.
- Keep the existing 12-minute per-cell liveness timeout but no token ceiling or token-triggered
  abort. A logical cell owns one immutable physical attempt ID created before process launch.
  Resume skips sealed attempts and starts only logical cells with no attempt ID; an interrupted or
  partial attempt is retained and invalid, never quarantined and rerun in place.
- Root/coordinator and live RRC planner use `gpt-5.5` at medium reasoning. Workers use
  `gpt-5.4-mini` at low reasoning through the documented
  `agents.default_subagent_model` and `agents.default_subagent_reasoning_effort` settings. A live
  canary must prove the account and pinned CLI actually use those exact models; otherwise the paid
  experiment aborts rather than silently substituting.
- Official prices frozen on 2026-08-09 are per million tokens: GPT-5.5 $5 uncached input, $0.50
  cached input, $30 output; GPT-5.4 mini $0.75 uncached input, $0.075 cached input, $4.50 output.
  The report links the official model pages, records the table/hash/date, and computes cost per
  attributed model turn. GPT-5.5 turns over the documented 272K-input threshold multiply uncached
  and cached input prices by 2 and output by 1.5 for that entire turn, rather than thresholding an
  aggregate. Native transcript `last_token_usage` rows are the turn authority; monotonic cumulative
  rows are retained as the reconciliation authority.
- Existing native keyring auth, credential-deny sandbox, source seals, transcript attribution,
  fail-closed evidence parsing, exact worker counts, and proof that every spawn completes before
  the first wait remain mandatory. Common transcript-lifetime overlap is recorded but is not a
  validity gate because Codex serializes spawn handshakes and a fast first worker can finish while
  later worker prompts are still being admitted.
- Workspace mode is `unleashed`; direct implementation is allowed. No new runtime dependency is
  required.

## Grounded wiring and design-bank clarification

- `contextmesh/scripts/rrd_native_config.py::config_text` currently gives the root and spawned
  workers the same `gpt-5.5`/medium settings; Codex's current official subagent reference documents
  the two `[agents]` defaults selected above.
- `contextmesh/scripts/rrd_codex_hook.py::_handle_pre_tool` injects a handler's exact source, while
  `_shared_context` injects both each shared file's digest and its exact bytes. Final3 worker
  transcripts then show direct `nl`/`sed` reads of those same four files. The duplicate source path
  is therefore real and measurable.
- `contextmesh/scripts/rrd_codex_hook.py::_resolve_packet` already has the zero-token deterministic
  control. The live generic planner cost about 8.1K–8.5K tokens per prior cell.
- `contextmesh/bench/run_bench.py` currently executes baseline/combined-local/combined-EverOS once
  at worker counts 1/2/4 and reports total plus cached input and lexical rubric matching. It does not
  implement the requested 2x2 factors, replication, cost table, or semantic adjudication.
- Design-ledger queries for hierarchical planner/worker selection, duplicate source-delivery policy,
  and the replicated ablation protocol returned no matching entry. The operator resolved the
  material product direction. Low-risk experimental choices made here are exact-source-once rather
  than digest/on-demand, four replicates, local SQLite for the factorial, and a blind adjudicator
  whose own usage is reported separately.

## Approach

### 1. Establish a real hierarchy and a single-delivery contract

Generate a native Codex config with GPT-5.5/medium for the root and planner and the documented
GPT-5.4-mini/low defaults for spawned workers. Validate transcript/session metadata against those
roles; model drift invalidates the cell. Live planner execution is non-ephemeral and its sealed
native transcript/turn-context must expose and reconcile effective GPT-5.5/medium settings; the
requested `--model` string alone is not proof.

For ContextMesh-enabled arms, deliver the assigned handler plus the three shared files as one sealed,
line-indexed exact-source bundle. Every physical source line occurs exactly once as
`L<1-based decimal>:<original line>`; removing only the controller-owned prefix reconstructs the
exact UTF-8 bytes, including the final-newline bit. Do **not** include the structural digest text
beside the exact bytes. The hook records a source hash, byte count, line count, and final-newline bit
for each of exactly four delivered files. After that bundle is
delivered, any worker local-shell/file-read tool call is denied and recorded as a source-reread
attempt. A blocked canonical required-source command remains valid only when the transcript call ID,
hook violation, immediately following policy denial, and tool result all correlate and the result
contains only the denial—not source bytes. Count and report these attempts as enforcement overhead.
Any uncorrelated denial, noncanonical/extra tool, or returned source content is protocol-invalid.
The worker prompt still says to use the injected bundle and not tools. Source unavailability fails
open to direct reads for the interactive demo but marks an experiment cell invalid, so the benchmark
never calls a fallback a successful single-delivery run.

The no-ContextMesh arms retain direct reads, but each worker is restricted to exactly four
`/usr/bin/nl -ba <path>` calls: its assigned handler and the three shared files. Wildcards, scripts,
other tools, missing files, duplicates, and cross-handler reads invalidate the cell. This preserves a clean factorial: source
delivery and native wait-result compression are ContextMesh interventions, while the RRC packet is
the RRC intervention. All arms use the same root/worker model hierarchy, prompt grammar, target
bytes, sandbox, worker count, and evaluator.

### 2. Express the four factors explicitly

Use four local variants for every scenario:

| variant | RRC packet | ContextMesh exact-source-once + result compression |
|---|---:|---:|
| `native` | no | no |
| `rrc` | yes | no |
| `contextmesh` | no | yes |
| `combined` | yes | yes |

One hook implementation receives sealed factor flags. RRC-disabled arms must emit no packet or
planner evidence. ContextMesh-disabled arms must emit no source bundle, digest delivery, or
compression decision. Poison fixtures make cross-factor leakage fail closed. `RRC_CONTROL` is an
explicit cell field, not inherited ambient state.

### 3. Calibrate live versus deterministic RRC before freezing the factorial

Run eight paired, position-balanced randomized calibration cells on the four-handler/four-worker
combined-local task: four live planner and four deterministic packet cells, all using the new worker model and
single-delivery contract. Each mode occupies each of the two within-pair positions twice. Blind
semantic scoring is frozen before calls. Analyze complete live/deterministic pairs only and require
at least three of the four randomized pairs, retaining one provider-failure margin. With three
pairs the two launch orders are necessarily imbalanced 2:1, so selection must pass the quality
thresholds globally **and separately in both launch-position strata**; each stratum is evaluated
without weighting it by its replicate count. Select deterministic for the factorial only when its
median per-replicate semantic F1 is no more than five percentage points below live, its pooled
micro-recall over critical/high rubric findings is not lower than live, and its planner usage is
zero in every analyzed deterministic cell, both globally and in each position stratum. Equality
passes. Fewer than three complete pairs, malformed/non-finite quality metrics, or a missing launch
stratum selects live and marks calibration inconclusive. An incomplete pair remains visible with
all recoverable usage/cost but contributes to neither mode. The selection record contains every
cell's errors and accounting, the included/excluded replicate IDs, both position strata, the
predeclared rule, the selected mode, planner usage, calibration totals, and calibration-judge
overhead; the final aggregate/report repeats those totals and cannot erase the failed attempt.

### 4. Run the replicated four-way experiment

After selection, freeze 48 product cells: 3 scenarios (1, 2, 4 workers) x 4 variants x 4 replicates.
For each scenario, draw a permutation of the four variants using a named derived seed, then use its
four cyclic rotations in an independently derived and recorded random replicate order. Thus every
variant occupies each ordinal position exactly once. Shuffle the twelve scenario/replicate blocks
with a third named recorded seed; calibration order and judge labels have their own named derived seeds.
Execute sequentially to avoid stable-home/backend concurrency. A
replicate is an independent fresh target, fresh root session, and fresh RRC/ContextMesh round.

Report per cell and distribution summaries (median, min/max, and sample standard deviation where
defined) for:

- total provider-visible tokens (`input + output`);
- uncached input (`input - cached_input`) and cached input;
- output and reasoning output (reasoning is a reported subset of output, never added twice);
- root, worker, and planner component splits;
- API-equivalent estimated dollars using the frozen per-model-turn price table;
- wall time and protocol validity;
- lexical rubric metrics retained for reproducibility; and
- blind semantic precision, recall, and F1 against the frozen independent rubric.

Before any product call, seal the analyzer and its hash into the experiment manifest. For each
complete scenario/replicate block, run the formulas over these exact scalar fields:
`provider_visible_tokens`, `uncached_input_tokens`, `cached_input_tokens`, `output_tokens`,
`reasoning_output_tokens`, `api_equivalent_dollars`, `wall_seconds`, `lexical_f1`, and `semantic_f1`;
and, for each of `root`, `worker`, and `planner`, the component's total, uncached-input,
cached-input, output, reasoning-output, and API-equivalent-dollar fields. For every field `y`,
compute absolute-factor estimands in native units:
`RRC = 0.5*((rrc-native)+(combined-contextmesh))`,
`CM = 0.5*((contextmesh-native)+(combined-rrc))`, and
`interaction = combined-rrc-contextmesh+native`. Aggregate each estimand across the four blocks for
a scenario using its arithmetic mean and sample standard deviation; median/min/max are descriptive.
Relative variant-versus-native percentages are reported only when native is nonzero and are not
factorial estimands. A block with any invalid cell or judgment is excluded whole, never imputed.
Fewer than three complete blocks makes that scenario's headline factorial conclusion
`INSUFFICIENT`, while retained valid cells and every paid attempt remain visible.

Semantic evaluation uses shuffled opaque candidate labels. One `gpt-5.5`/medium adjudicator call per
scenario/replicate block sees the frozen source/rubric and all four opaque outputs, never variant
names or token counts; each calibration replicate similarly groups its two opaque outputs. Its
strict top-level output is `{"candidates":[{"label":...,"claims":[...]}]}` so an empty candidate is
representable. It contains exactly one row for every parsed claim with candidate label, zero-based
claim index, one scenario-valid rubric ID or `null`, `supported`, and a rationale capped at 512 UTF-8
bytes. A claim is a true positive only when `supported` is true, the rubric ID is non-null, the
deterministic scorer independently confirms exact severity equality and same-handler line-range
overlap from the parsed claim/rubric, and no earlier claim for that candidate consumed the ID.
Later duplicate mappings, unsupported claims, wrong severity, wrong/non-overlapping handler line
ranges, and claims mapped to `null` are false positives. Unconsumed in-scope rubric IDs are false
negatives. One conjoined bullet is indivisible and can consume at most one rubric ID, and
`supported=true` means every conjunct, qualifier, and causal statement is supported by that one
finding. Zero claims produce precision=1 only when the scenario rubric is also empty (it is not
here), recall=0, F1=0. Every candidate must contain exactly the grammar-parsed claim indices once;
unknown IDs, missing or duplicate indices/labels, extra rows, judge-supplied mechanical booleans, or
malformed JSON invalidate the whole judge block. The deterministic scorer derives micro
precision/recall/F1; the adjudicator never emits scores. Candidate labels and within-call order are
derived from a sealed independent seed and withheld from the adjudicator prompt.

Adjudicator usage/cost is stored and reported as **evaluation overhead**, excluded from product
totals. A malformed or missing judgment invalidates semantic comparison rather than falling back to
lexical scores. Because one model judge is not ground truth, the report retains the raw judgment and
labels the metric model-adjudicated semantic quality.

### Alternatives considered and rejected

- **Digest-only with on-demand reads:** this can save initial context when most source is irrelevant,
  but the audit requires all four files and prior workers read all four anyway. Exact-source-once is
  simpler to enforce and directly removes the observed double delivery.
- **Source-aware RRC planner that emits all vulnerability findings:** this best matches a pure
  planner/renderer hierarchy, but it changes the RRC packet trust boundary and makes workers unable
  to independently inspect evidence. First determine whether the existing live Plan+Spec call has
  measurable value after eliminating duplication; source-aware briefs can then be evaluated as a
  separate design rather than smuggled into this ablation.
- **Reuse the prior nine-cell backend matrix:** it conflates RRC, ContextMesh, and backend and has
  one replicate. A 2x2 local factorial is the smallest design that estimates the requested main and
  interaction effects.
- **Use lexical quality only:** rejected because the operator explicitly requested semantic audit
  quality and the existing report correctly says lexical matching is not semantic adjudication.

**Relevant ADRs:** none; the design ledger returned no matching entry.

## Milestones

- [x] **M1 — Hierarchical model and exact-source-once protocol.** Add worker model/reasoning config,
  factor flags, exact source bundle, reread denial/evidence, and role/model attribution. **Acceptance:**
  unit fixtures reconstruct exact bytes/line numbers and reject digest+source duplication, source
  reread delivery, malformed denial evidence, wrong worker model, and fallback; active README/env/CLI help documents the hierarchy;
  an installed-Codex canary proves GPT-5.5 root, GPT-5.5 live planner, and GPT-5.4-mini worker from
  effective native transcript/turn-context evidence, with zero successful worker source reads in a
  ContextMesh cell and every attempted canonical reread visibly blocked.
- [x] **M2 — Four-way replicated harness and metrics.** Replace the old 3x3 single-replicate matrix
  contract with explicit 2x2 factors, four position-balanced randomized replicates, component usage
  fields, exact
  uncached/cached/output/reasoning arithmetic, and per-turn price estimates. **Acceptance:** offline
  fixtures prove 48 unique cells, seeded cyclic Latin-square positional balance, resume identity,
  factor poison isolation, per-turn/cumulative reconciliation, cost math including a multi-turn
  threshold crossing, recoverable invalid-attempt usage, and invalid usage/model evidence rejection;
  immutable-attempt fixtures prove interrupted attempts are not replaced; the analyzer hash,
  estimand formulas, complete-block exclusion, and insufficient-block threshold are sealed;
  README/help and historical RFC status point operators to the new matrix.
- [x] **M3 — Planner calibration and semantic evaluator.** Add the eight-cell live/control calibration,
  immutable selection rule, blind strict-schema adjudication, and evaluation-overhead accounting.
  **Acceptance:** fixtures prove deterministic/live selection boundaries, opaque label shuffling,
  unsupported/duplicate claim handling, malformed-judge rejection, and exclusion of evaluator
  tokens/cost from product totals.
- [ ] **M4 — Deterministic and live verification.** Run focused/full static and test gates, local and
  EverOS smoke tests, the model hierarchy canary, then the eight calibration cells. Freeze the selected
  RRC mode only after the calibration record validates. **Acceptance:** every gate and calibration
  attempt is recorded. A deterministic harness defect abandons the entire experiment ID; after a
  fix, a new manifest reruns the full calibration and retains the abandoned usage/cost in an
  excluded-run registry. Provider, timeout, model, or output failures remain invalid observations
  and are never converted to zero or retried inside the experiment.
- [ ] **M5 — Execute and analyze the four-way ablation.** Run/resume all 48 frozen cells and semantic
  blocks, regenerate the report from sealed summaries, and conduct research-claim and adversarial
  reviews. **Acceptance:** 48/48 attempted cells and all judge blocks appear; the report gives
  distributions, paired within-block deltas, RRC x ContextMesh interaction, quality/cost tradeoffs,
  limitations, exact run/hash paths, and no causal or billing claim beyond the evidence.

## Definition of done

- [ ] ContextMesh-enabled worker prompts contain each required exact source exactly once, contain no
  digest alongside it, and no worker source-read attempt returns source bytes; a blocked canonical
  attempt is counted, while missing/malformed denial correlation invalidates the cell.
- [ ] Installed Codex evidence proves exact GPT-5.5/medium root and planner roles and
  GPT-5.4-mini/low worker roles; no silent fallback or same-model worker is accepted.
- [ ] A sealed four-pair live-versus-deterministic calibration applies the predeclared selection
  rule and records zero planner tokens for every deterministic cell.
- [ ] The frozen final matrix has exactly 48 unique product cells covering four factor combinations,
  all three worker counts, and exactly four fresh, position-balanced randomized replicates.
- [ ] Every valid cell separately reports provider-visible total, uncached input, cached input,
  output, reasoning output, component splits, wall time, and API-equivalent cost without double
  counting reasoning or evaluator overhead. Invalid/timed-out attempts retain every recoverable
  usage/cost row and explicit `unquantified consumption` when no usage row is available; scheduled
  attempts are never silently replaced.
- [ ] Every comparison reports lexical and model-adjudicated semantic precision/recall/F1; judge
  labels are blind, unsupported claims count against precision, missing/malformed judgments fail
  closed, and judge usage/cost is separate.
- [ ] The sealed analyzer computes the preregistered absolute RRC main effect, ContextMesh main
  effect, and difference-in-differences interaction per complete block for tokens, dollars, wall
  time, lexical F1, and semantic F1; incomplete blocks are excluded and fewer than three blocks is
  labeled insufficient.
- [ ] Baseline/RRC/ContextMesh/combined factor isolation is proven by both poison tests and live
  evidence; local SQLite is the only factorial backend and EverOS still passes storage-only smoke
  and teardown.
- [ ] Full pytest, Ruff check/format, Pyright, `bash -n`, ShellCheck, `git diff --check`, active
  provider scan, and secret scan pass on final bytes; final research-claim and adversarial reviews
  are non-blocking.
- [ ] Active README, environment example, CLI help, and superseded-RFC pointers describe the new
  hierarchy, source contract, calibration, factor variants, resume command, and 48-cell matrix.
- [ ] After live validation, `/design-maintain` records the supported public model hierarchy and
  exact-source-once ContextMesh boundary in standing design canon; until then this RFC is an
  experiment proposal rather than canon.
- [ ] A concise tracked results report identifies exact ignored run directories and hashes, explains
  why the observed deltas occurred, and does not call one four-replicate benchmark universal proof.

## Verification plan

- **Types:** `uv run --locked pyright rrc contextmesh/scripts contextmesh/bench tests`.
- **Tests:** assert `codex --version` is exactly `codex-cli 0.147.0`; run the non-skippable installed
  contract `uv run --locked pytest -q -p no:cacheprovider
  tests/test_rrd_codex_cli_integration.py` and require zero skipped tests; then focused
  `uv run --locked pytest -q -p no:cacheprovider
  tests/test_rrd_codex_hook.py tests/test_rrd_multiagent_demo.py
  tests/test_rrd_product_matrix.py tests/test_rrd_native_codex.py`; finally run
  `uv run --locked pytest -q -p no:cacheprovider`.
- **Lint/format:** `uv run --locked ruff check rrc contextmesh/scripts contextmesh/bench tests` and
  `uv run --locked ruff format --check rrc contextmesh/scripts contextmesh/bench tests`.
- **Shell/build:** `bash -n contextmesh/RRDdemo.sh contextmesh/RRDdemo-local.sh
  contextmesh/RRDdemo-everos.sh contextmesh/demo.sh contextmesh/scripts/rrd_demo.sh
  contextmesh/scripts/rrd_demo_tui.sh contextmesh/scripts/rrd_demo_preflight.sh
  contextmesh/scripts/rrd_start_stack.sh contextmesh/scripts/rrd_stop_stack.sh`;
  `/usr/local/bin/shellcheck` on that same explicit list; `git diff --check`; active provider scan
  `uv run --locked pytest -q -p no:cacheprovider
  tests/test_rrd_native_codex.py::test_public_demo_surface_has_no_legacy_provider_dependency`; and
  `.agent-workspace/bin/secret-scan .` (plus `--staged` if the operator later requests a commit).
- **Live:** local/EverOS storage preflight/smoke without `--canary` (zero model sessions); one
  provider-backed root+live-planner+worker hierarchy/single-delivery canary; eight
  position-balanced planner calibration cells; then one resumable 48-cell matrix invocation plus blind semantic
  adjudication. Regenerate aggregate/report from seals and compare hashes.
- **Review:** `/research-claim-review` on the calibration selection and final result; `/adversarial`
  after risky protocol milestones and on the final diff.

## Risks & one-way doors  ⚠️

- **Provider usage:** experiment cells contain at most 56 root sessions (8 calibration + 48 final),
  144 worker sessions (32 calibration + 112 final), 28 live planner sessions if live is selected
  (four calibration plus 24 factorial RRC cells), and 16 adjudicator sessions (four calibration plus
  twelve factorial blocks). The mandatory hierarchy canary adds one root, one worker, and one live
  planner session, making the per-experiment maximum 57/145/29/16. Local/EverOS storage preflights
  make no model call. Each session may contain multiple model turns. This can be expensive.
  The operator explicitly asked for 3–5 replicates and previously removed token ceilings; wall
  timeouts remain only for liveness. Only logical cells without an attempt ID are resumable; every
  physical attempt is immutable and visible.
- **Model availability:** GPT-5.4-mini may not be available through the current ChatGPT Codex login.
  The canary aborts before the matrix instead of substituting another model.
- **Factor purity:** ContextMesh encompasses exact-source delivery and result compression, so its
  measured effect is the bundle, not either feature independently. The report must not attribute a
  delta to compression alone.
- **Judge variance/circularity:** a blind model judge is stronger than lexical matching but not human
  ground truth and uses a related model family. Preserve raw judgments, report judge usage, retain
  lexical metrics, and avoid a universal quality claim.
- **RRC meaning:** if deterministic matches live, the result only says this generic packet planner is
  unnecessary for this frozen audit workload. It does not show that source-aware planning or RRC in
  general is useless.
- **Public experiment schema:** replacing the old 3x3 matrix schema is reversible because historical
  reports/runs remain immutable and the new manifest version changes. No data migration is required.
- **Invalid paid attempts:** every scheduled cell is attempted at most once after the implementation
  and canaries are frozen. Provider/rate-limit/protocol failures remain as invalid observations and
  are not replaced; analysis uses complete paired blocks and reports attrition, while all
  recoverable usage/cost remains in experiment totals. Interrupted physical attempts are invalid,
  not resumable; “resume” continues only logical cells that never acquired an attempt ID. A fixable
  harness defect requires a new full experiment ID and retains the abandoned experiment's
  attempts/costs as excluded evidence, so the 57/145/29/16 maxima apply per experiment ID rather
  than across abandoned runs.

## Open questions

- None blocking. The operator specified the requested architecture/tests; the low-risk choices not
  fixed by the design ledger are recorded above and are tested rather than assumed.

## Deviations log (fill during implementation)

- 2026-08-09: plan drafted; no implementation bytes changed.
- 2026-08-10: direct-read arms were tightened from unconstrained on-demand reads to four canonical
  per-file commands so required coverage and cross-handler isolation are observable rather than
  inferred. A real one-worker native canary passed the contract with GPT-5.5/medium root and
  GPT-5.4-mini/low worker.
- 2026-08-10: an ignored four-worker combined live canary proved effective GPT-5.5/medium root and
  planner, GPT-5.4-mini/low workers, exact-source delivery with zero reread violations, and one live
  planner call. A separate pinned/blinded semantic-judge canary passed at GPT-5.5/medium. These are
  implementation canaries; the sealed experiment still runs its own hierarchy canary before calibration.
- 2026-08-10: the first sealed experiment (`hierarchical-ablation-final-20260810T025739Z`) was
  abandoned before product cells after the analyzer rejected a valid sentence whose period preceded
  a closing curly quote. All nine physical attempts and three paid judge calls remain registered in
  ignored `abandoned.json`/`hierarchical-ablation-excluded.json` evidence. The grammar was corrected
  and regression-tested; per the preregistered harness-defect policy, the next experiment uses a new
  ID and reruns the complete hierarchy canary and calibration rather than replacing one cell.
- 2026-08-10: the second sealed experiment (`hierarchical-ablation-final2-20260810T031551Z`)
  stopped after its hierarchy canary and before calibration. Its root completed all four spawn
  calls before its first wait, three worker lifetimes overlapped, and every exact-source bundle was
  tool-denied after delivery, but the analyzer incorrectly required one common instant shared by
  all four worker transcript lifetimes. The one paid canary remains excluded and accounted for;
  the gate now enforces launch-before-wait ordering and records lifetime overlap only as an
  observation. The replacement again receives a new experiment ID and reruns the canary.
- 2026-08-10: the third sealed experiment (`hierarchical-ablation-final3-20260810T033113Z`)
  stopped before product cells after one of eight calibration calls produced a transcript without
  terminal usage. Seven cells and three blind paired judgments were valid, but the original
  all-eight rule made calibration inconclusive. The run remains excluded with its nine cell
  attempts and three paid judge calls fully accounted. This run is an **exploratory pilot** whose
  observed outcomes informed the attrition revision; it is not confirmatory evidence for the new
  rule. Before another call, calibration was revised to the same complete-pair attrition concept
  used by the product factorial: at least three of four randomized pairs, pairwise exclusion, no
  substitution, explicit position-stratified gates, and failed-attempt usage in the final report.
  The next prospective validation ID reruns the canary and all eight calibration calls; no result
  from this excluded pilot is reused. Claims from the next run remain bounded as an adapted
  prospective evaluation, not an independent confirmation of the original preregistration.
- 2026-08-10: the fourth sealed experiment (`hierarchical-ablation-final4-20260810T040349Z`)
  stopped after its canary and before calibration. One worker attempted its canonical handler read;
  the hook denied it, the transcript contained only the denial, and no source was delivered twice,
  yet the old analyzer invalidated all attempts rather than distinguishing enforced denial from a
  successful reread. The one paid canary remains excluded and accounted. The revised prospective
  protocol accepts only fully correlated canonical denials, reports their count, and still rejects
  any extra tool, malformed evidence, or returned source. A new ID reruns the complete canary and
  calibration without reusing this outcome.
