# Lane B Live EverOS Handoff

## What works now

Lane B's storage and retrieval path was exercised against a real local EverOS
server and a real SQLite template store. The run used the current
`OrchestratorRuntime` and `EverOSClient` (not legacy `rrc.run`).

The observed normal path was:

1. Task 1 searched by stable `case_shape`, missed, called the planner once and
   the worker once, stored a generic packet in SQLite, and indexed its opaque
   `external_ref` in EverOS.
2. Task 2 used the same `case_shape` with different slot values, found the
   same `external_ref`, skipped the planner, and called the worker once more
   with the new locally rendered values.

The recorded product checks were all true:

- task 1 was a miss;
- task 2 was a hit using the same `external_ref`;
- planner calls stayed at one and worker calls reached two;
- EverOS requests used `method: "keyword"`;
- EverOS received the case shape and opaque reference, never either task's
  dynamic slot values;
- the second worker prompt contained only task 2's values.

The local evidence is at
`runtime/rrc/lane-b-real-everos-0fdcd77183/summary.json`. Its top-level
status says `FAIL` only because its `first_planner_1_worker_1` assertion was
evaluated after task 2, when the correctly accumulated worker count was two.
That is a test-harness bookkeeping error, not a product failure; every actual
Lane B behavior check in that same file succeeded.

The deterministic focused verification also passed:

```text
python -m pytest -q tests/test_orchestrator_runtime.py
2 passed
```

## How to run a future EverOS-backed task

1. Start EverOS on WSL/Linux. The checked-out source imports Unix `fcntl`, so
   native Windows startup is unsupported. Confirm `GET /health` has
   `status: "ok"`, `cascade.healthy: true`, and `cascade.pending: 0`.
2. Configure an EverOS LLM provider. Embeddings and reranking are optional for
   Lane B's exact template lookup. With no embedding provider, EverOS correctly
   disables hybrid search but continues to support keyword search.
3. Build a `Task` with a generic `case_shape` containing named placeholders and
   a separate `slot_values` map. Put only the reusable shape in EverOS; retain
   task-specific values locally.
4. Construct `OrchestratorRuntime` with a `SQLiteTemplateStore`,
   `EverOSClient`, a planner completion, and a worker completion. Call
   `runtime.run(task)`.
5. On a miss, require the planner to return a generic `PlanSpecPacket`; the
   runtime renders it locally for the worker, saves it in SQLite, then indexes
   only `case_shape` plus `external_ref` in EverOS. On a hit, SQLite supplies
   the generic packet and the planner must not run.

Use `EverOSClient.search()`'s keyword mode for this exact-shape index. Hybrid
search needs embeddings and is not necessary for the minimum Lane B contract.

## Model-provider note

The native Codex CLI is authenticated and works when it uses its configured
default model. A host must pass a model that its own Codex account supports.
For a fully live planner, give Codex an enforced JSON schema
for `PlanSpecPacket` (or validate/retry at the host boundary): some general
models emit incomplete packet fields even though the Lane B runtime correctly
rejects them.

This provider-format concern does not change the verified Lane B storage,
privacy, rendering, or exact-reuse behavior.
