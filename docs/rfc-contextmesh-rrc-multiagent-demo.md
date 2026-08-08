# RFC — Combined ContextMesh + ReasonRenderCoding multi-agent demo

## Goal

Replace the mistaken standalone two-function `RRDdemo.sh` proof with a real combined demo:
OpenCode orchestrates one worker subagent per HTTP handler, ContextMesh removes repeated shared-file
context and compresses large worker results, and ReasonRenderCoding supplies a validated reusable
Plan + Spec packet at each `task` boundary.

## Non-goals

- Do not replace OpenCode workers with direct Codex completions.
- Do not run the fixed `return_two` / `return_three` Lane-A proof from the website demo.
- Do not make the primary orchestrator read or edit source files.
- Do not change the ordinary `demo.sh` stock-vs-ContextMesh experiment.
- Do not claim audit correctness merely because token/reuse evidence is present.

## Constraints and grounded current wiring

- `contextmesh/demo.sh` already launches a primary `orchestrator` that calls OpenCode's `task` tool
  for one `worker` per handler (`contextmesh/configs/arm-{a,b}.json`).
- `contextmesh/plugin/contextmesh.ts` already hooks `read` output and completed `task` output.
- The current RRD configs instead select a tool-only `reasonrendercoding` primary agent and therefore
  cannot call `task` or create children (`contextmesh/configs/rrd-arm-{a,b}.json`).
- `rrc.orchestrator_runtime.OrchestratorRuntime` is the existing reusable Plan + Spec controller. It
  accepts injected planner/worker completions, stores exact packets in SQLite, and indexes only the
  stable case shape plus `external_ref` in EverOS.
- All four handler assignments have one stable shape and one dynamic `{handler}` slot. Dynamic paths
  must never enter the generic planner prompt or EverOS index.
- Both arms must use the same OpenCode model, agents, audit prompt, target tree, and ContextMesh
  settings. The only experimental difference is RRC packet mode: COLD versus WARM.

## Approach

### Runtime topology

```text
OpenCode orchestrator
  -> four task calls emitted together in one assistant turn (four-at-once)
       -> ReasonRenderCoding tool.execute.before for task
            COLD: planner MISS for each assignment
            WARM: one locked MISS, then exact packet HITs for sibling assignments
            -> prepend rendered Plan + Spec packet to that task prompt
       -> real OpenCode worker subagent
            -> ContextMesh read hooks (shared-file digests)
       -> ContextMesh task.after (large-result compression)
  -> merged final audit report
```

Both RRD configs load both plugins and restore the primary/subagent roles from the ContextMesh demo,
but deliberately replace its current two-wave cap with an explicit four-at-once instruction: the
orchestrator must emit all four foreground `task` calls in one assistant turn before receiving any
result. The RRC plugin no longer exposes a one-shot tool. It intercepts only `task` calls whose prompt
contains exactly one assigned `src/handlers/...` path, invokes a small Python bridge, and mutates the
worker prompt in place. Any RRC failure fails open to the original worker prompt but records an
error event, so multi-agent execution is never silently disabled.

The Python bridge extracts exactly one handler path but does **not** derive reusable shape from the
LLM-authored task text. It uses one trusted, checked-in audit case shape containing `{handler}` and
passes `slot_values={"handler": concrete_path}` to the existing `OrchestratorRuntime`; the original
task text is preserved separately for the OpenCode worker. A no-op worker adapter returns the
runtime's rendered packet; the actual worker is OpenCode. A demo-local `AuditPacketPolicy` preserves
the strict Plan + Spec and slot contracts while allowing an empty `write_paths` list and restricting
every worker-facing Plan/Spec clause to a finite controller-owned audit-action catalog. The planner
selects and orders safe clauses but cannot append source-writing instructions. The actual worker
receives an explicitly labelled audit packet, not the runtime's implementation-oriented
`Product worker input` wrapper.

The planner is a Codex JSONL completion constrained by a task-specific JSON Schema. Its artifact
log records prompt, raw stdout/stderr, exit code, parse status, response, and usage even when the CLI
or schema/parser fails.

WARM resolutions use an arm-local file lock around retrieve/plan/store/index visibility. The RRC
EverOS project namespace includes the round ID and is paired with that round/arm's fresh SQLite
store. After a MISS, the bridge holds the lock until an explicit search returns the newly stored
`external_ref`; global cascade health alone is insufficient. Later siblings acquire the lock,
retrieve that exact packet through EverOS + SQLite, and launch with zero planner tokens. The lock is
released before the hook returns and therefore before OpenCode starts the actual worker. COLD uses
both a no-op store and a no-op case index, so it never persists or retrieves a packet.

### Workload and UI

`RRDdemo.sh prep` seeds ContextMesh digests and copies the same canonical four-handler audit prompt
used by `demo.sh`. Side A and side B open identically formatted OpenCode TUIs with `orchestrator`
selected. The banner explicitly identifies `ContextMesh + RRC`, four worker subagents, and COLD or
WARM packet mode.

The combined meter uses Tollgate as the sole authority for consumed OpenCode/worker/summarizer
tokens, adds disjoint direct-Codex planner usage from RRC evidence, and uses the OpenCode DB only for
session/worker counts. ContextMesh digest savings remain a separate counterfactual row and are never
added to or subtracted from consumed totals. It reports worker count, RRC planner calls/MISS/HIT,
ContextMesh digest savings, and current readiness. It must not reuse the old standalone two-task
`rrc.demo` proof display.

## Alternative considered

