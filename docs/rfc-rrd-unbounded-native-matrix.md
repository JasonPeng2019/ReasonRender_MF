# PLAN — Unbounded native Codex product comparison

> **Historical / non-RRCv2 / superseded.** This experiment record is retained only for
> reproducibility; it is not current product guidance. ADR 0002 and `docs/RRCv2.md` govern the active
> implementation. The handler-audit matrix below is fixture-only/non-product.

## Goal

Run and report the complete controlled comparison requested by the operator: native Codex baseline
without ReasonRenderCoding or ContextMesh, combined local SQLite memory, and combined EverOS memory,
each on one-, two-, and four-worker handler-audit workloads. Remove token-based abort ceilings from
the active experiment contract, preserve exact observational usage evidence and quality checks, then
commit and push the complete migration and result report.

## Non-goals

- Do not restore OpenCode, Tollgate, a custom model provider, or any external model dependency for
  EverOS.
- Do not call provider-visible usage exact billing or causal savings.
- Do not commit raw Codex transcripts, credentials, generated homes, databases, or large run bundles.
- Do not run the two optional default-summary fidelity cells; the requested full comparison is the
  controlled 3 implementations × 3 worker-count matrix.
- Do not remove wall-clock liveness timeouts, exact worker-count validation, source-drift checks, or
  quality eligibility gates. Only token ceilings are removed.

## Constraints and operator override

- The operator explicitly removed the preregistered per-cell and aggregate token ceilings and asked
  for all nine controlled cells to run. There is therefore no token-triggered cancellation and no
  aggregate token budget.
- Each cell retains the earlier 12-minute wall timeout so a hung process cannot block the matrix
  forever. The operator's request for a full comparison explicitly supersedes the earlier
  first-invalid abort rule: all independent cells are attempted and invalid cells remain ineligible.
- All cells run sequentially in the existing Latin-square order to avoid backend concurrency and
  reduce ordering bias: baseline/local/EverOS for one worker, local/EverOS/baseline for two, and
  EverOS/baseline/local for four.
- Every cell uses the same resolved `codex-cli 0.147.0` binary/hash, native ChatGPT keyring login,
  model `gpt-5.5`, medium reasoning, effective service tier, stock-tool inventory, read-only
  sandboxing, approval policy, target bytes, and scenario prompt. The manifest hashes those values.
- Baseline runs with an empty `RRD_*`/`RRC_*` environment, disables the combined hooks through a
  strict command-line feature override, and overrides `agents.worker.description` with a neutral
  audit-only description. A poison test proves the baseline cannot read combined manifests,
  databases, hook logs, or packet text. Combined cells use the shipped hook, deterministic
  shared-file digests, one within-cell RRC MISS followed by exact reuse for remaining workers, and
  the selected SQLite or EverOS storage backend.
- EverOS is storage-only: assistant writes and keyword reads use the inert closed-loopback model
  sentinel already verified by the owned lifecycle smoke test.
- Run artifacts live under mode-0700 `contextmesh/runs/native-matrix-<UTC>/` directories with mode
  0600 metadata. Only a concise, source-linked result report is committed.

## Grounded implementation surface

- `contextmesh/bench/run_bench.py` currently writes only a nine-cell manifest or invokes a canary;
  it does not execute a cell.
- `contextmesh/scripts/rrd_demo_tui.sh` and `rrd_codex_hook.py` already provide the current native
  config, combined environment, seeding, RRC packets, ContextMesh delivery, result compression, and
  native transcript evidence for public four-worker runs.
- `contextmesh/scripts/rrd_combined_meter.py` is intentionally fixed to the public four-worker A/B
  demo and is not the matrix collector.
- The independently produced 23-finding rubric is currently preserved only in an ignored historical
  run. Before live calls M1 copies it byte-for-byte to tracked
  `contextmesh/bench/rubric-independent.json`, pins its SHA-256 and seven source hashes, and makes the
  runner fail closed on rubric/source drift.
- Native transcript `session_meta` rows bind a root thread to direct worker children; final
  `token_count` rows expose cumulative input, cached input, output, reasoning output, and total
  provider-visible tokens.

## Approach

Rewrite `contextmesh/bench/run_bench.py` as a resumable native matrix runner rather than adding a
second launcher. It freezes source/config/prompt/rubric hashes, holds an exclusive matrix lock for
the stable authenticated Codex home, runs the cell, binds the exact root to the
`thread.started.thread_id` in that cell's JSON stream, accepts only direct children whose
`session_meta.parent_thread_id` equals that ID, reconciles every additional new top-level transcript
to an RRC model-event thread ID, and fails the cell on unexplained transcript activity. It validates
combined hook/RRC evidence where applicable, scores the final report against the tracked rubric,
and atomically writes a cell summary plus aggregate JSON/Markdown report.

