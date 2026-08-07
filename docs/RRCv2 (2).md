# ReasonRender for Code v2 — Spec Amortization

**Have the expensive model spec the work, the cheap model write it, tools prove it's correct — then remember the spec so you stop paying for it.**

> *Successor to ReasonRenderCoder v1. Same spec→implement→verify core, but the headline is no longer "the spec is cheaper than the code" (a modest, fragile, non-novel claim). It's "the spec stops being re-paid on repeat work." That single change makes the memory layer load-bearing, converts the demo from a static bar chart into a falling-cost curve, and sidesteps the reasoning-writer death case of v1. The prose sibling for fact-sparse language tasks is still RRv1.md.*

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
 RETRIEVE (EverOS: hybrid search over stored specs/skills, top_k=3)
    │
    ├── HIT  (similarity ≥ τ_reuse) ─────────────► REUSE spec verbatim   (skip expensive)
    │                                                    │
    ├── NEAR (τ_prime ≤ sim < τ_reuse) ──► PRIME: cheap model writes spec, neighbor(s) as few-shot
    │                                                    │
    └── MISS (sim < τ_prime) ────────────► SPEC (expensive): plan+signature+contract+tests
                                                         │
                                                         ▼
                                              IMPLEMENT (cheap, one pass)
                                                         ▼
                                    VERIFY  ruff --fix → pyright → pytest
                                       fail → repair on cheap model
                                       fail after N rounds OR reused-spec tests don't fit
                                            → ESCALATE to a fresh expensive spec (fallback)
                                                         ▼
                                    ACCEPT → STORE spec+outcome in EverOS (seeds/reinforces skill)
                                                         ▼
                             Test-passing code  +  per-task cost logged to Snowflake
```

The verifier is the backstop that makes retrieval safe: a stale or ill-fitting reused spec fails its tests or fails implementation, triggering escalation to a fresh expensive spec. A bad cache hit therefore costs a wasted cheap pass plus one escalation — **not a wrong answer shipped.**

---

## 2. Stages and prompts

**Spec (expensive) — MISS path.** One spec for the whole task: a 2–4 step plan (direction, not code), exact signature(s), a contract with edge/error cases, and acceptance tests. Only stage that needs judgment.

```
You are the SPEC stage. Given a coding task, output JSON. Do NOT implement.
{ "plan": "approach in 2-4 terse steps",
  "signature": "def f(x: int) -> int",         // exact types; pyright enforces them
  "contract": "behaviour incl. edge cases and errors",
  "tests": ["assert f(0)==1", "f(-1) raises ValueError"] }
Rules: types precise; plan names the approach and tricky cases, does NOT transliterate
line-by-line (that re-writes the solution at expensive prices and kills the saving);
tests are acceptance criteria — cover normal path, edges, errors.
```

**Prime (cheap) — NEAR path.** Same schema, but the small model writes it, given 1–2 retrieved neighbour specs as few-shot exemplars. Cheaper than the expensive stage, safer than blind reuse.

```
You are the SPEC stage (assisted). Write a spec (same JSON schema) for the NEW task.
Adapt the pattern of the EXAMPLE spec(s) below; change signatures/contract/tests to fit
the new task exactly. Do not copy an example that does not fit — if none fits, say
{"unfit": true} and stop.
Examples: {neighbor_specs}
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

**Independent tests (adopt from AgentCoder).** Generate acceptance tests *without sight of the implementation*, and — critically for reuse — re-validate a reused spec's tests against the new task's contract, so a near-miss spec can't pass thin, mismatched tests. Test-generation quality is the binding constraint (§4).

---

## 3. Verification: deterministic first, execution for the rest

Cheapest-first — the free layers strip most junk before you spend a repair token.

- **Tier 0 — `ruff format` + `ruff check --fix`:** free, deterministic, auto-fixes formatting, unused imports, import order, dead syntax. Run first.
- **Tier 1 — `pyright` (strict):** free, deterministic, detect-only. Catches type mismatches, undefined names, missing returns, None-safety — *if the code is annotated*, which is why the spec must emit precise signatures. Type-error fixes loop back to the model.
- **Tier 2 — `pytest` against the spec's tests:** the only tier that catches logic/behavioural bugs, and non-negotiable. Well-typed, lint-clean code that computes the wrong answer sails through Tiers 0–1; only execution catches it.

**Load-bearing weakness (unchanged from v1, sharpened by reuse):** the tests are model-authored, so "the tests make it safe" holds only if the tests are correct and complete. Reuse compounds this — a reused test suite may under-specify the *new* task. Mitigations: independent test generation (above), the reuse/prime/miss thresholds in §5, and treating any benchmark pass-rate as an upper bound production won't meet.

