# PLAN — Live RRD product comparison matrix

> **Superseded experiment contract (2026-08-09):** the controlled native-Codex matrix is now
> governed by `docs/rfc-rrd-unbounded-native-matrix.md`. The earlier Ollama transport, optional
> fidelity cells, token ceilings, polling watchdog, and first-invalid abort below are retained only
> in the dated deviations log as historical evidence; they are not active requirements.

## Goal

Run a live, provider-backed comparison of three implementations on the same checked-in target:

1. Codex native multi-agent baseline with no ReasonRenderCoding transformation or ContextMesh;
2. combined local memory (SQLite RRC index + sealed-file ContextMesh); and
3. combined EverOS memory (EverOS RRC index + EverOS ContextMesh).

Exercise one-, two-, and four-worker audit workloads, preserve exact provider usage and product
evidence, validate that each requested worker completes, and report results with setup and blinded
quality scores rather than presenting a single exploratory matrix as a causal benchmark. Add one
bounded default-summary four-worker validation cell for each combined backend so the controlled
storage matrix is not mislabeled as complete default-product coverage.

## Non-goals

- Do not generalize RRC beyond its current HTTP-handler audit contract.
- Do not claim support or savings for unrelated feature, refactor, documentation, or research task
  shapes; the "different tasks" in this matrix are different handler-audit sets within the shipped
  product domain.
- Do not change production launcher, hook, proxy, meter, or RRC code merely to run the experiment.
- Do not compare OpenCode and Codex; every matrix arm uses installed Codex 0.147.0 and the same
  configured Ollama model.
- Do not claim a causal backend difference, statistical significance, or quality equivalence from
  one replicate.

## Constraints and grounded wiring

- The product hook accepts exactly one `src/handlers/*.js` path per worker and canonicalizes every
  accepted assignment to the same audit case shape. The valid scaling test is therefore one, two,
  or four handler workers, not arbitrary prompt classes.
- The checked-in meter requires exactly four overlapping workers. A separate experiment collector
  must read exact Tollgate rows and evidence for one/two-worker runs without weakening production
  READY semantics.
- The baseline uses the same target bytes, root prompt obligations, model, provider, sandbox, and
  maximum concurrency, but has no RRC/ContextMesh transformations and routes directly through RRD
  Tollgate. Its neutral observer-only worker profile necessarily differs from the combined product's
  shipped RRC/ContextMesh-aware description; the exact condition-specific profile text and hash are
  frozen and reported rather than mislabeled as identical.
- Combined arms use the production bundled hook, response proxy, RRC resolver, worker profile, and
  target. Every cell receives a unique round ID, target copy, manifest, database/lock, RRC evidence
  files, and EverOS project/session namespace. The harness proves that fresh namespace has no case
  hit before launch, so the first assignment is a planner MISS and later same-shape assignments may
  HIT.
- To compare the shipped local and EverOS memory implementations, both combined backends seed the
  production hook with
  `RRD_SUMMARY_MODE=deterministic`. This yields byte-identical current digests from identical source
  bytes without a stochastic summarizer. It is a supported control path, not the default live
  summary path; setup-model tokens are therefore zero in the controlled matrix and must be reported
  separately from earlier default-summary measurements.
- Live worker, root, result-compression summarizer, and RRC planner calls use the configured Ollama
  model through exact Tollgate session partitions. Two additional `four-all-default` validation
  cells use the ordinary model-backed seed summarizer and production worker profile for local and
  EverOS. Their prompts require detailed reports and they are valid full-product cells only if
  setup summarizer and result-compression evidence are both present. No credential value is written
  to artifacts.
- The real EverOS assistant-only add/keyword-search path and the real local SQLite index are used.
- Runs are sequential to avoid provider-concurrency interference. A preregistered Latin-square
  implementation order is used across scenarios: baseline→local→EverOS for one worker,
  local→EverOS→baseline for two, and EverOS→baseline→local for four. This requires backend
  switches while placing every implementation first, second, and third once. Cache usage and request
  timestamps are recorded. A single replicate still cannot identify a backend or product effect, so
  the report uses only order-qualified observed deltas and does not attribute them causally.
- This is an exploratory single replicate per cell. Every total is an observed cell value; only
  mechanism identities supported by packet/digest evidence may be attributed.

## Design-bank clarification

