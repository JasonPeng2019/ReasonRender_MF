# PLAN — Dual EverOS and local-memory RRD demos

> Revised after `/adversarial` on 2026-08-08. This extends, rather than supersedes,
> `docs/rfc-codex-ollama-contextmesh-rrc-demo.md`.

## Goal

Provide two explicit ContextMesh + ReasonRenderCoding demo entry points: one backed by EverOS and
one backed only by local SQLite/files. The local version must not start, health-check, call, or need
credentials for EverOS. Both versions retain the same interactive Codex/Ollama four-worker
COLD/WARM workflow and produce backend-bound evidence.

## Non-goals

- Do not remove EverOS from `contextmesh/demo.sh`, core RRC Lane B, or the repository.
- Do not change the audit prompt, model, worker count, COLD/WARM contract, or live-provider claim.
- Do not add local vector, fuzzy, embedding, or LLM-backed memory.
- Do not claim that separately seeded EverOS and local runs are a controlled backend ablation. The
  two launchers demonstrate dependency choices; a scientific comparison needs a future shared
  digest fixture and repeated paired runs.
- Do not support simultaneous live execution. Both variants intentionally share the dedicated
  `:8789` Tollgate and `:8790` proxy, so startup is mutually exclusive and refuses a backend switch
  until the active variant is stopped.

## Constraints and grounded wiring

- Public launchers force their backend even when the parent shell exports a conflicting value:
  `RRDdemo.sh` and `RRDdemo-everos.sh` force `everos`; `RRDdemo-local.sh` forces `sqlite`. Shared
  command logic lives in a private helper.
- EverOS currently serves two demo roles: ContextMesh digest records in `rrd_codex_hook.py` and the
  RRC case-shape-to-external-ref index in `rrc/multiagent_demo.py`. The local variant replaces both.
- The RRD EverOS case index will use assistant-only add plus keyword search of unprocessed messages,
  scoped by deterministic round/case session. It will not call `/flush`, so it does not invoke the
  EverOS extraction LLM or create unmetered memory-model tokens. Core `EverOSClient` Lane B behavior
  is unchanged.
- Backend-specific round pointers and backend-prefixed round IDs prevent artifact mixing. An atomic
  `mkdir` startup lock serializes validation plus start/adoption of the shared Tollgate/proxy. A
  persistent active-backend record is written only after health/version/route checks pass; `down`
  refuses to stop a different backend's services.
- The local variant never delegates to the ordinary `:8788`/`:8000` stack. It uses only RRD
  Tollgate `:8789` and proxy `:8790` for Codex/Ollama.
- Round metadata, seed manifests, hook/RRC/proxy evidence, and token session prefixes carry the
  backend. Missing or mixed evidence makes the meter NOT READY.
- Local digests live in a mode-`0600` manifest and are checked against target identity, current raw
  file hash, digest hash, and an unkeyed self-seal. This is integrity/current-source binding within
  a trusted same-user filesystem, not authentication against a same-user attacker who can rewrite
  both data and seal.
- Local RRC reuse uses a dedicated case-index table in the same `plan-spec.sqlite` database. Keys
  include round id and case-shape SHA-256, and lookup also compares the full case shape.
- EverOS keeps the existing **stack-wide** lifecycle: its RRD launcher delegates to the ordinary
  stack and its `down` stops that ordinary stack even if it was already healthy. This potentially
  disruptive compatibility behavior is documented rather than mislabeled component ownership.
  The local launcher never touches the ordinary stack. Per-component ordinary-stack ownership is a
  separate future hardening task.
- Automated tests make no provider calls. A poison EverOS URL plus request/client spies and a real
  child-process listener proves the local seed and resolution paths make zero EverOS requests.

## Design-bank clarification

- Design queries for `contextmesh/RRDdemo.sh` and `rrc/multiagent_demo.py` found no matching canon.
- The user explicitly resolved the material architecture choice by requesting both variants.
- Assumption: keeping plain `RRDdemo.sh` as the EverOS-compatible alias is the least surprising UX.

## Approach

Move the current command dispatcher behind three tiny forcing launchers and validate the selected
backend before any side effect. Persist backend-specific active-round pointers plus one
active-backend record. Make stack, preflight, TUI, hook, RRC index, proxy, and meter conditional and
backend-bound.