Baseline and combined cells share the exact scenario prompt. Baseline passes `features.hooks=false`
and derives lifecycle/usage from native transcripts and root JSON events. Combined cells prepare a
fresh backend-bound warm round and use the production hook; their planner usage is added from exact
RRC model-event rows. The runner continues to later independent cells after an invalid or timed-out
cell and labels that cell ineligible instead of manufacturing a zero. One canonical prompt renderer
receives the ordered handler list and emits exact one-, two-, or four-worker prompts; every combined
cell gets a fresh backend-bound round with `RRC_DEMO_MODE=warm`, while its expected packet pattern is
one MISS plus `N-1` HITs.

The quality metric is deliberately named **lexical rubric-match quality**, not semantic unsupported-
finding precision. Before live output the scorer freezes this contract:

- a claim is one nonblank bullet with exactly one severity in `critical|high|medium|low`, exactly one
  assigned `src/handlers/<name>.js:<start>[-<end>]` citation, and one indivisible sentence;
- an edge exists only when claim/rubric handlers and line ranges overlap, severities match exactly,
  and normalized claim text contains a full rubric alias or at least three non-stopword rubric
  evidence tokens; maximum-weight one-to-one matching prevents duplicates from increasing recall;
- lexical recall is matched rubric IDs divided by all scenario rubric IDs; lexical precision is
  matched claim lines divided by all parsed claim lines; duplicates and unmatched claims remain in
  the precision denominator; handler/category coverage require at least one matched ID for every
  in-scope handler/category;
- zero claims score zero; malformed claims invalidate the cell rather than disappearing; unmatched
  high/critical claims are reported explicitly;
- a combined cell is savings-eligible only if it is valid, recall is at least 70%, precision is at
  least 90%, all handler/category coverage passes, it has no unmatched high/critical claim, and its
  recall and precision are each no lower than the same-scenario baseline. Otherwise any lower token
  count is called only an observed reduction.

Fixtures freeze duplicate, conjoined true-plus-false, ambiguous alias, valid paraphrase, wrong-line,
wrong-handler, malformed, and severity-mismatch behavior. Because this lexical gate cannot prove
semantic truth, the report preserves unmatched text and states that manual security review remains
required.

**Alternative considered:** adapt the archived Ollama/OpenCode run-local harness. Rejected because
it writes custom-provider config, reads retired Tollgate rows, and cannot exercise native Codex
keyring authentication or current hooks.

**Simpler alternative considered:** manually run nine shell commands and transcribe totals. Rejected
because transcript attribution, source drift, quality scoring, restartability, and result hashing
would be unverifiable.

## Milestones

- [x] **M1 — Remove token ceilings and freeze the executable matrix contract.** Amend both active
  RFCs and README, copy the verified rubric to a tracked immutable input, and add tests proving the
  manifest has nine cells, no token budget/ceiling field, the Latin-square order, neutral baseline
  profile/environment, exact binary/config/tool hashes, and a wall timeout only. **Acceptance:**
  documentation and CLI help contain no active token-abort rule; old historical outcomes remain
  clearly historical; rubric/source drift fails closed.
- [x] **M2 — Implement native cell execution and evidence collection.** Add baseline and combined
  launch paths, transcript attribution, per-cell source/config/prompt hashes, exact usage parsing,
  backend lifecycle cleanup, resumable cell state, and evidence validation. **Acceptance:** offline
  fixtures prove one/two/four transcript attribution, injected unrelated root and planner transcript
  reconciliation, malformed/missing usage rejection, combined MISS/HIT validation, exact scenario
  prompt/environment rendering, and no baseline hook intervention.
- [x] **M3 — Implement deterministic quality and aggregate reporting.** Parse severity-tagged
  file:line claims, match only scenario-relevant rubric findings using the frozen lexical contract,
  report recall/precision/severity agreement and token/latency deltas, and refuse a savings label
  when a combined cell is invalid or quality-inferior. **Acceptance:** all listed lexical mutation
  fixtures pass and the report never labels lexical precision as semantic adjudication.
- [x] **M4 — Verify and execute all nine cells.** Run deterministic gates and native canary, then one
  `run_bench.py --run-matrix` invocation for all nine cells in the declared order without token
  cancellation. **Acceptance:** each attempted cell has an atomic summary and raw hashes; the
  aggregate reports every cell as valid, invalid, or timed out, never silently absent.
