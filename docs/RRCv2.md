# ReasonRender for Code v2 — Spec Amortization

**Have the expensive model spec the work, the cheap model write it, tools prove it's correct — then remember the spec so you stop paying for it.**

> *Successor to ReasonRenderCoder v1. Same spec→implement→verify core, but the headline is no longer "the spec is cheaper than the code" (a modest, fragile, non-novel claim). It's "the spec stops being re-paid on repeat work." That single change makes the memory layer load-bearing, converts the demo from a static bar chart into a falling-cost curve, and sidesteps the reasoning-writer death case of v1. The prose sibling for fact-sparse language tasks is still RRv1.md.*

---

## Revision note (v2.1 — implementation decisions locked)

This revision folds in every open decision from the v2.0 draft and reconciles the EverOS integration against the **actual** EverOS source and HTTP contract (`docs/api.md`, `docs/how-memory-works.md` in `EverMind-AI/EverOS`, plus real response captures in those docs). Where the v2.0 draft assumed an API shape that turned out to be wrong, the corrected version is what appears below; the corrections are called out inline so the change is auditable.

**Locked decisions**

1. **Two stores, joined by an RRCv2-owned key — not by an EverOS ID.** `/add` and `/flush` return no memory ID (confirmed: they return only `{message_count, status}` / `{status}`). EverOS is a similarity index; RRCv2's own KV store is the store of record for the templated artifact.
2. **Structural gate is primary; score is a secondary floor.** EverOS returns a bounded `[0,1]` fused `score` and accepts native `radius` / `min_score` cutoffs, so the MISS floor is a server-side `min_score` — no bespoke adaptive-threshold machinery. REUSE-vs-PRIME is decided by a deterministic structural match (arity / arg types / field-set), which a score cannot verify.
3. **SPEC stage emits its own slots.** Slot annotations are a first-class field of the SPEC JSON, so `templatize()` and `extract_slot_values()` are structured reads, not prose NLP.
4. **A maximally-loose structural sanity check** guards only the rendered-garbage case; it is guaranteed never to reject a "close enough" spec (see §3). Execution (pytest) remains the real correctness backstop.
5. **`external_ref` metadata passthrough** is a small, documented patch to the open-source EverOS: a correlation ID carried as structured metadata (never through the extraction LLM), surfaced on every search hit — so the join survives extraction by construction. A zero-patch fallback (id-capture via `/get`) is documented for completeness.
6. **Live retrieval rides the synchronously-written `episode` track;** `agent_case → agent_skill` clustering is the production self-evolving path (real, but OME-async).
7. **Cost of record via Snowflake Cortex,** read inline per call from `AI_COMPLETE(..., show_details => TRUE)` `usage`, reconciled against `CORTEX_AI_FUNCTIONS_USAGE_HISTORY` by `QUERY_TAG`.

**Still open (see Appendix):** the repeating-workload design and target hit-rate `h`; the accepted, bounded production risk from the loose check; how an EverOS *write* acquires its `agent_id` (only relevant if the production agent-track path is used, not the demo); and one measurement term (`extract_slot_values` cost) to measure rather than assume.

---

## 0. The bet, restated

v1's own cost model (§4) found the **Spec stage is ~84% of pipeline cost** for small functions — because for a short function the spec-with-tests is nearly as long as the solution, and if the spec-writer reasons at all, the expensive stage balloons and the pipeline goes *negative* vs. baseline. So the one-shot saving is small and brittle.

The fix attacks the dominant term directly: **the plan/contract/tests for a task type are written once, stored as memory, and retrieved on the next similar task** — so on a cache hit you pay ~0 (reuse) or a small cheap-model cost (prime) for the spec instead of the full expensive stage. Per-task cost then *falls as the skill library fills*. This is inference amortization across a workload, and it is the genuinely under-explored seam v1 pointed at but didn't build.

**When this is worth building.** Only when the workload *repeats*: CRUD endpoints, glue/integration code, config, repetitive transforms, scaffolding — obvious structure, cheap-to-write tests, small enough for the cheap model in one pass. On an all-distinct benchmark (vanilla HumanEval) the hit rate is ~0 and you've built a slower cascade; see §6 for why the eval set choice is do-or-die. If you control your own serving, speculative decoding still dominates; if traffic splits cleanly by difficulty, a router is still simpler. This design's niche is **repetitive hosted-model workloads where the expensive judgment recurs.**

