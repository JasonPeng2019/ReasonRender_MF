# Lane B Coding Subagent Protocol

## Roles

- **Coding orchestrator:** owns coding slice order, scope, and code review.
  It is not the RRC runtime and does not use EverOS or execute tests.
- **`luna-xhigh-fast` coding subagent:** a fresh worker for one coding task. It
  reads the supplied files, edits code, returns a code-result packet, and exits.

No harness manages workers. Normal mode is serial: one active coding subagent,
code review, then a new fresh subagent. No real test or runtime subagent is
selected by this protocol.

## Required coding packet

Each packet must be self-contained and contain only:

```text
Task: <short coding slice name>
Elapsed build time: <HH:MM since the Lane B timer started>
Plan: <one bounded code change and why it is next>
Spec: <behaviour, non-goals, and acceptance criteria>
Write paths: <explicit files the subagent may change>
Read first: <3-5 initial file paths, in priority order>
Do not run: EverOS, tests, RRC runner, models/subagents, or Snowflake
Stop when: <code slice is complete or real validation is the next step>
Execution mode: serial | parallel (only when expressly authorized)
```

The coding orchestrator gives a fresh plan and spec every time. It may provide
a small static product-spec excerpt, but never a live EverOS response, runtime
memory, or general project-memory dump.

At or after two hours, `rrc/lane_b/SHAVE_IF_LOW_TIME.md` replaces one ordinary
`Read first` entry and must be read before coding. The subagent must not start
an incomplete item listed there and must report it as cut; the orchestrator
must not send it in a later packet.

## Coding subagent rules

1. Read only the supplied `Read first` files before editing. Do not scan the
   repository, redesign the architecture, or self-assign another task.
2. Change only the allowed write paths. If one is missing, complete the rest
   and report `done_degraded`; the next packet can add it.
3. Do not call EverOS, start a server, execute tests, run `rrc`, invoke a
   model or real subagent, or write to Snowflake. Static code and diff review
   are the only permitted verification in this loop.
4. Orchestrator, packet, process-scaffold, tool, or evidence failures are
   non-blocking. Record them and finish the viable code change.
5. If the next useful action is real validation, return `ready_for_user_test`.
   Stop; only the user can authorize the real test and choose its subagent.

## Required code-result packet

```text
Status: done | done_degraded | ready_for_user_test
What changed: <short behaviour-level summary>
Files changed: <paths>
Static review: <diff/contract observations; no executed checks>
Operational errors: <non-blocking coding/process/scaffold issues>
Potential product risks: <unverified risks, never a confirmed defect>
User test handoff: <none, or proposed live checks/services/cost>
Recommended next coding slice: <one sentence; orchestrator decides>
```

The coding orchestrator reviews the diff and either sends another coding packet
or returns `ready_for_user_test`. It never converts an untested risk into a
product-defect claim and never launches a real test automatically.

## Parallelization exception

The coding orchestrator may set `Execution mode: parallel` only for tasks with
disjoint write paths, independent acceptance criteria, no shared runtime state,
and a stated integration order. Each task still gets a fresh Luna subagent and
a small packet. Otherwise coding remains serial.

## Curated read-list examples

| Coding slice | Initial files to read |
| --- | --- |
| EverOS `external_ref` patch | `docs/RRCv2-lane-B-memory-measure.md`, `EverOS/src/everos/entrypoints/api/routes/memorize.py`, `EverOS/src/everos/service/memorize.py`, `EverOS/src/everos/memory/search/dto.py`, `EverOS/src/everos/memory/search/shaper.py` |
| SQLite template store | `docs/RRCv2-lane-B-memory-measure.md`, `rrc/contract.py`, `rrc/pipeline/solve.py`, `tests/pipeline/test_contract.py` |
| EverOS/SQLite retrieval join | `docs/RRCv2-lane-B-memory-measure.md`, `rrc/contract.py`, `rrc/pipeline/solve.py`, `rrc/REPO_LAYOUT.md` |
| Workload and runner | `docs/RRCv2-lane-B-memory-measure.md`, `rrc/contract.py`, `rrc/pipeline/solve.py`, `tests/pipeline/test_solve.py` |

These are starting lists, not standing context. The coding orchestrator selects
only the row and files appropriate to the immediate slice.