- Design queries for token benchmarking, `rrd_demo_tui.sh`, and `rrc/multiagent_demo.py` returned no
  matching canon entries.
- The user explicitly requested live paid product testing across backends, tasks, and multi-agent
  setups. Assumption: a minimal 3 implementations × 3 supported workload matrix is preferable to
  an open-ended high-token sweep.
- Assumption: "different tasks" means different handler-audit sets inside the current product
  contract. Expanding to unrelated task shapes would be a product redesign, not an evaluation.

## Approach

Create an immutable run directory under `contextmesh/runs/rrd-product-matrix-<timestamp>/` with a
resolved config, target hashes, tool versions, prompts, per-cell stdout/stderr, exact evidence, and a
machine-readable report. Before calls, copy and hash every executed launcher, hook, proxy, RRC
module, config, the full tracked diff, and relevant untracked source. Finalize each cell through an
atomic append-only receipt. A run-local Python harness will invoke existing production scripts and
Codex headlessly; it is an experiment artifact, not production source. Immediately before and after
every cell it re-hashes every live launcher/module/config against the frozen manifest and aborts on
drift, including RRC modules imported from the live repository.

The matrix is:

| scenario | assigned handlers | workers | schedule |
|---|---|---:|---|
| `single-users` | users | 1 | one worker |
| `pair-users-products` | users, products | 2 | parallel |
| `four-all` | users, products, orders, reviews | 4 | parallel |

For each scenario run baseline, combined-local, and combined-EverOS once in the Latin-square order
above. These are nested workloads,
but handler complexity still changes, so totals are workload-specific descriptions rather than a
causal concurrency curve. Validate exact token rows, expected handler mentions, successful Codex
exit, backend-bound combined evidence, and expected RRC MISS/HIT counts `(1/0)`, `(1/1)`, and
`(1/3)`. Keep invalid or incomplete cells in the report but exclude them from savings claims.

The baseline has no ReasonRenderCoding or ContextMesh transformation. It does use a run-local
**non-intervening observer hook** that returns `{}` and only records exact spawn input, spawn result,
SubagentStart/Stop agent IDs and message hashes, wait input/result, and root final hash. Before the
matrix, a provider-backed single-worker pilot must show that this observer neither rewrites prompts
nor adds context and can prove the exact handler set, unique agent IDs, terminal completion, waits,
and `fork_context=false`. Cells without complete observer evidence are invalid.

Every arm also seals its Codex JSONL/session trace. For exactly `N` requested workers it must contain
exactly `N` `spawn_agent` calls naming the expected one-handler assignments with
`fork_context=false`; all spawn calls must precede the first wait. Hook start/stop timestamps (or the
observer equivalents) must prove `max(start_ts) < min(stop_ts)` for `N > 1`. Codex JSONL/session
tool-call input is the combined-arm authority for `fork_context`, because the production assignment
event does not record that field. The pilot aborts the matrix if the installed CLI does not expose
this proof. Completion evidence alone never qualifies as a parallel multi-agent run.

Before seeing outputs, freeze a handler-by-handler ground-truth rubric from the checked-in source:
concrete supported findings, category, severity band, and file:line range. Afterward randomize arm
labels and score finding recall, unsupported-finding precision, category coverage, and handler
coverage. A delta may be called token **savings** only when the combined cell has at least 70%
supported-finding recall, at least 90% precision, covers every rubric category present in that
scenario, has recall and precision no lower than its scenario baseline, and adds no unsupported
high-severity claim. Equal zero recall can never qualify, and unsupported low/medium findings count
against precision. Otherwise it is only an observed token reduction with the quality delta shown.

**Alternative rejected — three unrelated task classes.** The current RRC policy always converts an
accepted handler assignment into the same security-audit packet; claiming different feature or
refactor tasks would compare different effective work.

**Alternative rejected — independent model-generated digests per backend.** That confounds storage
with digest text. Deterministic product digests isolate storage while still exercising current-source
binding and delivery.

**Alternative rejected — two or more replicates immediately.** It would roughly double paid usage
before basic full-product feasibility is known. The first matrix is exploratory; repeat only valid
cells in a follow-up if variance-supported claims are needed.

## Milestones

