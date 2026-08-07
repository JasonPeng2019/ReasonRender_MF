# RRCv2 — Lane B: Memory, Measurement & Harness

**Mission:** make the pipeline *warm* and *measured*. Provide the two ports Lane A consumes (models via Cortex, memory via EverOS + own store), then run the five arms over a repeating workload and produce the money-shot: cost-per-solved falling below the cascade/cheap-alone floors as the library fills.

Read `RRCv2-contract.md` first (the frozen seam) and `RRCv2.md` §§4, 6, 7, 9 for domain rationale. This file is your build sheet.

---

## In scope

- **Model providers + `make_model` factory:** `CodexModel(ModelPort)` — the prototyping/integration runner, **default** (`RRC_MODEL_PROVIDER=codex`); and `CortexModel(ModelPort)` — **final metered runs only**, `AI_COMPLETE(..., show_details => TRUE)`, sets `QUERY_TAG = ctx.tag(stage)`, reads inline `usage`. Both return a `Completion` and answer a single call (no loop — invariant 7). `make_model(cfg)` picks one from the env switch.
- **`EverOSRetrieval(RetrievalPort)`** — EverOS HTTP client (`/api/v2/memory/*`), RRCv2's own KV store, `external_ref` surfacing, read-your-write.
- **The EverOS `external_ref` fork patch** (separate repo — see below).
- **Measurement:** the live cost-per-solved curve + hit-rate from `SolveOutcome.cost_events`; post-hoc reconciliation against `CORTEX_AI_FUNCTIONS_USAGE_HISTORY` by query tag; the price table.
- **Workload generator** (repeating families) and the **arms runner** that calls Lane A's `solve()` per task per arm.
- The 3-minute demo wiring (pre-baked warm-up + three live credibility beats).

## Out of scope (Lane A owns — you only call `solve()`)

- The solve control flow, stages, verify tiers, repair/escalate, templatize/render/structural/sanity. You never re-implement the loop; you supply ports and iterate.

---

## Files & namespaces (yours alone — no path collides with Lane A)

```
rrc/memory/__init__.py
rrc/memory/everos_client.py  # everos_add, everos_flush, everos_search, wait_searchable
rrc/memory/store.py          # kv_put, kv_get  (SQLite; store of record, keyed by external_ref)
rrc/memory/retrieval.py      # EverOSRetrieval(RetrievalPort): retrieve, get_template, store
rrc/memory/fake_everos.py    # FakeEverOS  — in-proc, so retrieval is testable w/o a server or the patch
rrc/models/__init__.py
rrc/models/factory.py        # make_model(cfg) -> ModelPort   (reads RRC_MODEL_PROVIDER)
rrc/models/codex.py          # CodexModel(ModelPort): prototyping runner; role->Codex model
rrc/models/cortex.py         # CortexModel(ModelPort): FINAL only; AI_COMPLETE + usage + query tag
rrc/measure/__init__.py
rrc/measure/reconcile.py     # reconcile_costs() against the account-usage view
rrc/measure/curve.py         # cost_per_solved(), hit_rate(), plot_curve()  (gate on provider)
rrc/measure/prices.py        # per-model credit->cost table; apply identically across arms
rrc/harness/__init__.py
rrc/harness/workload.py      # gen_workload(): repeating task families -> list[Task]
rrc/harness/arms.py          # run_arm(), run_all_arms()
rrc/harness/demo.py          # pre-bake + three live beats
rrc/harness/fake_solve.py    # FakeSolve — stand-in for Lane A's solve() during dev
everos-fork/                 # SEPARATE REPO: the external_ref patch (cannot clash with rrc/*)
tests/memory/... tests/harness/...
```

Everything you own is under `rrc/memory/`, `rrc/measure/`, `rrc/harness/`, or the separate `everos-fork/`. None of these names or paths appear in Lane A. The only symbols you import from Lane A are `solve` and `ArmMode`/types — and those only in `arms.py`, only at the end (stub them with `FakeSolve` until then).

---

## Functions you implement

