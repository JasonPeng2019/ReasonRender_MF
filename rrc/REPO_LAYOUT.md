# RRC Repository Organization Layout

Keep the RRC package flat. Lane B is a small sequential integration; it does
not need provider, memory, or harness subpackages.

```text
rrc/
├── __init__.py                 # Package marker and public package version only.
├── py.typed                    # PEP 561 marker.
├── contract.py                 # Frozen shared types and ports. No lane logic.
├── pipeline/                   # Lane A only; Lane B must not edit or import internals.
│   ├── __init__.py
│   ├── prompts.py
│   ├── solve.py
│   ├── stages.py
│   ├── stubs.py
│   ├── template.py
│   └── verify.py
├── model.py                    # Lane B: one-shot Codex CLI completion and token parsing.
├── everos.py                   # Lane B: thin /api/v2/memory HTTP functions and readiness wait.
├── store.py                    # Lane B: SQLite Template storage keyed by external_ref.
├── memory.py                   # Lane B: EverOSRetrieval joins EverOS hits to SQLite templates.
├── workload.py                 # Lane B: 2-3 interleaved repeating task families.
├── run.py                      # Lane B: cold/warm arm runner, local metrics, CLI entry point.
├── sink.py                     # Lane B: Snowflake per-task inserts and optional curve query.
└── lane_b/                     # Lane B implementation documentation; no runtime code.
    ├── Workflow.md             # Fresh external-DeepSeek-worker workflow; serial by default.
    ├── SUBAGENT_PROTOCOL.md    # Bounded plan/spec/read-list packet and result format.
    ├── SHAVE_IF_LOW_TIME.md    # Required two-hour scope-cut list for incomplete work.
    └── PLAN.md                 # Four-hour milestones, scope cuts, and handoff checks.

tests/
└── lane_b/
    ├── test_model.py           # JSONL parsing and Codex command construction.
    ├── test_everos.py          # HTTP request/response and index-readiness behavior.
    ├── test_store.py           # SQLite Template serialization and external_ref lookup.
    ├── test_memory.py          # EverOS hit -> SQLite Template retrieval behavior.
    ├── test_workload.py        # Repetition, interleaving, and oracle-test isolation.
    ├── test_run.py             # Injected fake solver, token totals, cold/warm curve.
    └── test_sink.py            # Per-task Snowflake row shape and SQL construction.

local-config/rrc/               # Ignored local Codex, EverOS, and Snowflake settings.
runtime/rrc/<run-id>/           # Ignored JSONL, response evidence, CSV, and run output.
```

## Placement rules

- `contract.py` is the only shared interface. Lane B consumes it and does not
  add compatibility types elsewhere.
- `model.py`, `everos.py`, and `memory.py` are independent adapters. Keep
  orchestration out of them. `store.py` is the only owner of SQLite access.
- The checked-out `EverOS/` submodule owns the small `external_ref` patch:
  add DTO, persistence metadata, and episode-search response field. RRC must
  not substitute a session-id/family join for that patch.
- `run.py` is the only Lane B module that combines the model, memory, workload,
  and injected `solve` function.
- `sink.py` receives completed outcomes; it does not call Codex, EverOS, or
  `solve`.
- `lane_b/` contains decisions and operating notes only. `Workflow.md` and
  `SUBAGENT_PROTOCOL.md` define the fresh-worker orchestration loop. It is
  serial by default and permits only explicit dependency-free parallel work.
  `TEST_PLAN.md` defines the separate user-authorized live test procedure.
  Put executable Lane B code directly in `rrc/` so the ship-fast path stays
  easy to follow.
- Tests use fakes or recorded external responses by default. Live Codex,
  EverOS, and Snowflake checks are explicit smoke runs and write only to
  `runtime/rrc/<run-id>/`. They are user-authorized validation work; the
  coding orchestrator and external DeepSeek workers do not launch them.
