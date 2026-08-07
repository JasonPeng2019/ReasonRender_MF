# RRCv2 — Ship-It-Fast Plan (4h, two lanes) — Codex CLI + EverOS + Snowflake

> **SUPERSEDED CONTRACT PROFILE.** The family/params `Complete`/`Memory`/`Outcome` shortcut below
> is retained only as historical context. Do not implement it. The authoritative shared seam is
> `rrc/contract.py` from the full two-store design recorded in
> [`decisions/0001-rrcv2-full-two-store-contract.md`](decisions/0001-rrcv2-full-two-store-contract.md),
> with the active lane profiles in `RRCv2-lane-A-pipeline.md` and
> `RRCv2-lane-B-memory-measure.md`.

**Goal (the only one):** lower **tokens per solved task**, measured. Win = a curve: warm (reuse stored specs via EverOS) tokens/solved falls below cold (no memory) as the workload repeats.

**Three hard requirements (none optional):** model backend = **Codex CLI (subscription, headless `codex exec`)**; memory = **EverOS**; cost-of-record = **Snowflake**.

**Build order (do in this sequence):**
1. **Milestone 1 — EverOS + Codex CLI end-to-end**, measured locally from Codex's own token counts. This is the whole core loop + the cold-vs-warm curve printed.
2. **Milestone 2 — Snowflake** as the cost-of-record: log per-task tokens, compute the curve in SQL.

**Record start time here → ____:____ .** Gates are on elapsed time.

---

## Historical tiny shared contract (SUPERSEDED; do not implement)

```python
# rrc/contract.py  — FROZEN
from dataclasses import dataclass
from typing import Optional, Protocol, Callable

@dataclass
class Task:
    task_id: str
    family: str          # retrieval key; also the EverOS session_id (the join trick)
    params: dict[str, object] # JSON-like slot values; non-strings render as canonical JSON
    text: str            # NL description; fed to Codex AND embedded by EverOS for matching
    oracle_tests: str = "" # EVAL ONLY — run separately and never shown to a model

@dataclass
class Spec:              # stored as named templates so reuse is replacement, not NLP
    signature: str
    template: str        # spec body with {param} placeholders
    tests: str           # pytest snippet with {param} placeholders

@dataclass
class Outcome:
    task_id: str; warm: bool; passed: bool; reused: bool
    spec_tokens: int; impl_tokens: int; repair_tokens: int
    oracle_passed: Optional[bool] = None
    @property
    def total(self): return self.spec_tokens + self.impl_tokens + self.repair_tokens

# model call -> (text, total_tokens). Lane B shells to `codex exec`; Lane A calls it.
Complete = Callable[[str, str], tuple[str, int]]     # (prompt, model) -> (text, tokens)

class Memory(Protocol):  # Lane B implements with EverOS; Lane A calls
    def get(self, task: "Task") -> Optional[Spec]: ...   # semantic match on task.text
    def put(self, task: "Task", spec: Spec) -> None: ...

class NoMemory:          # cold arm (shared)
    def get(self, task): return None
    def put(self, task, spec): pass

# Lane A implements:
def solve(task: Task, *, warm: bool, complete: Complete, memory: Memory,
          strong: str, cheap: str) -> Outcome: ...
```

Note vs. the previous draft: `Memory.get/put` take the whole `Task` (EverOS matches on
`task.text`, not an exact family key). The clarifications below define oracle reporting, malformed
SPEC behavior, deterministic structured params, and ACCEPT-only storage.

`passed` means the final implementation passes the model-authored spec tests. If
`oracle_tests` is non-empty, Lane A runs it separately against the final code and records
`oracle_passed`; the oracle is never included in SPEC, IMPLEMENT, or repair prompts. A malformed
SPEC response returns a failed `Outcome` with its spec tokens recorded and skips implementation.
`params` values must be JSON-like (scalars, mappings, and sequences); Lane A renders non-string
values as sorted, compact JSON so reuse is deterministic.

---

## Who owns what

| | Lane A | Lane B |
|---|---|---|
| `solve()` loop, spec/implement/repair, pytest verify, `render` | ✅ | — |
| `complete()` = `codex exec --json` wrapper (text + tokens) | calls | ✅ |
| `Memory` = EverOS episode track (`session_id=family` trick) | calls | ✅ |
| workload generator, run harness, cold-vs-warm curve | — | ✅ |
| Snowflake logging + curve SQL (milestone 2) | — | ✅ |