- [ ] **M1 — freeze experiment contract and rubric.** Record git/worktree state, executed-source
  copies/hashes, target hashes, model, CLI/runtime versions, exact prompts, per-cell round/session/index
  namespaces, counterbalanced order, and the blinded finding rubric. **Acceptance:** resolved config
  contains no API key; every code/target/prompt input is hashed; every combined cell has a unique empty
  index namespace; the rubric is frozen before output exists.
- [ ] **M2 — dry run and three-cell pilot.** Validate commands/configs without a provider call, then
  run `single-users` baseline, local, and EverOS. **Acceptance:** the baseline observer proves one
  non-rewritten worker lifecycle; combined evidence proves one MISS, current digest delivery, root
  merge, and backend identity; teardown/restoration succeeds. Abort the remaining matrix on the first
  invalid cell.
- [ ] **M3 — remaining controlled matrix.** Execute the counterbalanced two- and four-worker cells
  with deterministic digests. **Acceptance:** every cell has exact sessions, output, observer or
  combined lifecycle evidence, exact digest equality/read receipts, expected MISS/HIT counts, and an
  atomic final receipt—or the matrix aborts without further paid calls.
- [ ] **M4 — default-product fidelity.** Run one fresh four-worker default-summary cell per combined
  backend. **Acceptance:** live seed-summarizer usage; six current digest records because the
  production seed command initializes both arms, with the three arm-b records actually delivered;
  four valid shared-context deliveries; 1/3 RRC reuse; one root merge; and zero hook/RRC/proxy
  fail-open or policy-deny events. Every worker response above the frozen 2,000-character production
  threshold must have receipt/hash-correlated result compression; below-threshold responses are
  recorded as legitimate bypasses, and at least one worker must exceed the threshold and compress.
  These cells are reported separately from the storage-controlled matrix.
- [ ] **M5 — blinded analysis and claim review.** Produce authoritative raw/setup-separated totals,
  latency, cache fields, workload-specific observations, local-vs-EverOS order-qualified deltas,
  baseline deltas, and blinded rubric scores. **Acceptance:** every number traces to an artifact;
  invalid cells are excluded; `research-claim-review` returns no unaddressed blocker.

## Definition of done

- [ ] A validated three-cell pilot precedes the remaining spend; nine controlled and two fidelity
  cells are attempted only while every preceding evidence contract remains valid.
- [ ] Baseline has ContextMesh/RRC disabled and only a proven non-intervening observer hook; combined
  controlled cells use production delivery/proxy/RRC with deterministic seed summaries; fidelity
  cells additionally use the default live seed summarizer and production profile.
- [ ] Local sealed-file ContextMesh and EverOS ContextMesh receive byte-identical deterministic
  digest text/hashes for identical target bytes, both read paths match those exact hashes, and each
  arm's separate RRC index backend is evidenced.
- [ ] One/two/four worker setups request exactly the declared handler set and combined evidence
  confirms 1/0, 1/1, and 1/3 RRC MISS/HIT behavior.
- [ ] Provider rows are exact and partitioned; planner, outer, summarizer, and setup totals are never
  double-counted or silently omitted.
- [ ] Baseline observer evidence proves the exact spawn/agent/wait/final lifecycle and combined hook
  evidence proves the analogous lifecycle. Sealed Codex tool-call input proves exactly `N` spawns,
  all-before-wait ordering, `fork_context=false`, and temporal overlap for `N > 1` in every arm.
- [ ] Final reports are blindly scored against the preregistered source-grounded rubric. Token
  savings language is used only under the declared non-inferiority rule.
- [ ] The report distinguishes measured cell deltas, deterministic-digest control conditions,
  historical default-summary setup costs, and unsupported generalization.
- [ ] Dedicated services and state are cleaned up, and the pre-existing ordinary stack is restored.

## Verification plan

- Before provider calls: run the full deterministic gate, a native local canary, validate every
  resolved command/config, and verify the tracked source-grounded rubric and target hashes.
- Per cell: start Codex in a fresh process group and enforce only the 12-minute wall timeout. There
  is no token watchdog, per-cell token ceiling, aggregate token budget, or token-triggered abort.
- Attempt all nine independent controlled cells in the declared Latin-square order. A failed or
  timed-out cell is preserved as invalid and does not prevent later cells from running.