Loading both existing plugins while retaining the one-shot `reasonrender_coding` tool is simpler,
but it still bypasses OpenCode `task` calls and does not combine either product at the worker
boundary. Having every worker call the Lane-A code generator is also rejected: the demo workload is
an audit, has no executable implementation oracle, and would replace rather than guide the worker.

## Milestones

- [x] M1: Add a tested multi-agent packet bridge over `OrchestratorRuntime`, including a canonical
  audit shape, audit-specific policy, structured planner output, COLD isolation, round-scoped WARM
  exact reuse/visibility polling, locking, rendered worker input, complete event evidence, and
  fail-open behavior.
- [x] M2: Convert the RRC OpenCode plugin/configs to task-boundary augmentation and load ContextMesh
  in both arms. Prove four task calls still target `worker` sessions and receive RRC packets.
- [x] M3: Restore `RRDdemo.sh`/TUI/prep to the canonical audit prompt, digest seeding, matching
  formatting, and a combined meter.
- [x] M4: Update documentation and run an offline OpenCode integration with a deterministic local
  provider/bridge that asserts four `worker` child sessions were launched from one assistant turn,
  all four lifetimes overlap, packets were injected, shared-file `digest_hit` events belong to worker
  sessions, and an oversized worker report produces `task_compressed`. Then run full
  Python/config/shell verification. Do not trigger paid live model calls automatically.

## Definition of done

- RRD side A and side B both select the OpenCode `orchestrator`, expose `worker`, set
  `subagent_depth=1`, and load both ContextMesh and RRC plugins.
- Pasting the canonical four-handler prompt causes one real OpenCode `task` call per handler; the
  orchestrator merges worker reports normally.
- Each worker receives a rendered, validated RRC Plan + Spec packet for its own handler and retains
  its original audit instructions.
- COLD produces four planner MISSes with no storage or cross-task reuse. WARM serializes only packet
  resolution in a round-isolated index, produces one MISS and subsequent exact HITs, explicitly
  observes the first ref before unlocking, and does not serialize worker execution.
- All workers read their assigned handler plus the three shared files; ContextMesh digest and task
  compression hooks remain active in both arms.
- No concrete handler path is sent to the generic planner or stored in EverOS case-shape content.
- RRC errors are visible in evidence and fail open to the original OpenCode worker task.
- `RRDdemo.sh prep/a/b/meter` retains the three-terminal interaction and uses the audit prompt.
- The meter distinguishes OpenCode/worker tokens, RRC planner tokens/calls/reuse, and ContextMesh
  savings rather than showing the obsolete fixed-function proof.
- Unit/integration tests, Pyright, relevant Ruff, ShellCheck/syntax, and config parsing pass without
  a paid live RRC run.

## Risks and one-way doors

- Plugin hook ordering is runtime-sensitive. RRC changes only `tool.execute.before` task input;
  ContextMesh changes only
  `read`/`task` output, so behavior must remain order-independent and be tested.
- Parallel WARM calls can all MISS without locking. The lock scope must end before actual worker
  execution or it would fake serial multi-agent behavior.
- EverOS indexing may lag after the first MISS. The bridge must poll the round-isolated search until
  the exact stored ref is visible while holding the WARM resolution lock, then release before
  returning to OpenCode.
- Planner packets are stored before audit success because siblings need them while launched in the
  same parallel turn. Evidence must label them planning templates, not verified audit results.
- Config and event schemas are demo-local and reversible; no public RRC contract or database schema
  migration is introduced.

## Verification plan

- Focused Python tests for extraction/privacy, COLD call counts, WARM MISS/HIT, schema rejection,
  locking behavior, and packet rendering.
- Static plugin/config tests proving both plugins and orchestrator/worker definitions are wired,
  plus a deterministic local-provider OpenCode integration that inspects the single-turn four-task
  fan-out, four overlapping child sessions, injected packets, worker-session digest hits, and task
  compression.
- Shell tests for prep prompt/seed order, TUI environment/banner, and meter invocation.
- `ruff check` and `ruff format --check` on changed Python, Pyright, full pytest, `bash -n`,
  ShellCheck when installed, and JSON parse of all configs.

## Completed verification

- Deterministic local-provider OpenCode integration passed: one root assistant turn launched four
  overlapping real `worker` child sessions; every worker received the RRC marker; ContextMesh
  produced worker-session digest hits and compressed all four oversized task results.
- Four concurrent WARM bridge subprocesses produced exactly one planner invocation and branches
  `MISS, HIT, HIT, HIT` through the lock + EverOS-ref/SQLite path.
- Bun integration covered successful task-prompt augmentation and bridge-error fail-open behavior.
- Audit packets now use a finite safe action catalog that rejects worker-facing implementation
  instructions (including allowed-prefix compound bypasses), and duplicated Python/plugin fail-open
  evidence is correlated by task ID. Planner usage remains billable evidence even when a returned
  packet fails response or policy validation.
- The meter reports READY only after both arms have four workers, four expected RRC branches, exact
  Tollgate traffic, ContextMesh plugin-load plus digest-hit evidence from all four worker sessions,
  and no fail-open; otherwise the comparison is visibly INCOMPLETE/NOT READY.
- `opencode debug config` resolved `default_agent=orchestrator`, `subagent_depth=1`, both custom
  agents, and both plugins from the RRD config.
- Full gate: Pyright reported zero errors; all 115 pytest tests passed; targeted Ruff/format,
  `bash -n`, ShellCheck, JSON parsing, and canonical-prompt byte comparison passed. No paid live
  Codex/Ollama RRD run was triggered.
