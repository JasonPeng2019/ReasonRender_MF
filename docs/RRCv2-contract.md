# RRCv2 — Shared Contract (the frozen seam)

This is the **only** surface the two implementation lanes share. Both lanes import it; neither lane puts logic here. The design rule that keeps the lanes from clashing:

> **Golden rule.** `rrc/contract.py` is frozen. Any change to it is a *coordination sync-point* — stop, agree, edit once, both lanes re-pull. Everything else in each lane is private and may be built, renamed, and refactored freely without touching the other lane.

Domain rationale for every type below lives in `RRCv2.md` (the full spec); this file is the machine-checkable interface, not a re-explanation.

---

## Model providers & the prototyping→final swap

All model access goes through one interface, `ModelPort`. There are **three** implementations, selected by a single switch — no pipeline code changes between them:

| Provider | `RRC_MODEL_PROVIDER` | When | Cost captured? |
|---|---|---|---|
| **`FakeModel`** | `fake` | Lane A unit tests — canned, deterministic, offline | no (zeros) |
| **`CodexModel`** | `codex` *(default)* | **All prototyping & integration** — the agent/runner that actually produces specs/code | best-effort / approximate |
| **`CortexModel`** | `cortex` | **Final testing only**, once the arms pass functionally on Codex | yes — inline `usage`, the money-shot curve |

**Swapping is one variable.** `make_model(cfg)` (Lane B) dispatches on `RRC_MODEL_PROVIDER` (or `cfg.model_provider`). Prototype everything with `codex`; when arms pass functionally, set `cortex` and re-run for the metered cost curve. No path edits, no code edits — just the env var (unit tests construct `FakeModel` directly).

**Two consequences to hold in mind (invariants 7–8 below):**
- The **control loop is identical across providers** — a provider only ever answers a single `complete()` call; repair/escalate/branch logic lives in `solve()`. This is what keeps the arm comparison valid and lets "passed on Codex" mean the plumbing is correct.
- **"Passed on Codex" ≠ "passed on Cortex" for pass@1 / cost.** Codex validates logic and wiring; the final correctness rate and the economics must be re-confirmed on Cortex because model behaviour and the strong/small split differ.

---

## Coordination at a glance

**Dependency direction (one-way):** `contract ← Lane A`, `contract ← Lane B`, and at runtime **Lane B injects its port implementations into Lane A's `solve()`.** Lane A never imports Lane B. Lane B imports Lane A only at the final wiring point (the arms runner), and even that is stubbed during development.

**Ownership matrix**

| Concern | Lane A (Pipeline) | Lane B (Memory · Measure · Harness) |
|---|---|---|
| `solve()` control flow, arm routing | ✅ implements | consumes |
| SPEC / PRIME / IMPLEMENT / repair prompts + parsing | ✅ | — |
| VERIFY tiers (ruff / pyright / pytest), sandbox | ✅ | — |
| templatize / render / structural_match / choose_branch / sanity | ✅ | — |
| `ModelPort` providers (`CodexModel`, `CortexModel`) + `make_model` factory | consumes | ✅ implements |
| `RetrievalPort` (EverOS client, own KV store, `external_ref`) | consumes | ✅ implements |
| EverOS `external_ref` fork patch (separate repo) | — | ✅ |
| Cost curve, hit-rate, Snowflake reconciliation, prices | — | ✅ |
| Workload generator, arms runner, demo | — | ✅ |

**Mutual stubs (how each lane runs alone)**

- Lane A ships **`FakeModel`** (a `ModelPort` with canned `Completion`s) and uses the shared **`NullRetrieval`**. With these it runs arms `baseline / cheap_alone / cascade / cold` end-to-end, offline, no Codex/Cortex, no EverOS. For real end-to-end prototyping, the harness injects `CodexModel` unchanged.
- Lane B ships **`FakeSolve`** (a stand-in for `solve()` returning a canned `SolveOutcome` + `Template`) and **`FakeEverOS`** (in-proc). With these it builds retrieval, thresholds, the curve, hit-rate, and the arms wiring without real models or Lane A.

