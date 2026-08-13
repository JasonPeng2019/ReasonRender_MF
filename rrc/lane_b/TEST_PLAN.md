# Lane B Live Test Plan

## Goal

Establish whether the normal Lane B path works end to end:

```text
EverOS external_ref -> SQLite Template -> EverOS search -> SQLite join
-> render new slots -> WARM reuse with zero SPEC tokens
```

This is a user-authorized live test plan. It does not authorize source edits.

## Test topology

```text
test orchestrator (this session): packet, supervision, final triage
    -> one fresh external DeepSeek test executor: all live checks, one run directory, full logs
    -> exits
test orchestrator: read artifacts and make one pooled findings list
```

There is no harness, nested test manager, parallel worker, or repair agent in
this run. The executor is a fresh external `deepseek-v4-flash:0731-cloud`
worker launched through the checked-in DeepSeek delegate workflow. It may write
only under `runtime/rrc/<run-id>/`; it must not edit source or EverOS.

## Executor packet

```text
Task: Run the complete Lane B live validation and preserve all evidence.
Plan: Run every check below that its dependencies permit. Do not repair code.
Spec: Test real EverOS external_ref round trip, SQLite exact-template storage,
      stale-ref MISS behavior, and one real two-task COLD/MISS -> WARM/HIT run.
      Keep going after an independent failure; label blocked checks instead of
      stopping the test.
Write paths: runtime/rrc/<run-id>/ only.
Read first:
  1. rrc/lane_b/TEST_PLAN.md
  2. rrc/lane_b/VALIDATION_HANDOFF.md
  3. rrc/run.py
  4. rrc/everos.py
  5. rrc/memory.py
Do not: edit code, change EverOS data outside the RRC namespace, write
        Snowflake, launch another agent, or retry a paid model call.
Stop when: all runnable checks have a log and the artifact manifest is written.
Execution mode: serial.
```

## Preconditions

1. The user chooses an available Codex model and approves two real Codex
   completions for the end-to-end proof.
2. EverOS is started from the patched checkout. Its `/health` endpoint must
   report a healthy cascade before the proof.
3. Create one unique run directory under `runtime/rrc/`, and save every
   command, stdout, stderr, exit code, timestamp, and redacted environment
   summary there.

## Checks, in order

Run later checks even when an earlier independent check fails. A missing
dependency marks only its dependent checks `blocked_infrastructure`.

1. **Preflight** — record the RRC and EverOS revisions, selected model name,
   `codex --version`, and two healthy EverOS `/health` samples. This makes
   configuration failures diagnosable without consuming a model call.
2. **SQLite exact-template round trip** — in the run directory, store a known
   generic `Template`, reload it by `external_ref`, and verify its three Spec
   fields and ordered slot names are unchanged.
3. **Live EverOS external-ref round trip** — use a unique probe task shape and
   reference; call `add`, `flush`, and readiness wait; search the same unique
   shape. Record the returned candidate ref and score. It passes only when the
   exact non-empty probe ref returns.
4. **Stale-ref safety** — retain the EverOS probe but use a fresh SQLite store
   with no corresponding row. `EverOSRetrieval.get()` must return a MISS rather
   than inventing or accepting an artifact.
5. **Live end-to-end proof** — run `python -m rrc.run` once with the approved
   model. Capture its process outcome. If `evidence.json` exists, preserve it
   unchanged and inspect it; do not rerun a paid completion merely to improve
   the result.
6. **Evidence audit** — the proof passes only if `evidence.json` says
   `pass: true`, the stored and retrieved refs match, the first task has
   `reused: false`, the second has `reused: true` and `spec_tokens: 0`, both
   tasks pass generated and oracle tests, and the rendered second spec has the
   second task's values.
7. **Artifact manifest** — write `summary.json` containing every check's
   status (`pass`, `fail_product_candidate`, or `blocked_infrastructure`),
   the evidence-file paths, ref values, token totals, and a short failure
   excerpt. Do not overwrite raw logs.

## Triage after the executor exits

The test orchestrator reads the manifest, logs, `evidence.json`, and SQLite
artifact. It creates one pooled list before any source change:

- **Product candidate:** a minimal reproduction shows a wrong normal path —
  missing/wrong `external_ref`, wrong SQLite join, wrong namespace, incorrect
  rendering, or nonzero WARM SPEC cost.
- **Infrastructure:** service startup/health, LanceDB rebuild, network,
  authentication, unavailable model, CLI/process, or test-script failure.
- **Not reached:** a check blocked by an earlier infrastructure dependency.

Only pooled product candidates receive serial repair packets. Infrastructure
findings are recorded and resolved separately; they never erase the other test
evidence or block its remaining independent checks.

## Cost and stop boundary

The normal proof uses exactly two real Codex completions. The executor must not
perform automatic retries, expanded workloads, Snowflake writes, or a second
paid proof. Any extra live run requires fresh user approval.
