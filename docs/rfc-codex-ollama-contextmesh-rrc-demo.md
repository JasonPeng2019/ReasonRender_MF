# PLAN — Codex + Ollama ContextMesh/ReasonRenderCoding demo hardening

> **Historical / non-RRCv2 / superseded.** This experiment record is retained only for
> reproducibility; it is not current product guidance. ADR 0002 and `docs/RRCv2.md` govern the active
> implementation. Ollama, OpenCode, Tollgate, and audit-packet routes below are
> fixture-only/non-product.

> Written with `/plan` on 2026-08-07 and revised after the installed-Codex M0
> traces on 2026-08-08. This supersedes the OpenCode runtime design in
> `docs/rfc-contextmesh-rrc-multiagent-demo.md` only for `RRDdemo.sh`.

## Goal

Make `contextmesh/RRDdemo.sh` launch two real, interactive Codex CLI arms using
the configured `OLLAMA_API_KEY`, four native Codex worker subagents,
ContextMesh shared-context delivery and safe result compression, and bounded
ReasonRenderCoding Plan+Spec reuse. Make the comparison meter evidence-based
and explicit about what one paired run does and does not prove.

## Non-goals and claim boundary

- Do not change the ordinary OpenCode `contextmesh/demo.sh`.
- Do not modify or depend on the user's normal `~/.codex`, OpenAI login, or a
  literal API key in generated config. Each arm receives an isolated
  round-local `CODEX_HOME`; the only credential reference is the environment
  variable name `OLLAMA_API_KEY`.
- Do not run a paid Ollama request in automated verification. Live canary and
  demo runs are explicit user actions.
- Do not infer audit correctness, causal savings, or general speedup from one
  paired sample. Tollgate totals are measured; digest savings are labelled
  counterfactual; COLD/WARM difference is labelled non-causal.
- Keep the historical OpenCode plugin/config artifacts for the ordinary demo;
  the RRD launcher must have no executable OpenCode dependency.

## Constraints and installed-Codex findings

- Preserve `RRDdemo.sh prep/a/b/meter` and the three-terminal workflow.
- The checked-in prompt must use Codex-native language: exactly one `worker`
  per handler, launch four in parallel, wait for all four, then merge by file.
- Both arms use the same target, prompt, model, Codex configuration, and
  ContextMesh behavior. Their intended difference is RRC COLD versus WARM.
- Codex uses the Responses protocol through a dedicated Tollgate on `:8789`.
  A loopback response adapter on `:8790` is the root/worker provider base.
- Installed `codex-cli 0.147.0` is pinned for the offline compatibility test.
  `multi_agent_v2=false` is required. Its v1 `spawn_agent` fields are observed
  via deferred `tool_search`, and hook names are pinned in a sanitized fixture.
- A role declaration in root `config.toml` is required for `agent_type=worker`
  to be advertised. A custom `agents/worker.toml` suppresses lifecycle hooks in
  this release, so the role has metadata only and inherits the isolated root
  config. The observed child tool set has no multi-agent tools.
- M0 found that blocking the first `SubagentStop` requires another provider
  continuation and can lose the raw result if that continuation fails. It is
  not the production compression boundary. The response adapter buffers a
  completed worker SSE stream, persists raw evidence, summarizes with a bounded
  auxiliary Codex session, and replaces the stream only after success. On any
  persistence, subprocess, parse, or size failure after a complete upstream
  response it returns the original bytes. If the upstream itself misses its
  wall deadline, no complete response exists to preserve, so the proxy returns
  a bounded 502 before headers or closes an already-started passthrough stream.

## Approach

### 1. Bounded RRC bridge

Add deadlines to the nested Codex planner, WARM lock acquisition, EverOS
visibility, and outer bridge. Subprocesses run in a process group; timeout kills
and reaps the group. Failure is recorded without credentials and the hook fails
open so the original worker still starts. Planner Responses use a distinct
`-planner` Tollgate session.

### 2. Authenticated Codex hook adapter

- `PreToolUse(spawn_agent)` accepts exactly one canonical demo handler path,
  rejects path traversal/symlinked components/non-regular or oversized files,
  reads current bytes through a no-follow descriptor, resolves a bounded RRC
  packet, and appends both the packet and exact handler bytes/hash to the worker
  message. Invalid scope is policy-denied; operational failure is fail-open.
- `seed` reads exactly `src/models.js`, `src/utils.js`, and
  `src/middleware.js`, creates bounded summaries, stores versioned records in
  EverOS, and atomically seals a mode-`0600` manifest containing root identity,
  current raw hashes, digest hashes, and content-addressed keys.