---

## 1. The pipeline

```
   Task
    │
    ▼
 RETRIEVE (EverOS POST /api/v2/memory/search: hybrid, top_k=3, min_score=τ_floor
           → per-hit {id, score} only; content is never used as the artifact)
    │
    ▼
 LOOKUP  (RRCv2's own store: fetch template(s) by the hit's external_ref / id
          — EverOS's returned content is never used directly)
    │
    ├── REUSE   (structural match == EXACT and score ≥ τ_floor) ─► render template with this task's slot values (skip expensive)
    │                                                                     │
    ├── PRIME   (structural match == NEAR)  ───► cheap model adapts template, neighbour(s) as few-shot
    │                                                                     │
    └── MISS    (no structural match, or nothing above τ_floor) ─► SPEC (expensive): plan+signature+contract+tests+slots
                                                                          │
                                                                          ▼
                                                     STRUCTURAL SANITY CHECK on the rendered spec
                                                     (loose; only rejects rendered garbage → treat as MISS)
                                                                          │
                                                                          ▼
                                               IMPLEMENT (cheap, one pass)
                                                          ▼
                                    VERIFY  ruff --fix → pyright → pytest
                                       fail → repair on cheap model
                                       fail after N rounds OR reused spec's tests don't pass
                                            → ESCALATE to a fresh expensive spec (fallback)
                                                          ▼
                ACCEPT → templatize spec (slots already labelled) → STORE template (own store, keyed by external_ref)
                        + write episode to EverOS with external_ref (seeds/reinforces the index)
                                                          ▼
                             Test-passing code  +  per-task cost logged to Snowflake
```

The verifier is the backstop that makes retrieval safe: a stale or ill-fitting reused spec — or a template rendered with the wrong slot values — fails its tests or fails implementation, triggering escalation to a fresh expensive spec. A bad cache hit therefore costs a wasted cheap pass plus one escalation — **not a wrong answer shipped.** The structural sanity check (§3) is a cheap pre-execution catch for the one silent case (rendered garbage) that would otherwise waste a pass; it is deliberately loose and never blocks a plausible reuse.

---

## 2. Stages and prompts

**Spec (expensive) — MISS path.** One spec for the whole task: a 2–4 step plan (direction, not code), exact signature(s), a contract with edge/error cases, acceptance tests, **and an explicit slot map** (so templatization is a structured read, not prose parsing). Only stage that needs judgment.

```
You are the SPEC stage. Given a coding task, output JSON. Do NOT implement.
{ "plan": "approach in 2-4 terse steps",
  "signature": "def f(x: int) -> int",         // exact types; pyright enforces them
  "contract": "behaviour incl. edge cases and errors",
  "tests": ["assert f(0)==1", "f(-1) raises ValueError"],
  "slots": {                                    // NEW in v2.1 — you label your own slots
    "entity":      "the domain noun this task is built around (e.g. 'User')",
    "identifiers": ["function/param/module names that are instance-specific"],
    "types":       ["arg/return types where only the entity type varies"],
    "fields":      ["the entity's fields that drive contract clauses and tests 1:1"],
    "constants":   ["status codes, default values, error messages/exception types"],
    "edge_values": ["DOMAIN-specific edge literals only; universal ones stay fixed"]
  } }
Rules: types precise; plan names the approach and tricky cases, does NOT transliterate
line-by-line (that re-writes the solution at expensive prices and kills the saving);
tests are acceptance criteria — cover normal path, edges, errors. Every instance-specific
literal that appears in signature/contract/tests MUST appear in slots, and vice versa, so
templatize() and extract_slot_values() are pure lookups.
```

**Prime (cheap) — NEAR path.** Same schema (including `slots`), but the small model writes it, given 1–2 retrieved neighbour specs as few-shot exemplars. **The neighbour spec text is fetched from RRCv2's own store by the hit's `external_ref`/`id` — never read off the search response.** Cheaper than the expensive stage, safer than blind reuse.

```
You are the SPEC stage (assisted). Write a spec (same JSON schema, including slots) for
the NEW task. Adapt the pattern of the EXAMPLE spec(s) below; change signatures/contract/
tests/slots to fit the new task exactly. Do not copy an example that does not fit — if none
fits, say {"unfit": true} and stop.
Examples: {neighbor_specs}   // fetched from RRCv2's own store by id, not from EverOS content
New task: {task}
```