```python
# models/factory.py  — the whole swap is here
def make_model(cfg) -> ModelPort:
    # env RRC_MODEL_PROVIDER (fallback cfg.model_provider):
    #   "codex"  -> CodexModel()    (default; prototyping + integration)
    #   "cortex" -> CortexModel()   (final metered runs only)
    #   "fake"   -> FakeModel()     (Lane A's stub; unit tests)

# models/codex.py
class CodexModel:                                   # implements ModelPort; provider = "codex"
    def complete(self, role, prompt, ctx, stage) -> Completion:
        ...  # map role -> Codex model (STRONG/SMALL names live HERE, env-overridable)
             # run ONE Codex completion (Codex CLI exec, or OpenAI Codex/Responses API)
             # usage: pass through if Codex returns it, else Usage(0,0,0)
             # return Completion(text, usage, model);  NO multi-turn / NO repair loop (invariant 7)

# models/cortex.py  — final only
class CortexModel:                                  # implements ModelPort; provider = "cortex"
    def complete(self, role, prompt, ctx, stage) -> Completion:
        ...  # map role -> Cortex model (e.g. STRONG='claude-3-5-sonnet', SMALL='llama3.1-8b')
             # ALTER SESSION SET QUERY_TAG = ctx.tag(stage)
             # SELECT AI_COMPLETE(model, prompt, show_details => TRUE)
             # parse usage -> Usage(prompt, completion, total); return Completion(text, usage, model)

# memory/everos_client.py
def everos_add(session_id, messages, external_ref, cfg) -> None      # POST /add (+ external_ref)
def everos_flush(session_id, cfg) -> None                             # POST /flush (sync md write)
def everos_search(query, cfg) -> list[Candidate]                      # POST /search: user_id=cfg.agent_identity,
                                                                      # method="hybrid", top_k, min_score=cfg.tau_floor
def wait_searchable(cfg, samples=2) -> None                           # poll GET /health cascade.pending==0 x2

# memory/store.py
def kv_put(external_ref, template) -> None
def kv_get(external_ref) -> Template | None

# memory/retrieval.py
class EverOSRetrieval:                               # implements RetrievalPort
    def retrieve(self, task, cfg) -> list[Candidate]         # everos_search -> [{external_ref, score}]
    def get_template(self, external_ref) -> Template | None  # kv_get
    def store(self, task, template, outcome) -> None         # kv_put THEN everos_add+flush(external_ref)

# measure/reconcile.py
def reconcile_costs(tag_prefix) -> dict                # CORTEX_AI_FUNCTIONS_USAGE_HISTORY by QUERY_TAG
# measure/curve.py
def cost_per_solved(outcomes, prices) -> list[float]   # ordered by task; every call counted
def hit_rate(outcomes) -> list[float]                  # running; hit == branch in {REUSE,PRIME} and not escalated
def plot_curve(...) -> None
# harness/workload.py
def gen_workload(seed) -> list[Task]                   # repeating families; carries oracle_tests
# harness/arms.py
def run_arm(tasks, mode, model, retrieval, cfg) -> list[SolveOutcome]
def run_all_arms(tasks, cfg) -> dict[str, list[SolveOutcome]]
```

## Original-spec sections you realize