---

## 4. EverOS integration (the amortization engine)

EverOS supplies **procedural memory**: accepted specs become `agent_case` records; repeated wins are distilled offline into reusable `agent_skill`s. We read them on RETRIEVE and write them on ACCEPT.

**Store (on ACCEPT).** Write the task, the accepted spec, and the outcome as an agent memory, then flush so extraction runs immediately.

```python
from everos_cloud import EverOS
client = EverOS(api_key=EVEROS_API_KEY)
m = client.v1.memories

m.add(  # POST /api/v1/memories/agent
  user_id="rrc",                        # one agent identity for the coder
  session_id=workload_id,
  messages=[
    {"role":"user","content": task_text},
    {"role":"assistant","content": json.dumps(spec)},   # the reusable artifact
    {"role":"tool","content": f"verdict=pass pass@1=1 repairs={n} sig={spec['signature']}"},
  ],
)
m.flush(user_id="rrc", session_id=workload_id)           # force extraction (async by default)
```

**Retrieve (on new task).** Hybrid search, filtered to procedural memory, task text as query.

```python
res = m.search(
  filters={"user_id":"rrc"},
  query=task_text,
  method="hybrid", top_k=3,
  memory_types=["agent_skill","agent_case"],
)
neighbors = res.data  # rank + similarity → apply thresholds below
```

**Thresholds** (tune on your workload; start here): `τ_reuse = 0.88` (reuse verbatim), `τ_prime = 0.70` (prime a cheap spec). Below `τ_prime` → MISS → expensive spec. Prefer PRIME over REUSE whenever the retrieved signature/arg-count differs from the new task, even above τ_reuse.