**Merge plan (what the merge AI wires):** delete the stubs; in the arms runner, obtain the model from `make_model(cfg)` (Codex during prototyping, Cortex for final), pass `NullRetrieval` for the cold arms and `EverOSRetrieval` for the warm arm, and call Lane A's real `solve`. Because both coded to this contract, the swap is drop-in. The only file both edit is a test-fixture directory; no shared source module carries logic from both lanes.

---

## The contract

```python
# rrc/contract.py  —  FROZEN. Edits are a coordination sync-point (see Golden rule).
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol

# ─────────────────────────── Core data types ───────────────────────────

@dataclass(frozen=True)
class Task:
    task_id: str                       # stable within a run, e.g. "0007"
    text: str                          # NL task description; drives retrieval + slot-fill
    oracle_tests: Optional[str] = None # benchmark oracle tests for pass@1 (EVAL ONLY;
                                       # never shown to any stage — see RRCv2 §6)

@dataclass(frozen=True)
class Slots:
    # The SPEC stage labels its own slots (RRCv2 §2). render()/extract_slot_values()
    # are pure lookups over `values`; the category tuples are for structural_match().
    entity: Optional[str] = None
    identifiers: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    constants: tuple[str, ...] = ()
    edge_values: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)   # label -> concrete value

@dataclass(frozen=True)
class Spec:
    plan: str
    signature: str
    contract: str
    tests: tuple[str, ...]
    slots: Slots

@dataclass(frozen=True)
class Template:
    external_ref: str          # RRCv2-owned join key: fingerprint over the templated form
    spec_skeleton: Spec        # instance values genericised to named placeholders
    slot_names: tuple[str, ...]

class BranchDecision(str, Enum):
    REUSE = "reuse"            # structural EXACT match -> render deterministically
    PRIME = "prime"           # structural NEAR match  -> cheap model adapts w/ few-shot
    MISS  = "miss"            # nothing usable          -> expensive spec

class ArmMode(str, Enum):
    BASELINE    = "baseline"     # strong role solves end-to-end
    CHEAP_ALONE = "cheap_alone"  # small role implements + verify/repair; no spec, no memory
    CASCADE     = "cascade"      # cheap_alone, escalate whole task to strong role on fail
    COLD        = "cold"         # spec->cheap->verify; retrieval is Null
    WARM        = "warm"         # retrieve/reuse/prime + spec->cheap->verify + store

class ModelRole(str, Enum):
    # Lane A asks by ROLE, never by a provider-specific model name. Each ModelPort
    # implementation maps role -> its own concrete model (Codex models, Cortex models, …),
    # so switching provider needs no change in Lane A.
    STRONG = "strong"          # SPEC / PRIME / baseline / cascade-escalate
    SMALL  = "small"           # IMPLEMENT / repair

@dataclass(frozen=True)
class Candidate:
    external_ref: str          # surfaced by Lane B (patch, or id-capture fallback)
    score: float               # EverOS fused score in [0,1]

@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage               # real on Cortex; best-effort/zero on Codex/Fake
    model: str                 # concrete model the provider actually used (for CostEvent.model)

@dataclass(frozen=True)
class CostEvent:
    arm: str
    task_id: str
    stage: str                 # "spec" | "prime" | "implement" | "repair" | "slotfill"
    model: str                 # == Completion.model
    usage: Usage
    provider: str              # "codex" | "cortex" | "fake" — so the curve can gate on it

@dataclass(frozen=True)
class SolveOutcome:
    task_id: str
    arm: str
    code: str
    passed: bool                       # against the spec's own tests
    pass_at_1: Optional[bool]          # against Task.oracle_tests, if provided (EVAL)
    branch: BranchDecision
    repairs: int
    escalated: bool
    template: Optional[Template]       # templatized spec to store (cold/warm/miss)
    cost_events: tuple[CostEvent, ...] # every model call this task incurred

@dataclass
class RunContext:
    arm: str
    task_id: str
    def tag(self, stage: str) -> str:  # QUERY_TAG assembled here; Lane B reconciles on it
        return f"rrc:arm={self.arm};task={self.task_id};stage={stage}"

@dataclass
class Config:
    # ── Lane A reads ──
    repair_cap_N: int = 2
    pyright_mode: str = "basic"               # NOT strict (RRCv2 §3)
    # ── Lane B / factory reads ──
    model_provider: str = "codex"             # "fake" | "codex" (proto) | "cortex" (final)
                                              # overridden by env RRC_MODEL_PROVIDER
    tau_floor: float = 0.35                   # EverOS min_score (MISS floor); calibrate
    top_k: int = 3
    prefer_prime_on_shape_diff: bool = True   # RRCv2 §4.6
    everos_base: str = "http://127.0.0.1:8000/api/v2/memory"
    app_id: str = "default"
    project_id: str = "default"
    agent_identity: str = "rrc"               # used as user_id on the episode track
    # NOTE: concrete model names (which Codex/Cortex model fills STRONG vs SMALL) live
    # INSIDE each provider (Lane B), overridable via env — NOT here — so a provider swap
    # never touches Lane A or this shared Config.

# ─────────── Ports: implemented by Lane B, consumed by Lane A ───────────

class ModelPort(Protocol):
    # A provider answers ONE completion per call. It maps `role` to its own model,
    # sets QUERY_TAG = ctx.tag(stage) (Cortex), captures usage, and returns a Completion.
    # It NEVER runs the repair/branch loop — that stays in solve() (invariant 7).
    def complete(self, role: ModelRole, prompt: str, ctx: RunContext, stage: str) -> Completion: ...
    provider: str              # "codex" | "cortex" | "fake"

class RetrievalPort(Protocol):
    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]: ...      # {external_ref, score}
    def get_template(self, external_ref: str) -> Optional[Template]: ...     # from RRCv2's own store
    def store(self, task: Task, template: Template, outcome: SolveOutcome) -> None: ...

# ────────────── Trivial null impls (shared; cold arms + dev) ─────────────

class NullRetrieval:
    def retrieve(self, task, cfg): return []
    def get_template(self, external_ref): return None
    def store(self, task, template, outcome): return None

# ───────────── The single entry point: implemented by Lane A ─────────────

def solve(task: Task, *, mode: ArmMode, model: ModelPort,
          retrieval: RetrievalPort, cfg: Config, ctx: RunContext) -> SolveOutcome: ...
```