**Implement (cheap).** Whole solution in one pass; the high-output stage where the savings live.

```
You are the IMPLEMENT stage. Implement the whole task to satisfy the spec and pass the
tests. Output only code. Match the signature exactly; satisfy the contract; handle edges
and errors. Follow the plan but own the details. Do NOT change the tests. Solve the
general contract, not just the listed asserts.
Spec: {spec}
```

**Repair.** The implement prompt again with the current code and the tool/test output appended, asking for a fix. Escalate to a fresh expensive spec after N failed rounds (default N=2).

**Independent tests (adopt from AgentCoder).** On the MISS/PRIME paths, generate acceptance tests *without sight of the implementation*. On REUSE the tests are rendered deterministically from the stored template's test pattern; test-generation quality is the binding constraint (§4), and REUSE's blind spot (a too-weak reused suite) is discussed in §3.

---

## 3. Verification: deterministic first, execution for the rest

Cheapest-first — the free layers strip most junk before you spend a repair token.

- **Tier −1 — structural sanity check (reuse/prime only; free, deterministic).** Fires only when the *rendered* spec is structurally incoherent for the new task: a slot came back empty, the rendered signature doesn't parse, or the rendered tests don't reference the new function's name / arity. On failure → treat as MISS (fresh expensive spec). **This check is deliberately maximally loose: it is a theorem, not a hope, that a "close enough" or even "semi-close" spec passes it** — because a plausible spec renders into parseable, correctly-named, correctly-shaped tests every time. It can only reject rendered garbage (malformed slot-fill), which execution would catch a beat later anyway; catching it here just saves the wasted implement pass.
  - **Honest caveat (accepted trade).** Because the check is that loose, it does **not** close the underlying reuse hole: a reused test suite that is simply *too weak* for the new task can pass subtly-wrong code, and it sails through both this check and pytest. In the hackathon this does not bite, because arms are scored against the benchmark's **oracle** tests (§6), which catch a wrong-but-passes-weak-tests result. In production, without oracle tests, that case can ship a wrong answer. Choosing a maximally loose check is choosing to accept this bounded risk in exchange for maximal hit rate; it is a deliberate trade, not an oversight (see Appendix).
- **Tier 0 — `ruff format` + `ruff check --fix`:** free, deterministic, auto-fixes formatting, unused imports, import order, dead syntax. Run first of the execution tiers.
- **Tier 1 — `pyright` (basic, not strict):** free, deterministic, detect-only. Catches type mismatches, undefined names, missing returns, None-safety — *if the code is annotated*, which is why the spec must emit precise signatures. **Use `basic`, not `strict`** — strict flags missing annotations on otherwise-correct bodies as errors and burns repair rounds on noise. Type-error fixes loop back to the model.
- **Tier 2 — `pytest` against the spec's tests:** the only tier that catches logic/behavioural bugs, and non-negotiable. Well-typed, lint-clean code that computes the wrong answer sails through Tiers 0–1; only execution catches it.

**Load-bearing weakness (unchanged from v1, sharpened by reuse):** the tests are model-authored, so "the tests make it safe" holds only if the tests are correct and complete. Reuse compounds this — a reused test suite may under-specify the *new* task (see the Tier −1 caveat). Mitigations: independent test generation on MISS/PRIME (§2), the structural gate + `min_score` floor in §4, and treating any benchmark pass-rate as an upper bound production won't meet.

**Sandboxing.** pytest executes model-generated code and model-generated tests. For the controlled synthetic workload this runs in a subprocess with a wall-clock timeout and no network; that is sufficient for the hackathon and should be hardened before any untrusted use.

---

## 4. EverOS integration (the amortization engine)

EverOS supplies **procedural memory**, but it is used here strictly as a *similarity index*, not as the store of record for the reusable artifact. Inside EverOS, a batch of messages is buffered on `/add`, an LLM carves a memory cell on `/flush` (one call), and derived kinds are written **asynchronously** by the Offline Memory Engine (OME); the cascade coroutine then turns each markdown write into LanceDB rows. None of that is safe to depend on for a byte-exact spec, so the design splits the job: **EverOS finds the matching past task; RRCv2 owns the artifact.**