**Gotchas.** Extraction is asynchronous — always `flush`, and note a freshly stored skill may not be searchable for a beat (fine across tasks; don't rely on store→immediate-hit within one task). Use one consistent `session_id` per workload so memory doesn't fragment. Skills can rot as conventions drift; TTL/versioning is future work (§7).

---

## 5. The economics

**Mechanism (unchanged).** Decode (output) tokens cost several × prefill (input) tokens; code is high-output; moving implementation to the cheap model targets the expensive phase. What v2 adds is removing the *expensive spec* on hits.

**Cold vs. warm.** Let the full pipeline cost per task be `C_full` (≈ v1: expensive spec + cheap implement + verify/repair), and a hit cost `C_hit ≈ cheap_implement + verify (+ small prime)`. Because the spec was ~84% of `C_full`, `C_hit` is a small fraction of it. With hit rate `h` on a repeating stream:

```
cost_per_solved(h) ≈ (1 − h)·C_full + h·C_hit
```

As `h → high`, blended cost collapses toward `C_hit` — and can fall **below the cheap-alone floor** while keeping the expensive model's spec quality on the misses that seed the library. The headline is not a single % but the **curve of cost-per-solved vs. tasks processed**, and its **warm asymptote** relative to the cascade/cheap-alone floors.

**Worked shape** (illustrative units `exp_in=3, exp_out=15, cheap_in=0.25, cheap_out=1.25`; ~120-tok task, ~160-tok solution, benchmark tests): `C_full ≈ 2,205` (v1's number), of which the spec is ~1,860. A verbatim REUSE hit removes the spec: `C_hit ≈ 255 + 0.3·repair ≈ 345`. At `h=0.6`: `cost_per_solved ≈ 0.4·2,205 + 0.6·345 ≈ 1,089` — roughly **half of v1 cold and below cheap-alone**, at matching pass@1. At `h→0.9` it approaches ~530. (Substitute real two-model prices; treat as shape, not result.)

**What still kills it:** `h≈0` (non-repeating workload) → you've built a slower cascade; a reasoning spec-writer on the MISS path (keep it non-reasoning); a low first-pass rate inflating repairs. Measure `h`, `C_full`, `C_hit` on your workload before believing any number.

---

## 6. Evaluation

Correctness is checkable by execution, so tests decide — no judge. Measure **cost per passing solution**, and report **hit rate** and **the curve**, not just an endpoint.

**Workload (do-or-die).** Use a *repeating* stream, not vanilla HumanEval. Options: synthesize 30–50 tasks with recurring structure (e.g., "CRUD endpoint for entity X" × many entities; "parse/transform format Y" families), or cluster MBPP/BigCodeBench by template and order so families recur. A flat curve on stage means you chose a non-repeating set and disproved yourself.

**Arms (run in one harness):**
1. **Baseline** — expensive solves end-to-end.
2. **Cheap-alone** — cheap solves + same verify/repair (no spec).
3. **Cascade** — cheap-alone, escalate whole problem to expensive on failure (FrugalGPT shape; the key competitor).
4. **Cold pipeline** — spec→cheap→verify, empty library (= v1).
5. **Warm (this design)** — cold pipeline + EverOS retrieve/reuse/prime/store over the repeating stream.

**Metrics:** pass@1 across arms (against provided oracle tests → benchmark safety, not the production case of §3–4); total cost counting *every* call (spec, all implement attempts, all repairs, and — for cascade — the escalated solve); **cost-per-solved over task order**; and **hit rate** for the warm arm. External bars: cite Parsel (85% HumanEval pass@1) and AgentCoder (~96% with GPT-4) — don't claim to beat them without running them. The honest scoreboard is arms 1–5 in your harness; the win condition is **warm asymptote below the cascade/cheap-alone floors at matching pass@1**, which a stateless cascade structurally cannot do.

---

## 7. Snowflake integration (cost of record + the money-shot)

Run **both model stages as Cortex `AI_COMPLETE`** — strong model for SPEC/PRIME, small model for IMPLEMENT/repair — so every token and AI Credit is metered natively; you don't hand-roll a token counter.

```sql
-- each stage call, tagged so cost attaches to arm + task order
ALTER SESSION SET QUERY_TAG = 'rrc:arm=warm;task=0007;stage=implement';
SELECT AI_COMPLETE('claude-3-5-sonnet', :prompt);   -- SPEC/PRIME: strong model
SELECT AI_COMPLETE('llama3.1-8b',       :prompt);   -- IMPLEMENT/repair: small model
```

Read cost from the account-usage views (per-call tokens + credits, hourly windows, with model/warehouse and — post-Feb 2026 — user/query-tag attribution):

```sql
SELECT query_id, model_name, tokens, credits, start_time
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
WHERE start_time >= DATEADD(hour, -2, CURRENT_TIMESTAMP());
-- join CORTEX_AISQL_USAGE_HISTORY (query grain, has user_id) to attribute by query_tag,
-- then GROUP BY task order to plot cost_per_solved vs. tasks → the falling curve.
```

**Latency workaround for a live demo:** the usage views lag ~2–5 min. For the on-stage curve, also capture token counts inline (`AI_COUNT_TOKENS(model, text)` for pre-call estimates and the per-call usage returned by AISQL), drive the chart from those in real time, and cite the account-usage view as authoritative reconciliation.

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

**Build.** RETRIEVE→(reuse/prime/miss)→IMPLEMENT→`ruff`→`pyright`→`pytest`→repair→ACCEPT/store loop, over a *repeating* 30–50-task stream. Both model calls via Cortex `AI_COMPLETE`; memory via EverOS Cloud; cost via Snowflake. Run arms 1–5; the harness logs every call's tokens (inline + reconciled from the usage view), applies prices, and plots cost-per-solved by task order. Hardcode the workload and thresholds.

**Money-shot.** The **descending cost-per-solved curve** for the warm arm crossing *below* the cascade and cheap-alone floors, with a **hit-rate counter climbing** beside it. This beats a four-bar chart precisely because it shows the compounding.

**Credibility beats (one live task each):** (a) a clean HIT — retrieval skips the expensive stage, cheap implement passes, cost drops; (b) a MISS — expensive spec seeds a new skill; (c) a NEAR-miss whose reused tests don't fit → verify fails → escalation to a fresh expensive spec → green. Beat (c) proves the backstop and pre-empts "what if the cache is wrong?".

**Caveats for the slot.** More moving parts than a one-call demo: pre-bake the cold arms and the early part of the warm stream so the curve is already bending on screen; keep the live portion to the three chosen tasks; account for EverOS async flush and view latency with inline capture; have a fallback recording.

---

## Appendix — assumptions & open questions

- All cost figures are illustrative relative units; substitute real two-model prices; no measured data yet.
- Compounding requires workload repetition; `h` must be *measured*, not assumed. `h≈0` ⇒ use a cascade instead.
- Non-reasoning spec-writer assumed for the headline; a reasoning writer on the MISS path can make cold cost exceed baseline.
- Reuse correctness rides on test quality; adopt AgentCoder-style independent tests and re-validate reused tests against the new contract.
- Open: right thresholds (τ_reuse, τ_prime) and repair cap N; skill TTL/versioning as conventions drift; how large a single-pass task the cheap model handles before quality collapses (where decomposition would return).