- `SubagentStart(worker)` revalidates the root identity, current regular-file
  bytes, manifest fields, EverOS record fields, raw hash, and digest hash before
  injecting three explicitly untrusted digest blocks. Missing/stale/poisoned
  context fails open and prevents meter readiness rather than being called a hit.
- `PostToolUse` records real spawn IDs and wait results. `SubagentStop` records
  the result delivered by the response adapter and its compression receipt.
  Root `Stop` records one final-merge digest. All JSONL evidence is best-effort,
  credential-free, and created mode `0600`.

### 3. Safe response compression

Run a loopback Responses proxy between Codex and Tollgate. It only considers
successful SSE responses for an RRD outer session with `x-openai-subagent`.
Short/non-message streams pass through byte-for-byte. For oversized final worker
messages it:

1. saves the exact raw report in a receipt-addressed mode-`0600` file;
2. invokes the sealed hook bundle's summarizer with a deadline and process-group
   cleanup;
3. requires a nonempty result smaller than 65% of the original; and
4. synthesizes a completed stream with the summary and receipt while preserving
   the upstream terminal usage event.

The proxy has request/response caps and upstream deadlines. A raw receipt retry
is idempotent only when the existing bytes match. Every exception before
replacement returns the original upstream stream and emits `compress_fail_open`.

### 4. Isolated launcher, stack, and prompt

`reset` creates two clean fixture repositories, a sealed round hook bundle, and
role-specific isolated homes:

- `-outer`: root and all four native workers through response proxy `:8790`;
- `-summarizer`: result compression direct through Tollgate `:8789`;
- `-planner`: RRC planning direct through Tollgate `:8789`; and
- `setup-seed-*`: setup-only digest generation, excluded from steady totals.

Sharing the outer session between root and workers is deliberate: the installed
CLI inherits one provider config into native children. Tollgate remains the sole
additive token authority and labels that row `Codex outer (root+workers)`.
The isolated configs disable Codex plugins, so neither the offline integration
suite nor the production demo clones or executes unrelated marketplace code.

The RRD stack normalizes `OLLAMA_BASE_URL`: HTTPS only, no embedded credentials,
query, or fragment; remove exactly one trailing `/v1`; reject residual paths.
It refuses to reuse a healthy dedicated Tollgate whose stored route digest does
not match. `prep` does local preflight, reset, and the explicit model-backed seed;
`--canary` is optional and consumes tokens. Launch uses the isolated config and
Codex's explicit hook-trust bypass; it never runs `codex login`.

### 5. Evidence meter

READY requires per arm:

- exact Tollgate rows only from the three steady sessions, with setup separate;
- four unique assignment IDs, spawn tool IDs, real agent IDs, and the exact four
  handlers, with packet IDs/handlers correlated to assignments;
- current handler hashes and three canonical raw/digest-hash receipts per agent;
- the expected RRC branches (COLD 4/0, WARM 1/3) or four visible control packets;
- all four agent lifetimes sharing a nonempty overlap interval;
- four final/wait results, at least one correlated proxy compression receipt,
  one root merge, and zero fail-open/policy/foreign/inexact evidence.

The UI says context was delivered, not that the model reasoned about it. Setup is
shown outside steady totals. One delta is a `PAIRED SAMPLE`, not a causal estimate.

### 6. Legacy ContextMesh defect

Use one cache-key helper for store and lookup in
`contextmesh/plugin/contextmesh.ts`, and use source-level `\0` rather than literal
NUL bytes so reread blocking works and Git treats the file as text.

## Milestones

- [x] **M0 — installed-Codex feasibility.** The pinned local Responses fixture
  proves deferred v1 schema discovery, schema-valid four-worker spawn/wait,
  model-visible PreToolUse and SubagentStart sentinels, four-worker overlap, one
  merge, and mode-`0600` hook traces. Timeout, nonzero, signal, empty, and
  malformed output are exercised at PreToolUse, SubagentStart, and SubagentStop;
  all four workers and the merge survive. The unsafe stop-continuation design was
  rejected in favor of the fail-open response proxy.
- [x] **M1 — liveness and cache key.** Planner/lock/visibility work is bounded;
  process groups are reaped; the shared-read key is consistent and contains no
  literal NUL byte.
- [x] **M2 — hook and proxy units.** Tests cover current handler delivery,
  ambiguous scope, symlink rejection, authenticated three-file seed/start,
  receipt recording, successful compression, summarizer failure fallback, raw
  permissions, and idempotent receipt retries.