- [ ] **M5 — Review, commit, and push.** Write the concise committed result report, run full static,
  test, secret, and adversarial gates, deliberately stage source/docs/tests/results summary, commit,
  and push the current branch. **Acceptance:** `git status` is clean apart from ignored run artifacts,
  the remote branch contains the commit, and the final response links the commit and reports honest
  findings.

## Definition of done

- [ ] No active matrix code or contract cancels a run based on token count.
- [ ] Exactly nine controlled cells cover all three implementations and worker counts 1, 2, and 4.
- [ ] All cells use native Codex authentication/model configuration and identical scenario inputs.
- [ ] Baseline has no RRC/ContextMesh intervention; combined cells prove backend-bound RRC and
  ContextMesh delivery with the expected within-cell MISS/HIT pattern.
- [ ] Provider-visible root, worker, and planner usage is exact, nonnegative, source-bound, and
  reported without subtracting cached input or claiming exact billing.
- [ ] Quality metrics are traceable to the frozen independent rubric, and token savings are named
  only for valid, quality-eligible comparisons.
- [ ] EverOS startup/smoke/teardown and local no-service behavior pass.
- [ ] Full pytest, Ruff, Pyright, Bash, ShellCheck, diff, active legacy scan, and staged secret scan
  pass on final bytes.
- [ ] A fresh adversarial diff review is SHIP, the changes are committed, and the branch is pushed.

## Verification plan

- Focused: `uv run --locked pytest -q -p no:cacheprovider tests/test_rrd_product_matrix.py
  tests/test_rrd_demo.py tests/test_rrd_native_codex.py`.
- Full: `uv run --locked pytest -q -p no:cacheprovider`.
- Static: Ruff check/format, Pyright, `bash -n`, ShellCheck, `git diff --check`, and active legacy
  dependency scan.
- Live: native local canary; EverOS owned up/smoke/down; then one unbounded
  `run_bench.py --run-matrix` invocation that executes nine cells.
- Evidence: rerun `run_bench.py --report <run-dir>` from sealed cell summaries and compare report
  hashes.

## Risks and one-way doors

- **Unbounded provider usage:** removal of token ceilings is intentional and explicitly authorized.
  The remaining wall timeout bounds hangs, not provider tokens; a completed cell can consume any
  amount permitted by the account.
- **Rate limits:** sequential execution may pause or fail if the account rate limit is reached. The
  runner preserves completed cells and can resume without rerunning them.
- **Measurement bias:** one replicate per cell is exploratory, not causal. Latin-square order and
  identical inputs reduce but do not eliminate variance.
- **Lexical quality scoring:** deterministic scoring is reproducible but can undercount valid
  paraphrases or miss an unsupported conjunct and cannot replace human security review. It is named
  lexical throughout; raw reports and unmatched claims remain in artifacts.
- **Push:** pushing is an externally visible, reversible Git operation explicitly requested by the
  operator. No force push or history rewrite is permitted.

## Design clarification

The design ledger has no entry for `contextmesh/bench/run_bench.py`. The operator resolved the only
material ambiguity by explicitly requesting removal of preregistered token ceilings and completion
of the full comparison. The controlled nine-cell matrix, rather than optional fidelity cells, is the
smallest interpretation that covers every implementation, task size, and multi-agent setup they
named.

## Deviations log

- 2026-08-09: A preliminary execution exposed a relative final-message path and a `local`/`sqlite`
  launcher alias mismatch. That attempt was excluded, both defects received regressions, owned
  EverOS state was removed, and the matrix restarted in a fresh run directory.
- 2026-08-09: The final-protocol controlled matrix finished with 9/9 protocol-valid cells and
  1,628,513 provider-visible tokens. Neither combined implementation reduced tokens relative to
  baseline. Earlier complete pre-hardening runs used 1,468,090 and 1,661,034 tokens and are retained
  only as directional corroboration because their protocol and source bytes differ.
  The independently recorded research-claim review returned `SUPPORTED` for that limited,
  observational conclusion and rejected causal/general efficiency interpretation.
- 2026-08-09: After model execution, a report-only fix separated quality eligibility from a positive
  savings label and explained protocol validity. The exact executed runner bytes were reconstructed
  and hash-verified against the frozen experiment manifest; no cell evidence or score changed.
- 2026-08-09: Final adversarial review added fail-closed duplicate/conflicting planner-final
  validation plus whole-process-group EverOS rollback and teardown. The final3 executed hashes remain
  frozen and disclosed; the three post-run active-file hashes and source drift are recorded in the
  result report. All six final3 planner logs pass the stricter parser, while lifecycle behavior is
  covered by subprocess regressions. No sealed output, usage total, or quality score changed.
