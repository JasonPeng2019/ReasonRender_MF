# WP0 compatibility notes

## SubagentStop blocking - confirmed

Probe `harness/artifacts/WP0-subagent-stop/stream-run2.jsonl` used one bounded
Claude Sonnet subagent, `--max-turns 4`, and a command hook that blocked once.

- Claude 2.1.220 requires `--verbose` with `-p --output-format stream-json`.
- The hook payload contains `session_id`, `transcript_path`, `cwd`,
  `agent_transcript_path`, `last_assistant_message`, and `stop_hook_active`.
- The first stop had `stop_hook_active: false`; JSON
  `{"decision":"block","reason":"..."}` blocked it and the reason reached
  the subagent. Its continuation had `stop_hook_active: true`.
- The allow transport is `{"decision":"approve"}`. `allow` is rejected by
  Claude's hook schema. The completion gate follows this observed contract.

## Per-agent tool restriction - confirmed for the full arm

The initial bounded tool probe showed that omitting `Read` is not sufficient:
with only `Bash, Write, Edit` available, Bash successfully ran `sed` and a
PowerShell `Get-Content` fallback. F2.3 therefore has a `PreToolUse:Bash`
guard in all three byte-identical arm settings; the guard activates only when
the runner exports `REASONRENDER_ARM=full`.

`harness/artifacts/WP0-bash-read-guard-live/block-stream.jsonl` is the live
Claude 2.1.220 proof. A two-turn, `$0.50`-bounded full-arm session attempted
`Get-Content -TotalCount 1 harness/agents/full/worker.md`. Claude emitted the
`PreToolUse:Bash` event, the hook returned
`hookSpecificOutput.permissionDecision: deny`, and Claude recorded a
`permission-rule` non-execution result before reporting the denial. The read
did not run. The paired `allow-stream.jsonl` ran `echo hook-probe` through the
same hook and received an empty hook response plus the expected command output.

The guard blocks direct and nested `Get-Content`, `cat`, `sed`, `grep`, `rg`,
and common Python file-read forms. Malformed payloads and non-full arms fail
open. The observed PreToolUse payload has no agent type or agent id, so this
installed Claude build cannot scope this mechanical rule to a worker alone;
the runner scopes it to the entire full arm via `REASONRENDER_ARM=full`. That is
stricter than F2.3's worker minimum and must remain explicit in future runs.

`--agents` custom registration is unavailable in this build when supplied as
the documented inline JSON object: the init record and the model both exposed
only `claude`, `Explore`, `general-purpose`, `Plan`, and `statusline-setup`.
`harness/artifacts/WP0-custom-agents-inline/stream.jsonl` retains that result;
the runner must not pass inline `--agents` JSON.

The target-local custom-agent mechanism is supported. In
`harness/artifacts/WP0-file-agent-registration/stream.jsonl`, a
`target/.claude/agents/worker.md` definition appeared in the initialization
agent list and the `worker` child honored `maxTurns: 1` while its parent CLI
had `--max-turns 3`. The runner therefore materializes that registered
target-local `worker` definition for each arm; it does not use inline JSON.

The viable enforcement boundary is a global `--tools` allow-list. In
`harness/artifacts/WP0-disallowed-tools-child/stream.jsonl`, a parent launched
a built-in `general-purpose` child with `--tools Task,Bash` and
`--disallowed-tools Read,Glob,Grep`; the child reported that it had only Agent
and Bash and could not invoke native Read. Use this inherited allow-list for
the full arm (`Task,Bash,Write,Edit`, plus the ContextMesh MCP tool when
configured), together with the PreToolUse Bash guard. This makes the parent
more restricted too, which is compatible with its no-edit prompt.

## Subagent usage granularity - confirmed in stream shape

The retained stream has a `task_notification` with per-subagent `usage`:
`total_tokens`, tool uses, duration, and cache fields. The future collector must
attribute that record separately from the parent result.

## MCP process sharing - confirmed for Claude built-in subagents

Probe `harness/artifacts/WP0-subagent-stop/mcp-sharing-stream.jsonl` launched
two background `general-purpose` Claude subagents, each calling
`mcp__contextmesh__read` for the same source file. The configured server wrote
four events to `mcp-sharing-metrics.jsonl`; every event has PID `192672`.
This confirms that the parent and both Claude subagents share one ContextMesh
MCP server process, so the in-process digest gate is applicable to this path.

The probe used `CONTEXTMESH_EV_TIMEOUT_MS=500` to bound the call. It observed
two raw reads, an expected timeout, and a digest rejection, not a deduplicated
digest result; it proves process sharing only, not concurrent gate savings.
The inline `--agents` custom `worker` definition was not registered by this
Claude CLI build. The later target-local agent-file probe did register `worker`,
so the runner uses that separately verified file mechanism rather than the
built-in `general-purpose` type.

The two completion notifications carried independent usage records:
`29,863` and `29,835` total tokens, respectively. This reinforces the
collector's per-subagent attribution requirement.

## Four-background dispatch topology - basis for the F2 amendment

`harness/artifacts/WP0-four-background-topology/attempt-2/stream.jsonl` is a
tight non-RuleForge capability probe using a target-local registered `worker`.
All four `worker` children launched with `run_in_background: true`, ran their
bounded Bash instruction, and completed. The four parent launches were still
recorded as four separate top-level parent assistant records with one Agent call
each (1+1+1+1), rather than one record containing four calls. Under the
superseded F2 this failed the report's topology rule despite real background
execution. The accepted 2026-08-10 amendment allows this one-to-four-record
shape, retains it in the report, and counts its parent-turn cost.

Attempt 1 discovered a configuration precondition: a custom worker's declared
tools are intersected with the parent CLI allowlist. A parent `Task`-only
allowlist made `tools: Bash` resolve to zero tools; attempt 2 used `Task,Bash`,
matching the runner's inherited tool setup. The result is a provider-topology
finding, not an economics result and not a reason to rerun the three-arm
benchmark. The executable prompt now explicitly requires its single packet
rendering Bash call, four worker calls, `run_in_background=true`, and no
preceding text-only response. It can now validate a fresh amended-contract run;
it does not retroactively promote this bounded probe or the retained r6 run.

## Max-turn semantics - confirmed

`harness/artifacts/WP0-max-turn-semantics/stream.jsonl` shows that CLI
`--max-turns 1` is inherited by a built-in child: the parent completed its
single turn, the child made one Bash call, and the child then stopped at its
turn ceiling. It cannot supply independent parent and worker ceilings.

`harness/artifacts/WP0-file-agent-registration/stream.jsonl` establishes the
needed separation: a parent with `--max-turns 3` discovered target-local
`worker.md`, and that worker with `maxTurns: 1` executed exactly one tool turn.
M8 applies this proved mechanism to every arm: the parent CLI remains at 25
turns and preparation writes `target/.claude/agents/worker.md` with
`maxTurns: 15`, bypass permission mode, and the arm's maintained tool/body
contract. `harness/artifacts/M8-worker-ceiling` retains the Fast/Priority
coder, Terra review, Luna compile, and Luna final-test handoffs. This settles
ceiling construction only; it does not retroactively validate the retained r6
run, which was captured before the F2/F3 amendment.