- Per cell evidence: exact native transcript identities and provider-visible usage, exit and timeout
  state, final-message hash, root/worker lifecycle correlation, handler coverage, cache-read fields,
  backend identity, RRC branch evidence, sealed source hashes, and artifact receipts.
- Post matrix: recompute aggregate JSON/Markdown from atomic cell summaries, compare regenerated
  report hashes, run the full static/test/secret gates, and submit result claims to independent
  review before reporting.

## Risks and one-way doors

- **Provider usage:** all nine controlled cells are intentionally attempted with no token-based
  cancellation. Native login offers no pre-dispatch reservation, so usage is unbounded by this
  harness; the retained 12-minute wall timeout bounds hangs but not tokens. One replicate remains
  exploratory rather than causal.
- **Provider nondeterminism:** one replicate cannot establish causality or variance. Report observed
  deltas and mechanism evidence only.
- **Quality:** filename coverage is a smoke check, not a correctness score. The preregistered blinded
  rubric and absolute/relative gates are required before savings language; one sample still does not
  establish general equivalence.
- **EverOS teardown:** the production EverOS `down` also stops the ordinary stack. The harness
  captures exact initial health/PIDs, installs INT/TERM/HUP handlers, launches each cell in its own
  process group, and uses `finally` to stop the selected RRD backend. It then restores the ordinary
  stack to its captured up/down state and asserts dedicated state/ports are absent after every switch.
- **Interactive behavior:** headless `codex exec` uses the same generated CODEX_HOME and hooks as the
  TUI but does not test terminal rendering.
- **Task scope:** the product is currently specialized to handler audits; results cannot be extended
  to arbitrary coding tasks without new policy and evaluation work.

## Deviations/results log

- 2026-08-08: Initial plan created. No matrix cells have run yet.
- 2026-08-08: First adversarial review BLOCKED deterministic-only “full product” language,
  cross-cell index leakage, missing baseline lifecycle evidence, no quality gate, paid-spend sequencing,
  fixed backend order, dirty-source reproducibility, and underspecified cleanup. The revision adds
  default-summary fidelity cells, per-cell namespaces, a non-intervening baseline observer, a frozen
  blinded rubric, pilot/abort ceilings, counterbalanced ordering, executed-source sealing, and
  signal-safe restoration.
- 2026-08-08: Second adversarial review BLOCKED a zero-quality savings loophole, missing proof of
  parallel spawn semantics, and partial compression/fail-open acceptance. It also requested balanced
  three-arm ordering, exact profile/backend labels, live-source drift checks, an explicit authoritative
  token source, and the true aggregate ceiling. The revision adds absolute recall/precision/category
  gates, Codex tool-call and overlap requirements, all-eligible compression with zero fail-open,
  Latin-square ordering, component-specific labels, pre/post source checks, Tollgate authority, and
  the 2.11M-token cap.
- 2026-08-09: Final plan adversarial verdict was SHIP. The full deterministic repository gate passed
  (`214 passed in 160.94s`), the run-local harness/observer passed syntax/lint/dry checks, the
  source-only 23-finding rubric was sealed before provider output, and the live 16-token Ollama
  canary passed.
- 2026-08-09: M2 aborted at the first live baseline cell as required. The configured model reported
  and behaved as though Codex 0.147's legacy `multi_agent_v1` tool had an empty schema; the outbound
  tool-definition payload was not captured. The model's attempted function calls returned
  `unsupported call: multi_agent_v1`; no worker
  started, no final report existed, one Tollgate row was inexact after cancellation, and the
  watchdog terminated the cell after 86,807 exact tokens (80,000-token ceiling overshoot from a
  completed request). No controlled combined/backend cells were launched.
- 2026-08-09: A separately labeled bounded v2 compatibility diagnostic also failed: attempted
  `collaboration` and `spawn_agent` function calls were rejected as unsupported. It completed with
  an explicit failure message after 45,861 exact tokens. This localizes the blocker to custom
  collaboration protocol/dispatch in the tested Codex 0.147 + `deepseek-v4-flash:cloud` + Ollama
  Responses pairing, before any RRC/ContextMesh/backend hook can run. The preserved missing-metadata
  warning and absent outbound tool-definition capture mean the run does not isolate Codex, the model,
  or the provider as the sole cause. Per the preregistered abort rule, the requested SQL/EverOS token
  comparison and savings claims are not measurable from this run.
