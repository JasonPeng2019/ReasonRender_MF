# RRCv2 — Lane A: The Solve Pipeline

**Mission:** turn one `Task` into test-passing code and a templatized spec, through the spec→implement→verify→repair→escalate loop, reaching models and memory *only* through injected ports. No Snowflake, no EverOS, no network — everything runs offline against fakes.

Read `RRCv2-contract.md` first (the frozen seam) and `RRCv2.md` §§1–6 for domain rationale. This file is your build sheet.

---

## In scope

- `solve()` — the whole control flow and arm routing (contract entry point).
- The four model stages (SPEC, PRIME, IMPLEMENT, repair): prompt construction, the `ModelPort.complete()` call, and strict JSON/code parsing.
- The three verify tiers (`ruff` → `pyright` (basic) → `pytest`) and the sandboxed executor.
- `templatize()`, `render()`, `structural_match()`, `choose_branch()`, and the Tier −1 `sanity_check()`.
- Assembling `SolveOutcome` incl. every `CostEvent` (built from `ctx`, `stage`, `model`, and the `Usage` the `ModelPort` returns).

## Out of scope (Lane B owns these — you only consume the ports)

- The model providers (`CodexModel` for prototyping, `CortexModel` for final), usage capture, query tags, and the `make_model` factory. You call `model.complete(role, ...)` by **role**, never by a provider-specific model name; the right provider is injected for you.
- EverOS, the own KV store, `external_ref` surfacing, read-your-write. You call `retrieval.retrieve/get_template/store`.
- The cost curve, hit-rate, reconciliation, workload generation, arms runner, demo.

---

## Files & namespaces (yours alone — no path collides with Lane B)

```
rrc/pipeline/__init__.py
rrc/pipeline/solve.py        # solve(): arm routing, retrieve->branch, repair/escalate
rrc/pipeline/stages.py       # spec_expensive, spec_prime, implement, repair (prompt+parse)
rrc/pipeline/verify.py       # run_ruff, run_pyright, run_pytest, verify (tier orchestration), sandbox
rrc/pipeline/template.py     # templatize, render, structural_match, choose_branch, sanity_check
rrc/pipeline/prompts.py      # build_spec_prompt, build_prime_prompt, build_implement_prompt, build_repair_prompt
rrc/pipeline/stubs.py        # FakeModel(ModelPort)  — your standalone dev dependency
tests/pipeline/...           # fixtures: a tiny hardcoded list[Task]; NOT Lane B's workload
```

Every function you own is namespaced under `rrc/pipeline/`. None of these names appear in Lane B. The only symbols you import from outside your tree are from `rrc.contract`.

---

## Functions you implement

```python
# solve.py  — the contract entry point
def solve(task, *, mode, model, retrieval, cfg, ctx) -> SolveOutcome
#   BASELINE     : strong model implements end-to-end; verify; no spec/memory
#   CHEAP_ALONE  : small model implements; verify+repair(N); no spec/memory
#   CASCADE      : cheap_alone; on final fail, strong model re-solves whole task (count both)
#   COLD         : _spec_path(task, retrieval=Null)  -> implement -> verify+repair -> escalate
#   WARM         : retrieve -> choose_branch -> {render | spec_prime | spec_expensive}
#                  -> sanity_check -> implement -> verify+repair -> escalate -> store

# stages.py  — every stage calls model.complete(ROLE, ...); ROLE, not a model name.
def spec_expensive(task, model, cfg, ctx) -> Spec           # ModelRole.STRONG, non-reasoning
def spec_prime(task, neighbors, model, cfg, ctx) -> Spec | None   # ModelRole.STRONG or SMALL; None == unfit
def implement(spec, model, cfg, ctx) -> str                 # ModelRole.SMALL, one pass
def repair(spec, code, tool_output, model, cfg, ctx) -> str # ModelRole.SMALL

# Each call returns a Completion; build CostEvent(arm=ctx.arm, task_id=ctx.task_id,
# stage=<stage>, model=c.model, usage=c.usage, provider=model.provider) and collect it.
# The repair/escalate loop stays entirely in solve() — never push it into a provider
# (invariant 7), so Codex and Cortex runs are structurally identical.

# template.py
def templatize(spec) -> Template            # deterministic; mints external_ref (fingerprint)
def render(template, task) -> Spec          # deterministic slot substitution
def extract_slot_values(task) -> dict        # see "the one risk" below
def structural_match(template, task) -> BranchDecision   # EXACT->REUSE, NEAR->PRIME, else MISS
def choose_branch(task, candidates, get_template, cfg) -> tuple[BranchDecision, Template | None]
def sanity_check(rendered_spec, task) -> bool   # Tier -1; loose (see below)

# verify.py
def verify(code, tests, cfg) -> VerifyResult   # ruff --fix -> pyright(basic) -> pytest, first fail wins
def run_ruff(code) -> VerifyResult
def run_pyright(code, cfg) -> VerifyResult
def run_pytest(code, tests) -> VerifyResult     # subprocess, wall-clock timeout, no network
```