- [x] **M3 — Codex launcher/preflight.** `prep/a/b` generates isolated Responses
  configs and launches Codex, never OpenCode/login. Dedicated Tollgate and proxy
  have health checks and bounded startup.
- [x] **M4 — correlated meter.** Positive and negative fixtures cover traffic,
  IDs, handlers, overlap, compression, merge, and non-causal labels.
- [ ] **M5 — final verification and adversarial diff.** Run focused/full tests,
  Ruff/format, Pyright, shell checks, config parsing, diff/secret checks, and an
  independent `/adversarial` review. Fix all blockers before handoff.
- [ ] **M6 — user-triggered live canary/demo.** Live Ollama Responses and model
  tool-call quality remain deliberately unspent. The user runs `prep`, `a`, `b`,
  and `meter`; this is the only evidence that the selected Ollama cloud model
  itself discovers the deferred tool and follows the prompt.

## Verification plan

- Run the installed-CLI M0 suite in `tests/test_rrd_codex_cli_integration.py`,
  including the complete 3-boundary × 5-failure matrix and the real response-
  proxy delivery case, against exactly `codex-cli 0.147.0`.
- Run the full offline suite with `uv run pytest -q -p no:cacheprovider`, then
  rerun focused hook/proxy/meter/launcher tests after any review fix.
- Run `uv run pyright`, `uv run ruff check rrc contextmesh/scripts tests
  contextmesh/tests`, and Ruff format checks on every changed Python file.
- Run `bash -n` and ShellCheck on the five RRD shell entry points, parse/compile
  generated Python/config assets, run `git diff --check`, and verify the actual
  configured Ollama key value is absent from all changed/untracked files.
- Start the dedicated local stack and run non-live preflight; do not invoke the
  token-consuming canary during automated verification.
- Run an independent `/adversarial` worktree review and fix every blocker before
  handoff. A live COLD/WARM round remains a separately labelled user action.

## Definition of done

- [x] `RRDdemo.sh a` and `b` execute interactive Codex with the environment-key
  custom Ollama provider and four-worker concurrency.
- [x] The installed CLI accepts the exact prompt, advertised v1 tool schema,
  role declaration, hook rewrites, start context, parallel workers, and merge.
- [x] Planner/lock/HTTP/hook/summarizer paths are bounded and fail open; special
  source files are rejected without blocking.
- [x] Current handler and exactly three authenticated shared digests are delivered
  with correlated hashes and cannot be reported READY when evidence is missing.
- [x] Oversized result replacement occurs only after raw persistence and summary
  success; failure returns the untouched upstream stream.
- [x] Meter partitions actual/setup/counterfactual quantities and rejects the
  specified false-positive states.
- [ ] Full deterministic verification and final adversarial review pass.
- [ ] A user-triggered paid live round confirms the configured Ollama model's
  streamed Responses and native Agent behavior; until then this is an explicit
  live-provider unknown, not an automated-test claim.

## Risks and reversibility

- **CLI wire drift:** preflight pins installed features; the integration fixture
  is skipped rather than generalized on another Codex version. Re-run M0 before
  claiming support for a new version.
- **Live model capability:** the local provider proves Codex orchestration, not
  that every Ollama model will emit valid deferred Agent calls. The canary/live
  run is required for that claim.
- **Proxy buffering:** bounded buffering adds latency and memory use. Caps and
  byte-for-byte failure fallback prevent silent partial replacement.
- **Hook trust bypass:** confined to a newly generated `CODEX_HOME` and clean
  fixture repo with a hashed round hook bundle. No global trust state changes.
- **EverOS trust:** content hashes and the sealed local manifest authorize data;
  an attacker able to rewrite both same-user manifest and target is out of scope.
- All schemas/evidence are demo-local and versioned; old OpenCode paths remain
  available for the ordinary demo. No irreversible external migration occurs.

## Deviations/results log

- 2026-08-07: checkpoint `b1d8e0b` preserved the last OpenCode-based state.
- 2026-08-08: M0 BLOCK review found the original trace lacked hook failure cases,
  model-visible injection proof, exact deferred schema, and a continuation-failure
  fallback. The installed integration now covers the first three. The production
  compression boundary moved from `SubagentStop` continuation to a fail-open
  loopback response proxy, resolving the raw-result-loss blocker.
- 2026-08-08: custom worker config files were removed. A metadata-only
  `[agents.worker]` declaration exposes `agent_type` while preserving lifecycle
  hooks; child requests expose no descendant multi-agent tools in the pinned run.
- 2026-08-08: live Ollama use remains deferred so no API key value or paid request
  is part of repository verification.