`rrd_start_stack.sh` acquires one atomic startup directory before reading or changing active state
and holds it through every dedicated-service check/start/adoption. Same-backend callers wait and
then adopt only healthy services with matching route/version; cross-backend callers fail. The
record contains backend, route digest, proxy version, and recorded dedicated PIDs. It is atomically
published only after success. On partial failure, the starter stops only dedicated components it
started and removes temporary state. A stale record is recovered only when its recorded dedicated
PIDs are dead and both ports are unhealthy; any live/ambiguous identity fails with an actionable
`down` instruction. `rrd_stop_stack.sh` uses the same lock around ownership checks and teardown.

For ContextMesh, the EverOS path continues writing/reading assistant-only digest records. The local
path stores the exact digest in the sealed manifest and never enters an HTTP helper. For RRC, the
EverOS version stores exact round-scoped case records without flush; the local version performs the
same exact lookup using SQLite. This preserves full RRD token accounting because neither memory
backend invokes an unmetered model.

**Alternative rejected — duplicate the launcher and all helpers.** It would create two
security-sensitive hook/proxy implementations that drift.

**Alternative rejected — retain EverOS `/flush` and call the existing meter exact.** EverOS model
traffic is routed through ordinary Tollgate `:8788`, while the RRD meter reads `:8789`; omitting it
would undercount EverOS. Avoiding extraction in this exact demo index is simpler and honest.

**Alternative rejected — concurrent variants on new ports.** It adds port/config/PID duplication
without serving the request. Explicit mutual exclusion is smaller and safer.

**Alternative rejected — local JSON case map.** Four hook processes can race; SQLite provides
atomic persistence and reuses an existing dependency.

## Milestones

- [x] **M1 — forcing launchers and atomic lifecycle state.** Write failing shell-facing tests, then
  add the public wrappers, private dispatcher, strict selector, backend-specific round pointers,
  serialized startup/stop, and active-backend record with failure rollback/stale rules. Make
  stack/preflight/TUI conditional. **Acceptance:** conflicting ambient selectors cannot alter a
  public launcher; local never starts/checks/stops EverOS; simultaneous same-backend starts adopt
  one healthy stack; simultaneous cross-backend starts yield one owner and one rejection; a partial
  start leaves no false record; stale recovery requires dead PIDs plus unhealthy ports. EverOS
  `down`'s documented stack-wide ordinary teardown and local `down`'s no-ordinary-stack behavior
  both have explicit tests.
- [x] **M2 — dual ContextMesh digest storage.** Write failing hook tests, then bind seed manifests
  to backend and store either an EverOS key or local digest text. **Acceptance:** both paths deliver
  exactly three current digests; local succeeds against a poison URL with zero HTTP attempts;
  stale, malformed, and cross-backend manifests fail open.
- [x] **M3 — dual exact RRC case indexes.** Write failing persistence and HTTP-contract tests, add a
  SQLite index and an RRD-specific no-flush EverOS index, then select by backend in the CLI/hook.
  **Acceptance:** process-reopened local lookup is exact and round-scoped; EverOS index emits no
  `/flush`; a pinned real-EverOS route/service test round-trips the exact assistant-only add/search
  while a counting/poison LLM client observes zero calls; local constructs no EverOS client; a
  black-box hook-to-CLI child process completes local MISS then HIT against a poison listener with
  zero connections; serialized WARM resolution remains 1 MISS / 3 HIT.
- [x] **M4 — backend-bound proxy and meter.** Write failing mixed-evidence fixtures, then propagate
  backend through round metadata, hooks, packets, proxy receipts, session prefixes, and meter.
  **Acceptance:** positive fixtures READY for each backend; missing/cross-labelled evidence and
  foreign session prefixes are NOT READY.
- [x] **M5 — docs and final gate.** Document both command sets and their tradeoff; run the full
  deterministic gate and final diff adversarial. **Acceptance:** tests/types/lint/format/shell checks
  pass, reviewer returns SHIP, and live Ollama execution remains explicitly unverified.

## Definition of done

- [x] `RRDdemo-everos.sh prep|a|b|meter|down` retains an EverOS-backed workflow.
- [x] `RRDdemo-local.sh prep|a|b|meter|down` runs with no EverOS process, health check, request, or
  memory credential; plain `RRDdemo.sh` always aliases EverOS.