### 4.1 What the real API actually returns (reconciled against `docs/api.md`)

- **Response envelope:** every 200 is `{"request_id": "<32hex>", "data": {…}}`. Read `res["data"]`.
- **`/add`** returns `{"message_count", "status": "accumulated"|"extracted"}` — **no memory ID.** **`/flush`** returns `{"status": "extracted"|"no_extraction"}` — **no memory ID.** *(This is the fact that kills the "join on EverOS's ID" design from v2.0: there is no ID to capture at write time.)*
- **`/search`** (`POST /api/v2/memory/search`) takes `user_id` **XOR** `agent_id`, `app_id`, `project_id`, `query`, `method` (`keyword|vector|hybrid|agentic`, default `hybrid`), `top_k` (`-1` or `1..100`), `radius` (cosine threshold `[0,1]`), `min_score` (post-fusion floor `[0,1]`), and a `filters` DSL. It returns five always-present arrays: `episodes`, `profiles`, `agent_cases`, `agent_skills`, `unprocessed_messages`. **There is no `memory_types` parameter** *(the v2.0 draft invented it)* — you select the track by passing `user_id` (→ episodes/profiles) or `agent_id` (→ agent_cases/agent_skills).
- **The score is bounded and usable.** Each hit carries `score: number` in `[0,1]` (a real capture: `0.6299`), and `min_score`/`radius` are `[0,1]` cutoffs. So the MISS floor is just `min_score=τ_floor` on the request — **no adaptive-threshold machinery is required** *(the v2.0 "the score isn't on a fixed scale" worry was over-stated; it is fused/RRF-based rather than a pure cosine, so treat it as an ordinal-ish floor, not a calibrated probability, and let the structural gate make the REUSE/PRIME decision).*
- **Stable IDs exist on read.** Search/get items carry `id` like `<owner>_<kind>_<YYYYMMDD>_<NNN>` (`_ep_`, `_ac_`, `_sk_`). They're minted at extraction, i.e. only visible via `/search` or `/get`, never from `/add`/`/flush`.

### 4.2 Two stores, one RRCv2-owned join key

- **EverOS** — holds a *templated* spec's task description as searchable content, purely so its hybrid search (BM25 + dense vectors + optional rerank) matches on task shape. Read on RETRIEVE for `{id, score}` only; its returned content is never treated as the artifact. (What EverOS embeds is its extraction LLM's *paraphrase* of what you submit, not your raw bytes — another reason its content is index-only.)
- **RRCv2's own store** — a plain deterministic KV store (SQLite), keyed by an RRCv2-owned `external_ref` (a content fingerprint RRCv2 computes over the templated form). Holds the templated spec verbatim. This is what actually gets rendered on a hit.

### 4.3 The `external_ref` passthrough (the EverOS patch)

The cleanest join does not depend on capturing any EverOS ID, and does not require the fingerprint to "survive" the extraction LLM. Carry it as **structured metadata that never enters a generative step:**

- Accept an `external_ref` on `/add` (per-session or per-message).
- Persist it verbatim in the memory's YAML frontmatter and as a LanceDB scalar column — alongside the existing immutable `id` / `entry_id` / `content_sha256` frontmatter the store already maintains.
- Surface it on every `Search*Item` / `Get*Item`.
- Because one `/add` can fan out into several memories, copy the ref onto every memory that call produces, so a hit on any of them maps back to the one template.

This is a correlation-ID passthrough — a small, generically reasonable feature, ~a few lines threaded through the add DTO, the frontmatter writer, and the search/get response DTOs. It keeps the "we use the real open-source EverOS layer" claim honest with only a documented, upstreamable patch. Relevant anchors in the source: the ingest/extraction path and `MemoryRoot` frontmatter chassis (`src/everos/core/persistence/…`), and the `Search*Item` DTOs behind `docs/api.md`.

> **Optional companion patch (match quality).** EverOS embeds the extractor's paraphrase, so shape-matching is only partial. Since you own the code, you can additionally embed the raw templated task text directly (or bias the extractor via `ome.toml`), so retrieval matches on the shape you control. Not required for the demo; it's what makes the §4.5 match-quality argument true rather than hopeful.