§4 (all of the EverOS integration: §4.1 real API shapes, §4.2 two stores, §4.3 `external_ref` patch + fallback, §4.4 episode track for the demo, §4.5 HTTP client, §4.6 the `min_score` floor and passing candidates to Lane A's `choose_branch`, §4.7 read-your-write), §6 (arms runner, hit-rate, pass@1 parity reporting, cost-per-solved), §7 (Cortex `show_details` inline usage, query-tag reconciliation, prices, warehouse notes), §9 (demo).

---

## Three things to get exactly right

**`retrieve()` returns index data only (invariants 1–2).** Return `[{external_ref, score}]` and nothing else the pipeline treats as truth. The `Template` always comes from `get_template()` → your KV store. Never hand Lane A the EverOS-paraphrased content.

**`store()` order matters.** Write the KV store *first* (it's the store of record), then `everos_add` + `everos_flush` with the same `external_ref`. If the EverOS write lags or fails, retrieval simply misses that ref later — which Lane A treats as a MISS, not an error (§4.8).

**The MISS floor is server-side.** Pass `min_score = cfg.tau_floor` on `/search`; don't build adaptive thresholding — the score is a bounded `[0,1]` fused value and Lane A's deterministic `structural_match` makes the REUSE/PRIME decision. You just hand it the surviving candidates.

### The EverOS fork patch (isolated by construction)

Lives in `everos-fork/`, a different repo from `rrc/*`, so it cannot clash with Lane A. It adds `external_ref` as a correlation id: accepted on `/add`, persisted in frontmatter + a LanceDB scalar column (never sent through the extraction LLM), copied onto every memory a single `/add` fans out into, and surfaced on every `Search*Item`. Until it lands, develop `EverOSRetrieval` against **`FakeEverOS`**, and/or use the zero-patch fallback (unique `session_id` per task + id-capture via `/get`). Swapping fallback→patch is a one-line change in `everos_search`.

---

### Prototype on Codex, finalise on Cortex (the swap)

`make_model` is the entire seam between the two backends — the swap is one env var, no path or code edits:

1. **Prototype (default).** `RRC_MODEL_PROVIDER=codex`. Run all five arms end-to-end; iterate on the pipeline, retrieval, thresholds, and the harness until arms pass *functionally* (branches fire, reuse/prime/miss route correctly, verify+repair behave, the warm arm shows hits appearing). The curve code runs but must **not** be read as an economic result (invariant 8) — Codex `usage` is best-effort.
2. **Gate.** Only once the Codex run is green do you move on. Treat "green on Codex" as "the plumbing and logic are correct," not as final pass@1 or cost.
3. **Finalise.** `RRC_MODEL_PROVIDER=cortex`. Re-run the arms for the metered numbers: inline `usage` drives the live cost-per-solved curve, `QUERY_TAG` reconciles against `CORTEX_AI_FUNCTIONS_USAGE_HISTORY`, and `prices` converts to one axis. Re-confirm pass@1 here — model behaviour differs from Codex, so correctness and the economics are only authoritative on this run.

Keep `STRONG`/`SMALL` model names inside each provider (env-overridable), never in the shared `Config`, so switching providers can't ripple into Lane A.

## Standalone development (you need nothing from Lane A)

Ship `FakeSolve` in `harness/fake_solve.py`: same signature as `solve()`, returns a deterministic `SolveOutcome` + `Template` (and synthesises a few `CostEvent`s) driven by a scripted branch pattern — e.g. first occurrence of a family → MISS, later occurrences → REUSE/PRIME. With `FakeSolve` + `FakeEverOS` you can build and assert:

- `EverOSRetrieval` round-trips: `store()` then `retrieve()` returns the ref with a score; `get_template()` returns the stored `Template`; a missing ref yields no candidate.
- `wait_searchable` polls health correctly (two consecutive zeros).
- `cost_per_solved` counts *every* `CostEvent` and bends downward as scripted hits rise; `hit_rate` uses the invariant-4 definition.
- `run_all_arms` produces five series; cold arms use `NullRetrieval`, warm uses `EverOSRetrieval`.
- pass@1 parity check: warm pass@1 stays within baseline's noise band (§6).
- `make_model` returns the right provider for each `RRC_MODEL_PROVIDER` value; `CodexModel.complete()` and `CortexModel.complete()` each return a `Completion` with `provider` set, and `CortexModel` (against a recorded/mock SQL executor) sets `QUERY_TAG` equal to `ctx.tag(stage)` verbatim. Both map `ModelRole` → a concrete model without Lane A knowing the names.

You never import `rrc.pipeline`. `arms.py` imports `solve` only at the final wiring step; until then it imports `FakeSolve`.

---

## Clash-avoidance checklist

- Only import from `rrc.contract` (plus `solve`/types in `arms.py` at the end); never reach into `rrc/pipeline/` internals.
- Don't touch `rrc/contract.py` except at an agreed sync-point.
- Keep code under `rrc/memory/`, `rrc/measure/`, `rrc/harness/`; keep the patch in `everos-fork/`.
- Format the query tag *only* via `RunContext.tag()` (invariant 6); reconciliation must parse that exact format.
- Compute hit-rate the one contract way (invariant 4); don't invent a second definition in the demo.

## Merge handoff

At merge, `arms.py` swaps `FakeSolve` → Lane A's real `solve`, injects `make_model(cfg)` as `model` for every arm (Codex while prototyping, Cortex for the final run — one env var), and picks `NullRetrieval` (cold arms) vs `EverOSRetrieval` (warm arm). `FakeEverOS` is replaced by the real server running the `external_ref` fork. Nothing in `rrc/models|memory|measure|harness` should need editing beyond that wiring; if it does, the contract was underspecified — fix it at the sync-point.