- [x] Shared RRD services are mutually exclusive, atomically adopted, rollback-safe, and guarded
  against stale/foreign teardown; prepared rounds remain separate. EverOS ordinary-stack teardown
  retains the documented legacy stack-wide behavior.
- [x] Backend identity is checked in round, manifest, hook, RRC, proxy, and token evidence.
- [x] Both paths deliver the current handler plus exactly three current shared digests. Local
  integrity assumes a trusted same-user filesystem and is not described as cryptographic origin
  authentication.
- [x] Four workers, COLD 4/0, WARM 1/3, bounded fail-open, result compression, root merge, and exact
  RRD token accounting remain intact; neither memory backend performs an unmetered model call.
- [x] Local reuse is deterministic, exact, persistent, process-safe, and round-scoped.
- [x] Tests cover invalid backend, active-backend conflict, missing/stale/mixed data, EverOS
  no-flush, and poison-URL local no-network behavior without paid calls.
- [x] Documentation states that this is a dependency/operation choice, not by itself a controlled
  token-efficiency ablation.

## Verification plan

- Red/green: focused cases in `tests/test_rrd_demo.py`, `tests/test_rrd_codex_hook.py`,
  `tests/test_rrc_multiagent_demo.py`, `tests/test_rrd_response_proxy.py`, and
  `tests/test_rrd_combined_meter.py`.
- Installed CLI regression: `tests/test_rrd_codex_cli_integration.py`.
- Full: `uv run pytest -q -p no:cacheprovider`, `uv run pyright`, `uv run ruff check rrc
  contextmesh/scripts tests contextmesh/tests`, scoped Ruff format check, `bash -n`, ShellCheck,
  `git diff --check`, plan checker, and secret scan.
- No-EverOS smoke: set a poison `RRC_EVEROS_URL`, patch request/client construction to fail on use,
  and run local seed plus resolution. Also run the real hook-to-`uv run python -m
  rrc.multiagent_demo` subprocess against a counting loopback listener; successful MISS/HIT and zero
  accepted connections are required. No listener on `:8000` is required.

## Risks and one-way doors

- **Public UX:** wrapper names are user-facing. Plain compatibility and thin wrappers keep this
  reversible.
- **SQLite schema:** the local case table is demo-local in `plan-spec.sqlite`; it has no external
  migration promise.
- **Retrieval semantics:** both demo indexes are deliberately exact; this does not prove EverOS
  episode/BM25 or embedding retrieval quality.
- **Same-user tampering:** manifests provide self-consistency and current-source binding only. A
  separate secret would be needed to authenticate against a same-user writer.
- **Shared services:** cross-backend concurrency is explicitly unsupported. Atomic acquisition and
  conservative stale recovery guard the active-backend record.
- **Ordinary stack shutdown:** the EverOS variant intentionally preserves current stack-wide
  teardown and can stop a pre-existing ordinary ContextMesh stack. The CLI/docs warn before use;
  replacing broad ordinary teardown with per-component ownership is out of this change's scope.

## Deviations/results log

- 2026-08-08: Initial plan received BLOCK for unmetered EverOS extraction, unsafe concurrent service
  ownership, overclaimed manifest authentication, ambiguous launcher selection, and incomplete
  evidence binding. The revision removes concurrency/ablation claims, eliminates `/flush` from the
  RRD EverOS index, defines the filesystem trust boundary, forces public selectors, and binds all
  evidence surfaces.
- 2026-08-08: M1-M4 implemented test-first. Focused tests cover forcing wrappers, local stack
  isolation, cross-backend refusal, dual digest storage, exact SQLite persistence, no-flush EverOS
  records, a poison-listener local CLI MISS/HIT, both meter backends, and mixed evidence rejection.
- 2026-08-08: A pinned real EverOS Docker image at revision `6f1585e57728` completed an
  assistant-only `/add` plus session-filtered keyword `/search` round trip against a counting poison
  LLM endpoint with zero LLM calls. Native Intel macOS `uv` cannot install LanceDB, so this
  integration was exercised through the repository's supported Linux-container path.
- 2026-08-08: Final deterministic gate passed: 214 pytest cases, Pyright with zero diagnostics,
  Ruff lint/format, Bash syntax, ShellCheck, plan validation, secret scan, and diff whitespace.
  Final diff adversarial returned SHIP with no blockers. Live paid Ollama/Codex interaction was not
  rerun and remains an explicit operator validation step.