## Original-spec sections you realize

§1 (pipeline control flow), §2 (all four prompts + the `slots`-emitting SPEC schema), §3 (all verify tiers + the Tier −1 sanity check and its accepted-risk caveat), §4.6 (`structural_match` / `choose_branch` policy, reading `cfg.tau_floor` / `cfg.prefer_prime_on_shape_diff`), §5 (you emit the `CostEvent`s the economics are computed from).

---

## Two things to get exactly right

**`sanity_check` must be maximally loose (RRCv2 §3).** It fires *only* when the rendered spec is structurally incoherent: an empty slot value, a signature that won't parse, or tests that don't reference the new function's name/arity. It must be a theorem that a "close enough" or even "semi-close" spec passes — it may only reject rendered garbage. Do not add any similarity/semantic judgement here; that would let it wrongly deny a plausible reuse. Real correctness is pytest's job.

**`extract_slot_values` is the one cost risk (RRCv2 §5).** Because the SPEC stage now emits its own `slots`, the *stored* template carries labelled placeholders, so `render()` is pure substitution. The open part is pulling the *new* task's values from `Task.text`. Implement it deterministically where the task text is structured; if a workload needs a small `small_model` call to fill slots, route it through `model.complete(..., stage="slotfill")` so the cost is captured like any other — and note in the outcome that REUSE incurred it. Keep it out of the REUSE "zero-model" path whenever the deterministic route works.

---

## Standalone development (you need nothing from Lane B)

Ship `FakeModel(ModelPort)` in `stubs.py`: return canned, deterministic `Completion`s (`provider="fake"`, `usage` zeros) keyed by `role`/`stage` — e.g. SPEC returns a valid `Spec` JSON for your fixture tasks, IMPLEMENT returns code that passes (and, for one fixture, code that fails once then passes, to exercise repair). Use the shared `NullRetrieval` for `retrieve/get_template/store`.

`FakeModel` is for fast offline **unit tests**. For real end-to-end **prototyping** the harness injects Lane B's `CodexModel` into your `solve()` unchanged (`RRC_MODEL_PROVIDER=codex`, the default) — you write no Codex-specific code, and nothing in `rrc/pipeline/` differs between the Fake, Codex, and Cortex runs.

With `FakeModel` + `NullRetrieval` you can run and assert:

- All five `ArmMode`s route correctly and terminate.
- Repair loop caps at `cfg.repair_cap_N`, then escalates; `escalated`/`repairs` recorded.
- CASCADE counts both the cheap attempts and the escalated strong solve in `cost_events`.
- `verify` returns on first failing tier; pytest runs in the sandbox with a timeout.
- `templatize` → `render` round-trips a spec; `structural_match` returns EXACT/NEAR/MISS on crafted templates; `sanity_check` rejects only crafted garbage and passes every plausible render.
- `SolveOutcome.template` is populated on COLD/WARM/MISS and `None` on BASELINE/CHEAP_ALONE.

You never import `rrc.memory`, `rrc.measure`, or `rrc.harness`. If you find yourself wanting to, it belongs behind a port — raise it as a contract sync-point instead.

---

## Clash-avoidance checklist

- Only import from `rrc.contract`; never from any `rrc.*` module Lane B owns.
- Don't touch `rrc/contract.py` except at an agreed sync-point.
- Keep all your code under `rrc/pipeline/`; keep fixtures under `tests/pipeline/`.
- Don't read `Task.oracle_tests` anywhere in a prompt (invariant 5).
- Build `CostEvent`s from `ctx`/`stage` plus the returned `Completion` (`model`, `usage`) and `model.provider`; don't format your own query tags (invariant 6 — that's the provider's job) and don't push the loop into a provider (invariant 7).

## Merge handoff

At merge, `FakeModel` is replaced by whatever `make_model(cfg)` selects — `CodexModel` during prototyping, `CortexModel` for the final metered run — and (for the warm arm) `NullRetrieval` by `EverOSRetrieval`, all injected into your `solve()` unchanged. Nothing in `rrc/pipeline/` should need editing across any provider; if it does, the contract was underspecified and that's the sync-point to fix.