---

## Invariants both lanes must honour (so behaviour never clashes)

1. **`external_ref` is minted by Lane A's `templatize()`** (a fingerprint over the templated form) and is the *only* join key. Lane B stores/retrieves templates under it and never invents its own id scheme.
2. **EverOS content is never the artifact.** Lane B returns `{external_ref, score}` from `retrieve()`; the `Template` always comes from `get_template()` (RRCv2's own store). Lane A never reads spec text off a search hit.
3. **`solve()` owns all control flow**, including repair/escalate and arm routing. Lane B never re-implements the loop; it only supplies ports and iterates `solve()` across the task stream.
4. **A "hit"** = `branch ∈ {REUSE, PRIME}` **and** `escalated == False`. Both lanes compute hit-rate from `SolveOutcome` this one way (RRCv2 §6).
5. **`oracle_tests` are eval-only.** Lane A must never feed `Task.oracle_tests` into any prompt; they exist solely for Lane B's pass@1 scoring.
6. **QUERY_TAG format is fixed** by `RunContext.tag()`. Lane B's Cortex wrapper writes it; Lane B's reconciliation reads it. Don't reformat it in either place.
7. **One control loop for every provider.** A `ModelPort` answers a single `complete()` call and nothing more — no internal agentic multi-turn, no repair loop. Whether the backend is Codex, Cortex, or Fake, the branch/repair/escalate sequencing in `solve()` is byte-for-byte the same, so cross-arm and cross-provider comparisons stay apples-to-apples.
8. **Cost is provider-scoped.** The cost-per-solved curve is only meaningful when `provider == "cortex"` (metered inline `usage`). Under `codex`/`fake`, `Usage` is best-effort or zero and used only to exercise the plumbing; the curve code must gate on `CostEvent.provider` and refuse to present Codex/Fake numbers as the economic result.