One-way dependency: Lane B injects `complete`/`memory`/`solve`; Lane A never imports Lane B.

**No stub modules.** Fake the other side in one line: Lane A tests with `complete=lambda p,m:("code",100)`, `memory=NoMemory()`. Lane B tests the harness with a `solve=lambda **k: Outcome(...)`.

---

## Codex CLI facts that shape the build (confirm with `codex exec --help`)

- Invoke: `codex exec --json --skip-git-repo-check --sandbox read-only [--model M] "PROMPT"`.
- `--json` → JSONL on stdout; the **final agent message** is the completion text, and the **`turn.completed`** event carries token usage → that's the meter. (Fallback if parsing is fussy: `-o msg.txt` for the text + tiktoken as a proxy count.)
- Progress goes to **stderr**; only the final message to stdout.
- **Auth = subscription** (`codex login` with your ChatGPT account); no API key. Keep model calls **single-shot**: read-only sandbox + a prompt that says "output only X, don't run commands or edit files," so Codex doesn't wander into extra turns (extra turns = extra tokens + latency).
- Codex is **slow** vs an API. Keep the workload small (~12–20 tasks) and pre-bake the run for the demo.
- `strong` vs `cheap`: use two `--model`/reasoning-effort profiles if your subscription exposes them; if not, use one model for both — the amortization win (skipping the whole spec call on reuse) still shows the drop.

---

## 4-hour schedule (scope only narrows)

- **0:00–2:00 Build.** Lane A: `solve` cold path + implement + pytest verify on 2 hardcoded tasks with a fake `complete`. Lane B: **get `codex exec` returning `(text, tokens)` first** (riskiest integration), then `EverOSMemory` (add/flush/search with `session_id=family`), then `gen_workload`.
- **2:00 Cut & sprint.** Freeze scope. No PRIME, no second model unless already working, no Snowflake yet.
- **3:00 Consolidate = Milestone 1 done.** First 5–10 min: wire real `complete` + real `solve` + `EverOSMemory`; get **warm + cold end-to-end** on the full stream, curve printed locally from Codex tokens. Then add nothing to the core.
- **Final hour Lock & validate + Milestone 2.** Pipe the per-task metrics into Snowflake, compute the tokens-per-solved curve in SQL, confirm warm < cold and falling. Logged numbers are the evidence.

Mid-feature at a gate → drop it.

## De-scope ladder (drop top-down if behind)

1. Snowflake curve SQL → just `INSERT` the rows (requirement met) + print/plot the curve in Python
2. Codex `--json` token parse → `-o` for text + tiktoken proxy count (consistent across arms)
3. plot → CSV + printed warm-vs-cold means
4. repair loop → 0 repairs (record pass/fail as-is)
5. two models → one Codex model for both stages
6. 3 families → 2 families

EverOS is a requirement, so it is **not** on the ladder; if EverOS is down during dev, a local dict behind the same `Memory` class unblocks you, but the shipped milestone 1 must use EverOS.

---

## Measure (the deliverable)

Per task log: `reused`, `spec_tokens`, `impl_tokens`, `repair_tokens`, `total`, `passed`,
`oracle_passed`, `arm`. For benchmark tasks, report **mean tokens per oracle-passing task,
warm vs cold** and the running curve over task order; fall back to `passed` only when no oracle is
provided. On reuse, `spec_tokens = 0` — that's the win. A flat warm curve = the workload didn't
repeat (fix the generator) or EverOS isn't matching (lower `min_score` / check index lag).

Lane A writes a newly generated template to memory only after the implementation passes the spec
tests (including after a successful repair). Failed or malformed attempts never seed memory.

## EverOS join trick (why there's no fork patch)

Store each family under `session_id = family`; keep the actual `Spec` in a local dict keyed by family. On `get`, search EverOS by `task.text`; the top episode hit carries its `session_id` — that IS the matched family — so look up the local dict. Real semantic retrieval (BM25+vector) with a trivial, synchronous join and no patch. Mind the index lag: a just-stored family may need a beat before it's searchable (poll `GET /health` `cascade.pending`==0, or interleave families).