> **Zero-patch fallback.** If you'd rather not patch: use a unique `session_id` per task, and after storing, capture EverOS's `id` via `POST /api/v2/memory/get` filtered by that `session_id` (episodes, newest first), then key the own-store under that `id`. Works with stock EverOS, at the cost of one extra read and dependence on read-your-write timing (§4.6).

### 4.4 Which track: episodes now, skills as the production path

`agent_case`/`agent_skill` are the "self-evolving skills" surface, but they are **OME-async**, thin agent trajectories are **skipped by design**, `agent_skill` only exists after an **offline clustering** pass, and how a *write* acquires its `agent_id` is not fully pinned by the public docs (Appendix). For a fast 30–50-task live stream, that is too much async on the critical path.

- **Demo (locked): ride the `episode` track.** Episodes are written **synchronously** on `/flush` (on disk before the call returns), have a clear owner (`sender_id` of a `role:"user"` turn = the `user_id` you later search under), and come back **scored**. RRCv2 writes the templated task as a user turn under a fixed agent identity (`user_id="rrc"`), flushes, and retrieves with `user_id="rrc"`.
- **Production (described, not demoed): the self-evolving path.** In production the same trajectories seed `agent_case`s that OME clusters into `agent_skill`s, and `POST /api/v2/ome/trigger` can force those strategies. RRCv2's contract is identical either way — it only ever consumes `{id, score}` + its own store — so switching tracks is an endpoint/track change, not a redesign.

### 4.5 Store and retrieve (real HTTP; no SDK)

There is no `everos_cloud` Python SDK; the surface is REST at `/api/v2/memory/*` (v1 is a legacy alias — write v2). *(The v2.0 `from everos_cloud import EverOS; client.v1.memories` snippet was fabricated and is removed.)*

```python
import requests, json
BASE = "http://127.0.0.1:8000/api/v2/memory"
SCOPE = {"app_id": "default", "project_id": "default"}

# --- STORE (on ACCEPT) ---
template, slots, ref = templatize(spec)        # deterministic, RRCv2-owned; ref = fingerprint over template
spec_store.put(ref, {"template": template, "slots": slots})   # own store IS the store of record

requests.post(f"{BASE}/add", json={
    "session_id": workload_id, **SCOPE,
    "external_ref": ref,                       # <-- passthrough patch; index-only correlation id
    "messages": [{"sender_id": "rrc", "role": "user", "timestamp": ts_ms,
                  "content": task_text}],       # task text drives shape-matching; template lives in own store
})
requests.post(f"{BASE}/flush", json={"session_id": workload_id, **SCOPE})   # sync md write

# --- RETRIEVE (on new task) ---
res = requests.post(f"{BASE}/search", json={
    "user_id": "rrc", **SCOPE,
    "query": task_text, "method": "hybrid",
    "top_k": 3, "min_score": TAU_FLOOR,        # server-side MISS floor; no bespoke thresholding
}).json()["data"]

hits = res["episodes"]                          # scored; index-only
best = hits[0] if hits else None
tmpl = spec_store.get(best["external_ref"]) if best else None   # never read the spec off the hit itself
# structural gate decides the branch (below); score already ≥ TAU_FLOOR by construction
```

### 4.6 Thresholds and the structural gate

- **`min_score = τ_floor`** (start ~0.30–0.40 for RRF-fused scores; calibrate on the workload) is the only score knob — it defines MISS. Everything returned is already "close enough to consider."
- **The REUSE vs. PRIME decision is deterministic and query-independent**, so it does not inherit any score instability: fetch the candidate template from the own store, compare its `slots` shape to the new task's — **EXACT** (same arity, arg types, field-set) → REUSE; **NEAR** (same family, differing slot values/count) → PRIME with neighbour(s) as few-shot; **no structural match** among the top-k → MISS. Prefer PRIME over REUSE whenever signature/arg-count differs even if the score is high — rendering a template against the wrong shape is a silent failure mode the Tier −1 check and pytest catch, but at the cost of a wasted pass.

### 4.7 Read-your-write and forcing the async engine

The write path is strongly consistent for markdown; the read path (LanceDB) is eventual — sub-second typically, up to ~10–15 s under load — so a `/search` right after the `/flush` that produced a record may miss it. Levers (all real):

- **Poll `GET /health` `cascade.pending`** until it reads `0` on **two** consecutive samples (a single zero can be a false convergence) before relying on a just-written record being searchable.
- **`everos cascade sync`** forces the md→LanceDB queue to drain now.
- **`POST /api/v2/ome/trigger {name, force:true, timeout}`** forces a named OME strategy (e.g. case/skill formation) synchronously-ish — only needed on the production agent-track path.

For the demo, do not depend on store→immediate-hit within one task; hits come from *earlier* tasks in the stream, and the read-your-write recipe covers the pre-baked warm-up.

### 4.8 Gotchas

- Always `flush`; a freshly stored episode may not be searchable for a beat (use the `cascade.pending` recipe).
- Use one consistent `session_id` per workload (or a deterministic per-task `session_id` if you use the zero-patch id-capture fallback) so memory doesn't fragment; keep `app_id`/`project_id` constant — searches never cross scopes.
- Skills can rot as conventions drift; TTL/versioning is future work.
- The join is a dependency: if RRCv2's own store and EverOS diverge (e.g. store restored from an older backup), a hit's `external_ref` resolves to no template — treat that as a **MISS**, not an error.

---

## 5. The economics

**Mechanism (unchanged).** Decode (output) tokens cost several × prefill (input) tokens; code is high-output; moving implementation to the cheap model targets the expensive phase. What v2 adds is removing the *expensive spec* on hits.

**Cold vs. warm.** Let the full pipeline cost per task be `C_full` (≈ v1: expensive spec + cheap implement + verify/repair), and a hit cost `C_hit ≈ cheap_implement + verify (+ small prime) (+ small slot-fill)`. Because the spec was ~84% of `C_full`, `C_hit` is a small fraction of it. With hit rate `h` on a repeating stream:

```
cost_per_solved(h) ≈ (1 − h)·C_full + h·C_hit
```

As `h → high`, blended cost collapses toward `C_hit` — and can fall **below the cheap-alone floor** while keeping the expensive model's spec quality on the misses that seed the library. The headline is not a single % but the **curve of cost-per-solved vs. tasks processed**, and its **warm asymptote** relative to the cascade/cheap-alone floors.

**Worked shape** (illustrative units `exp_in=3, exp_out=15, cheap_in=0.25, cheap_out=1.25`; ~120-tok task, ~160-tok solution, benchmark tests): `C_full ≈ 2,205` (v1's number), of which the spec is ~1,860. A verbatim REUSE hit removes the spec: `C_hit ≈ 255 + 0.3·repair ≈ 345`. At `h=0.6`: `cost_per_solved ≈ 0.4·2,205 + 0.6·345 ≈ 1,089` — roughly **half of v1 cold and below cheap-alone**, at matching pass@1. At `h→0.9` it approaches ~530. (Substitute real two-model prices; treat as shape, not result.)

**The one measurement caveat (v2.1).** REUSE is "no *spec* model call," but rendering a template still needs slot values for the new task. If `extract_slot_values()` is pure deterministic substitution (the goal, given the SPEC stage now emits `slots`), REUSE adds no model cost. If a given workload needs a small cheap-model call to fill slots from free-form task text, `C_hit` gains a small term — **measure it, don't assume it's zero.** It doesn't change the curve's shape; it raises the warm asymptote slightly.

**What still kills it:** `h≈0` (non-repeating workload) → you've built a slower cascade; a reasoning spec-writer on the MISS path (keep it non-reasoning); a low first-pass rate inflating repairs. Measure `h`, `C_full`, `C_hit` on your workload before believing any number.

---

## 6. Evaluation

Correctness is checkable by execution, so tests decide — no judge. Measure **cost per passing solution**, and report **hit rate** and **the curve**, not just an endpoint.

**Workload (do-or-die — remains an open design task; see Appendix).** Use a *repeating* stream, not vanilla HumanEval. Options: synthesize 30–50 tasks with recurring structure (e.g., "CRUD endpoint for entity X" × many entities; "parse/transform format Y" families), or cluster MBPP/BigCodeBench by template and order so families recur. A flat curve on stage means you chose a non-repeating set and disproved yourself.

**Define a "hit" honestly.** A hit = a task solved via the REUSE or PRIME path **without** escalation to a fresh expensive spec. A near-miss that escalates is a MISS-cost, not a hit; count it as such or the curve flatters itself.

**Arms (run in one harness):**
1. **Baseline** — expensive solves end-to-end.
2. **Cheap-alone** — cheap solves + same verify/repair (no spec).
3. **Cascade** — cheap-alone, escalate whole problem to expensive on failure (FrugalGPT shape; the key competitor).
4. **Cold pipeline** — spec→cheap→verify, empty library (= v1).
5. **Warm (this design)** — cold pipeline + EverOS retrieve/reuse/prime/store over the repeating stream.

**Metrics:** pass@1 across arms (against provided **oracle** tests → benchmark safety, not the production case of §3–4); total cost counting *every* call (spec, all implement attempts, all repairs, any slot-fill, and — for cascade — the escalated solve); **cost-per-solved over task order**; and **hit rate** for the warm arm. **Enforce pass@1 parity as a reporting rule:** run all arms on the same oracle tests, report pass@1 per arm, and disqualify a cost win if the warm arm's pass@1 falls outside the baseline's noise band (e.g. ±1–2 tasks on a 30–50 set). External bars: cite Parsel (85% HumanEval pass@1) and AgentCoder (~96% with GPT-4) — don't claim to beat them without running them. The honest scoreboard is arms 1–5 in your harness; the win condition is **warm asymptote below the cascade/cheap-alone floors at matching pass@1**, which a stateless cascade structurally cannot do.

---

## 7. Snowflake integration (cost of record + the money-shot)

Run **both model stages as Cortex `AI_COMPLETE`** — strong model for SPEC/PRIME, small model for IMPLEMENT/repair — so every token and AI Credit is metered natively; you don't hand-roll a token counter.

```sql
-- each stage call, tagged so cost attaches to arm + task order
ALTER SESSION SET QUERY_TAG = 'rrc:arm=warm;task=0007;stage=implement';
SELECT AI_COMPLETE('claude-3-5-sonnet', :prompt, show_details => TRUE);  -- SPEC/PRIME: strong
SELECT AI_COMPLETE('llama3.1-8b',       :prompt, show_details => TRUE);  -- IMPLEMENT/repair: small
```

**Inline per-call cost is the primary source, not a workaround.** With `show_details => TRUE`, `AI_COMPLETE` returns a `usage` object (`prompt_tokens`, `completion_tokens`, `total_tokens`) inline in the response — so the live curve reads real per-call tokens with no view lag. (Confirm the two model strings are enabled in your region with `SHOW MODELS IN CORTEX`; keep the warehouse ≤ MEDIUM and warm.)

**Reconciliation (authoritative, lagged).** Read credits from the account-usage view; it carries per-call rows with query-tag attribution:

```sql
SELECT query_id, model_name, tokens, credits, query_tag, start_time
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
WHERE start_time >= DATEADD(hour, -2, CURRENT_TIMESTAMP());
-- each row = a single function call, keyed by QUERY_ID; QUERY_TAG / USER_ID / roles are
-- populated for data after 2026-02-16. GROUP BY task order to plot cost_per_solved.
```

- Each row is one function call; `QUERY_TAG` lets you split cost by `arm` / `task` / `stage`.
- The view lags ~2–5 minutes, so it is **reconciliation**, not the live source.
- `AI_COUNT_TOKENS(model, text)` estimates **input** tokens only (pre-call) — use it for pre-flight sizing, never for the cost axis.
- A per-model credit→cost table (from Snowflake's service consumption table) converts credits to one comparable cost axis; apply it identically across all five arms.

**Money-shot pipeline:** drive the on-stage chart from inline `usage` in real time; cite `CORTEX_AI_FUNCTIONS_USAGE_HISTORY` as the authoritative reconciliation after the fact.

---

## 8. Prior art (unchanged — the method isn't the contribution; the amortization + measurement is)

| System | Relation |
|---|---|
| **FrugalGPT / cascades** (2305.05176) | The cost logic; the cascade is arm 3 and the competitor to beat. |
| **Parsel** (2212.10561) | The spec→implement→verify core (with decomposition we drop). Closest ancestor; validated plan-plus-tests. |
| **AgentCoder** (2312.13010) | Programmer + independent test-designer + executor. Source of §2's independent-test fix. |
| **Self-Refine / Reflexion / Self-Debugging** (2023) | The repair loop. |
| **Efficient LLM Collaboration via Planning** (2506.11578) | Cheap-implementer-via-planning; closest recent cost angle. |
| **Speculative decoding** (2023) | Cheap-does-the-bulk, lossless — but serving-side; unavailable to a hosted-API consumer. |

The novel seam v2 occupies: **cross-task amortization of the expensive spec via retrieved procedural memory**, measured as a falling cost-per-solved curve against those stateless baselines.

---

## 9. Hackathon build & demo

Scope for ~5 hours, 1–2 people, 3-minute demo.

**Build.** RETRIEVE→(reuse/prime/miss)→structural-check→IMPLEMENT→`ruff`→`pyright`→`pytest`→repair→ACCEPT/store loop, over a *repeating* 30–50-task stream. Both model calls via Cortex `AI_COMPLETE(show_details => TRUE)`; memory via self-hosted EverOS at `/api/v2/memory/*` (episode track, `external_ref` patch); cost from inline `usage`, reconciled from the account-usage view. Run arms 1–5; the harness logs every call's tokens inline, applies the price table, and plots cost-per-solved by task order. Hardcode the workload and thresholds.

**Money-shot.** The **descending cost-per-solved curve** for the warm arm crossing *below* the cascade and cheap-alone floors, with a **hit-rate counter climbing** beside it. This beats a four-bar chart precisely because it shows the compounding.

**Credibility beats (one live task each):** (a) a clean HIT — retrieval + structural EXACT skips the expensive stage, cheap implement passes, cost drops; (b) a MISS — expensive spec seeds a new skill and is stored; (c) a NEAR-miss whose rendered reuse doesn't fit → structural check or pytest fails → escalation to a fresh expensive spec → green. Beat (c) proves the backstop and pre-empts "what if the cache is wrong?".

**Caveats for the slot.** More moving parts than a one-call demo: pre-bake the cold arms and the early part of the warm stream so the curve is already bending on screen; keep the live portion to the three chosen tasks; account for EverOS async index with the `GET /health` `cascade.pending` recipe (and `everos cascade sync`) so pre-baked stores are searchable; have a fallback recording.

---

## Appendix — remaining open items

Everything from the v2.0 draft's open list is now resolved by decision or by the real EverOS/Snowflake docs, **except** the following.

1. **Repeating-workload design + target `h` (do-or-die; your judgment).** The specific task families, count per family, slot variation, and arrival order that make the curve visibly bend — and the target hit rate you're claiming — are creative judgment about your own benchmark. Too templatable proves something trivial; too varied gives `h≈0`. This is the single biggest determinant of the result and the one thing no lookup settles.
2. **Accepted bounded production risk from the loose structural check (chosen trade).** A too-weak *reused* test suite can pass subtly-wrong code in production (no oracle tests). This is accepted deliberately in exchange for a maximally loose check that never blocks a plausible reuse (§3). Written down here so it's an explicit trade, not a silent gap. If a future version wants to close it, the lever is a cheap independent-test pass or a mutation/property sanity check on REUSE — at a small `C_hit` cost.
3. **How an EverOS *write* acquires its `agent_id` (only if the production agent-track path is used).** `docs/api.md` pins how to *search* the agent track (`agent_id`) and the `agent_case`/`agent_skill` schemas, but not how a write is keyed to an `agent_id`. Irrelevant to the demo (which rides the episode track); resolve before shipping the self-evolving path by reading the agent pipeline in `src/everos` (or `ENV=DEV everos server start` + `GET /openapi.json`, then a probe).
4. **`extract_slot_values()` cost term (measure, don't assume).** If filling a template's slots for a new task needs a cheap-model call rather than deterministic substitution, `C_hit` gains a small term (§5). Measure it on the workload; it raises the warm asymptote slightly without changing the curve's shape.

Confirmations that closed the rest (for the record): `/openapi.json` is served only under `ENV=DEV`; `/add`/`/flush` return no ID; search returns bounded `[0,1]` scores with `min_score`/`radius` cutoffs; track = `user_id` XOR `agent_id` (no `memory_types` param); read-your-write via `GET /health` `cascade.pending`; `AI_COMPLETE(show_details => TRUE)` returns inline `usage`; `CORTEX_AI_FUNCTIONS_USAGE_HISTORY` carries per-call `QUERY_TAG` for data after 2026-02-16.