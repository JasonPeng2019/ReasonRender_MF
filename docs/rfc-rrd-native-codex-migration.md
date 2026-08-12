# PLAN — Native Codex migration for ContextMesh + ReasonRenderCoding

> **Historical / non-RRCv2 / superseded.** This migration record is retained only for
> reproducibility; it is not current product guidance. ADR 0002 and `docs/RRCv2.md` govern the active
> implementation. Legacy provider and handler-audit routes below are fixture-only/non-product.

> Written before implementation on 2026-08-08. This supersedes the Ollama runtime portions of
> `docs/rfc-rrd-dual-memory-backends.md` without rewriting historical run evidence.
>
> **Status:** historical, non-normative planning record. The implemented comparison contract and
> deviations are defined by `docs/rfc-rrd-unbounded-native-matrix.md` and
> `docs/rrd-native-matrix-results-2026-08-09.md`. The current hierarchical single-delivery/four-way
> experiment contract is `docs/rfc-rrd-hierarchical-four-way-ablation.md`. Unchecked prescriptive text below records the
> original proposal and must not be read as a claim about current bytes.

## Goal

Make every public ContextMesh demo launcher, including the combined ContextMesh +
ReasonRenderCoding path, use the installed Codex CLI with its
native OpenAI authentication and models, with no Ollama key, Ollama endpoint, Tollgate model proxy,
or model-response proxy in the active workflow; preserve both local SQLite and EverOS memory
variants, provider-visible token evidence, bounded ContextMesh result compression, and real four-worker Codex
multi-agent execution.

## Non-goals

- Do not rewrite old RFC result logs or ignored run artifacts that accurately describe earlier
  Ollama/OpenCode experiments.
- Do not modify the vendored `opencode/`, `TokenTracker/`, or `EverOS/` projects merely because they
  contain general provider support or historical documentation.
- Do not change the generic `python -m rrc.demo` or `python -m rrc.run` library CLIs. Their model
  adapter already invokes native `codex exec`; their general EverOS Lane-B semantics are not public
  ContextMesh demo launchers. This migration changes only `rrc.multiagent_demo`, which the combined
  ContextMesh product executes and which already uses assistant-only add/keyword search.
- Do not make EverOS call an OpenAI LLM or embedding endpoint. This demo continues to use only
  assistant-only add and keyword search, so EverOS remains a storage backend, not another model
  client.
- Do not call observed token deltas exact billed savings. Native-Codex live runs may report
  provider-visible usage only after satisfying the frozen quality gate, with the pinned CLI's hidden
  retry limitation and resulting unknown billing delta stated beside every comparison.
- Do not delete the user's ignored `.env.local` or cached Codex credentials. The migrated workflow
  simply stops reading Ollama variables from it.

## Constraints

- The user explicitly chose native Codex and no Ollama. The installed CLI is `codex-cli 0.147.0`,
  and `codex login status` currently reports ChatGPT authentication.
- Official Codex documentation says local Codex supports ChatGPT or API-key login, cached file
  credentials live under `CODEX_HOME/auth.json`, multi-agent and hooks are stable features, and
  `codex exec --json` emits `turn.completed.usage`.
- Official hooks currently permit `PreToolUse` input rewriting and `PostToolUse` replacement of a
  completed local-tool result through blocking feedback; they do not permit rewriting a
  `SubagentStop` message. Result compression must therefore happen on the native `wait_agent`
  result, not at the provider HTTP layer.
- Generated demo homes must remain isolated from user plugins/rules while reusing authentication
  without placing credential bytes in any model-readable filesystem. Native OS keyring auth is a
  hard preflight requirement for every model-bearing run; file/auto auth is rejected with an exact
  quoted `${RRD_CODEX_BIN} -c cli_auth_credentials_store=keyring login` remediation, using the
  already validated absolute binary plus the exact stable `CODEX_HOME` and real host `HOME`, and no
  model dispatch. The printed, shell-escaped remediation is exactly `HOME='<validated-real-home>'
  CODEX_HOME='<stable-canonical-home>' '<validated-absolute-RRD_CODEX_BIN>' -c
  'cli_auth_credentials_store="keyring"' login`; preflight revalidates every substituted path before
  displaying it. One deterministic canonical mode-0700 home outside `contextmesh/runs` owns all
  rounds; each run recreates only its credential-free contents at the same path so the pinned CLI's
  path-derived keyring account remains stable. All external Codex processes use it serially.
  Generated configs set `cli_auth_credentials_store = "keyring"` and
  `[features].plugins = false` (never a top-level boolean `plugins`), and never serialize credentials.
- The default model is configurable through `RRD_CODEX_MODEL`; otherwise the launcher uses the
  native Codex listed non-code-mode v1 model selected for this product (`gpt-5.5`). RRC planner and ContextMesh
  summarizer must use the same native model family unless explicitly overridden.
- The current worktree already contains uncommitted dual-backend work. This migration builds on it
  and must not reset unrelated changes.
- Mode is `unleashed`; manual red-green-refactor is allowed. No new dependency is required.

### Active entry-point inventory and disposition

The allowlist below is exhaustive for active ContextMesh demo code. Git history and `docs/` remain
the archive; there will be no executable `legacy/` copy.

| current surface | disposition |
|---|---|
| `RRDdemo.sh`, `RRDdemo-local.sh`, `RRDdemo-everos.sh` | migrate to native Codex; keep all three public names |
| `scripts/rrd_demo.sh`, `rrd_demo_tui.sh`, `rrd_demo_preflight.sh` | migrate to native auth/config and current Codex tools |
| `rrd_codex_hook.py`, `rrc/multiagent_demo.py` | retain and migrate native wait compression/usage evidence |
| `rrd_combined_meter.py` | retain, replace Tollgate/proxy authority with native Codex usage schema |
| `rrd_start_stack.sh`, `rrd_stop_stack.sh` | reduce to local no-service lease and owned EverOS-only lifecycle |
| `scripts/rrd_origin.py`, `scripts/rrd_response_proxy.py` | retire/delete with their dedicated tests; native Codex has no provider proxy |
| `demo.sh` | compatibility redirect to `RRDdemo-local.sh` with a one-line migration notice |
| `env.example` | replace provider secrets/endpoints with required absolute `RRD_CODEX_BIN` plus optional model/reasoning settings; launchers never source it automatically |
| `RRD-demo-prompt.txt`, `demo-prompt.txt` | converge on and hash one native-Codex audit prompt with the frozen output grammar |
| `bench/run_bench.py` | rewrite as the native baseline/local/EverOS matrix entry point using the same product harness |
| `bench/aggregate.py`, `bench/analyze.py` | rewrite for the versioned native evidence schema or delete if the matrix runner subsumes them |
| `configs/*.json`, `plugin/*.ts`, `scripts/demo_tui.sh`, `demo_preflight.sh`, `live_meter.py`, `start_stack.sh`, `stop_stack.sh`, `flush_run_report.py`, `subagent_tokens.py` | retire/delete; these are OpenCode/Ollama-specific and Git history is the archive |
| `scripts/compare_quality.py`, `smoke_everos.py`, `bench/target-template/**` | retain only after removing assumptions about retired evidence |
| `snowflake/load.py`, `snowflake/ddl.sql` | rewrite for version-2 native Codex evidence, or retire both and remove their README command together |
| `Dockerfile.everos` | retain as the owned EverOS image input; pin/hash it in Docker lifecycle state and active-surface tests |
| `tests/test_rrd_opencode_integration.py`, `tests/test_rrc_plugin.py`, `contextmesh/tests/test_demo_connectivity.py` and legacy-only test cases | replace with native Codex integration/runtime-scan coverage |
| `contextmesh/README.md` commands/diagrams | rewrite as native Codex-only documentation |
| `rrc/demo.py`, `rrc/run.py`, `rrc/everos.py`, vendored `opencode/`, generic `TokenTracker/`, `EverOS/`, historical `docs/**`, ignored `contextmesh/runs/**` | outside the public ContextMesh demo surface; do not rewrite |

One repository test owns a checked-in manifest generated from tracked active surfaces and maps every
documented command to one disposition. It scans every executable/script/config,
prompt, README command, Snowflake asset, and test beneath `contextmesh/` plus the RRC modules explicitly listed above; any Ollama/OpenCode/Tollgate/
custom-provider token outside that single test's literal patterns is unclassified and fails. The
scan does not open or inspect `.env.local`, `docs/`, ignored runs/homes, submodules, or Git history;
separate execution tests prove no retained launcher sources `.env.local`.

## Grounded wiring and design-bank clarification

- `.agent-workspace/bin/query refs _compress --lang python --path contextmesh/scripts` shows that
  completed worker result compression is confined to `rrd_response_proxy.py`.
- `.agent-workspace/bin/query refs CodexModel --lang python --path .` shows that all RRC model calls
  already use `CodexModel`/`codex exec`; the provider substitution is introduced by generated
  `CODEX_HOME/config.toml`, not by RRC itself.
- The current public RRD launch path is wrappers → `scripts/rrd_demo.sh` → stack/preflight/TUI; the
  TUI writes an `ollama_rrd` provider and requires ports 8789/8790. The meter treats Tollgate rows
  and proxy events as authoritative. These are the migration seams.
- Design-bank queries for provider/authentication, native-hook result compression, and token
  authority returned no entries. The user resolved the provider choice. Low-risk recorded
  assumptions: native PostToolUse feedback is the supported replacement seam, transcript token
  records are acceptable only under the pinned CLI contract and strict fail-closed validation, and
  legacy `contextmesh/demo.sh` should redirect to the local native-Codex product rather than retain
  an active OpenCode/Ollama path.

## Approach

Generate native Codex homes with no `model_provider`, `base_url`, or credential environment key.
Validate forced-keyring login through the sealed absolute `RRD_CODEX_BIN`; reject file/auto auth and
never open, copy, or link `auth.json`. Retire the dedicated Tollgate and HTTP response proxy from RRD
lifecycle state. Local mode needs no service; EverOS mode starts/stops only EverOS with deliberately
disabled LLM/embedding configuration because the exact demo API paths need neither.

Prep derives and seals the exact expected handler assignment worklist from the frozen prompt,
target manifest, and `expected_worker_count`. During the root session, PreToolUse validates each
`spawn_agent` assignment against that worklist before any resolver call. The first valid assignment
synchronously invokes one bounded native-Codex RRC planner inside the hook; its result is sealed as
the miss packet. Later assignments reuse that packet as handler-bound hits. Unknown, stale,
ambiguous, reordered, or duplicate assignments are rejected before resolution. The outer
300-second hook timeout contains nested clamped planner timeouts, and the same credential-deny
sandbox is inherited by the nested planner. This lazy one-miss design is the implemented deviation
from the earlier pre-resolved-packet proposal.

That paragraph applies only to combined local/EverOS cells. A baseline cell has zero RRC planner
attempts/packets and zero ContextMesh digests or compression decisions; it runs the same native root
and `N` workers under a non-rewriting, usage-only observer whose sealed hash replaces the combined
hook hash in the disclosed intervention diff. Its exact product equation is `root + sum(N workers)`,
with the same assignment/start/stop/final-usage invariants and no planner/seed rows. Tests poison all
RRC/ContextMesh inputs and prove a baseline cannot read them, while still producing version-2 native
attempt/usage and collaboration evidence.

Move result compression into the Codex lifecycle hook. The pinned 0.147 hook name is
`multi_agent_v1wait_agent`; `wait_agent` is accepted only as the documented forward name. For each
PostToolUse wait response, atomically lock a mode-0600 per-arm wait state. Validate `tool_input`
targets and the `{status,timed_out}` result, and classify every status as terminal completed or
still pending. Only the pinned `Completed(Some(nonempty UTF-8 report))` shape (serialized as a
status object with a nonempty string `completed` field) is a successful terminal. An absent target
remains pending. Errored, shutdown, not-found, completed-without-report, malformed, or unknown
terminal shapes persist as terminal failure, print no compression decision so the exact result
passes through, and permanently invalidate READY. For every newly completed agent, preserve its
exact UTF-8 report in a sealed receipt
file and summarize it with one deterministic local extractive algorithm (normalized finding lines,
bounded per-agent quotas, and a receipt footer), never another Codex/model process. Identical
duplicate completion is reused without recomputation, while a
conflicting duplicate fails open and invalidates READY. Partial and timed-out waits may compress the
new terminal subset while carrying the exact pending IDs and timeout flag in the replacement; a
wait with no new or prior terminal result passes through unchanged. The cumulative state becomes
complete only after the cell's sealed `expected_worker_count=N` spawned agent IDs have exactly one
terminal report each. Public `RRDdemo*.sh` rounds fix `N=4`; the matrix harness explicitly uses
`N=1,2,4` according to scenario.

The exact successful hook output is `{"continue":false,"stopReason":DELIVERED}`. This is the
documented code-mode-compatible PostToolUse replacement: it replaces the model-visible result without
rejecting the nested tool promise and deliberately omits `additionalContext` so delivery is not duplicated.
`DELIVERED` contains completed agent IDs, their summaries and receipts, pending IDs, and the timeout
flag, so a later wait remains possible without the original structured result. A successful delivery
is strictly `len(DELIVERED.encode("utf-8")) < floor(0.65 * len(CANONICAL_RESPONSE))`, including
the entire receipt/status envelope, and also at most 2,000 UTF-8 bytes with each agent summary at
most 300 bytes. Since tokenizer tokens cannot exceed the number of UTF-8 bytes, this is below the
documented approximate 2,500-token feedback spill boundary. The installed 0.147 integration canary
must additionally prove it stays below the actual
model-visible hook-feedback spill boundary by inspecting the next model input and rejecting any
Codex-created preview/path indirection. If Unicode decoding, state/receipt persistence, summarization,
size, schema, or correlation fails, the hook prints no decision so Codex receives the exact original
tool result and records `compress_fail_open`; READY then fails. Installed Codex integration tests
exercise single, partial, timed-out, repeated-identical, conflicting-duplicate, and final-`N` waits,
plus every terminal-failure shape alone and mixed with successful partial results.
`CANONICAL_RESPONSE` is the exact compact UTF-8 JSON encoding of the parsed `tool_response` value
using `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`; if the hook
receives a string, it first parses that string as JSON. Those same bytes are sealed as the raw wait
receipt and used as the denominator, so escaping and Unicode cannot change the boundary later.
`DELIVERED` never embeds those raw bytes: each completed item carries only
`{receipt_id, relative_path, sha256, byte_count, summary}` beneath the sealed round root.

The fresh round home does not rely on persisted user hook trust. Before keyring access, preflight seals
the absolute interpreter, hook, and generated-config hashes; the root invocation then uses the
pinned CLI's `--dangerously-bypass-hook-trust` automation flag and allows only that exact sealed
command hook. An installed-CLI negative test proves omission skips an untrusted fresh-home hook, and
a positive test proves the flag runs the sealed hook while a post-seal hook/config mutation aborts
before authentication or model dispatch.

Replace Tollgate token rows with version-2 provider-reported Codex evidence. A native usage row binds
`round_id`, arm, backend, component (`root`, `worker`, `planner`, `seed_summarizer`, or
`evaluation_adjudicator`), exact model/reasoning/service tier/config hash, thread ID, transcript ID,
parent session/agent/task identity where applicable, lifecycle ordinal, and integer
`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, and
`reasoning_output_tokens`. Pinned `codex exec` does not report `total_tokens`; the evidence marks it
`total_provenance="derived_input_plus_output"` and derives it exactly as input + output. Require all
reported fields to be nonnegative integers, `0<=cached<=input`, `0<=cache_write<=input`,
`0<=reasoning<=output`, monotonic
cumulative transcript rows, a final row after the last model item and before the lifecycle hook, and
unique thread/transcript/component identities. Unsupported/malformed transcript schema is inexact,
not coerced.

Started calls are reconciled from native root collaboration events, worker/planner transcript
identities, and RRC model-event rows. A visible failed/error/aborted event, missing final usage,
regressed cumulative usage, duplicate identity, or unexplained transcript invalidates the cell.
A crash before native Codex emits usage is recorded as an unquantified lower-bound limitation rather
than silently coerced to zero; provider-visible totals are never described as billing-exact.

Root and each of exactly `N` parent-bound worker transcripts contribute their final cumulative row
once. Every RRC planner and seed summarizer `codex exec --json` process contributes its single
`turn.completed.usage` row; a process error with recoverable usage is reported as consumed failure
but makes READY false, and an error without usage is reported as an unknown lower bound and also
makes protocol READY false. A successful provider-reported row is schema-exact for the completed
visible response when its stream contains no error or `turn.failed` event. It is never labeled exact
billed consumption: pinned Codex may perform hidden HTTP/stream retries without emitting a retry
event, and its built-in provider cannot be overridden to prove zero retries without reintroducing a
custom provider/proxy. Every report therefore sets `billing_exact=false` and
`hidden_retry_observable=false`; any visible retry/error additionally makes the row invalid.
Seed accounting is scoped by cell type and seed reuse across cells is forbidden. A controlled
deterministic-seed cell makes zero seed-model calls and has `setup_cell=0`. Each fidelity cell owns
exactly three seed summaries for its one arm/cell identity, records them once, and has
`setup_cell = sum(3 seed_summarizer rows)`. A paired two-arm fidelity round therefore has six seed
calls and `round_setup = setup_a + setup_b`. In every cell:

`steady_x = root_x + sum(N workers_x) + sum(successful planner_x)`

`one_run_total_x = steady_x + setup_cell_x`

`observed_consumed_lower_bound = sum(one conservative contribution for each distinct started call
identity)`

For a root/worker transcript identity, validate ordinal order and monotonic counters and use exactly
its one final cumulative row. Identical repeated rows are deduplicated. Conflicting rows at the same
ordinal, a nonmonotonic sequence, or the absence of a provably final row makes the identity unknown;
the lower bound contributes the smallest arithmetically valid cumulative total observed for that
identity, never the sum of cumulative snapshots. For each one-turn planner/seed/evaluation-adjudicator
process identity, identical `turn.completed` rows are likewise deduplicated; conflicting valid rows
make the identity unknown and contribute the smallest valid derived total. An identity with no valid
row contributes zero only as an explicitly unknown lower bound.

`unknown_started_calls = |{distinct started call identities with absent, conflicting, visible retry/error,
nonmonotonic, nonfinal, or otherwise inexact usage evidence}|`

`hidden_retry_risk_calls = |{all provider call identities}|`

The READY equation contains only successful calls. Abort/failure reports always use the all-started
lower-bound equation, print `unknown_started_calls` and `hidden_retry_risk_calls`, and never label
observed provider-visible usage as exact billed consumption, even when the former is zero. The primary
one-run product total is the scoped `one_run_total_x` above; controlled
cells therefore add zero, while fidelity cells add their one owned three-call setup. No setup is
shared or double-counted across cells and no arbitrary setup amortization is reported. If a future report
declares a reuse horizon `K>0`, it may additionally label `steady_x + setup_cell_x/K` as a hypothetical
K-run amortization, while keeping the one-run total primary. The meter never subtracts cached input
from consumed tokens and refuses READY for missing, duplicated, malformed, nonfinal, arithmetically
inconsistent, mixed, failed, or uncorrelated evidence. Duplicate/conflict fixtures and asymmetric
per-arm seed totals verify these equations. Live canaries must prove native root and child
transcripts are disjoint and that token rows precede Stop/SubagentStop; otherwise transcript-based
TUI metering is unsupported and live product claims abort.

EverOS gets a dedicated ownership record and lock; the native RRD scripts never call the ordinary
stack. If `:8000` is healthy without a matching record, startup refuses rather than adopting by
health alone. A started host process is bound to PID, command, revision and root; a Docker process is
bound to container ID, unique `contextmesh-rrd-everos` name, revision label, mount and root. Same-mode
callers reuse only an exact live record. Dead owned state is recovered, partial/signal failure rolls
back only the process/container started by that attempt, and teardown never kills an unrecorded or
foreign listener. The child environment explicitly removes all inherited EverOS provider variables
and sets empty LLM/embedding/rerank/multimodal credentials plus loopback-deny endpoints. Real
assistant-only add/keyword-search tests use a poison listener to prove zero provider requests.
Host EverOS starts in a new session/process group. Its atomic record includes leader PID, PGID,
OS-reported process start time/birth token, executable, cwd, root, revision, and launch nonce; every
field is revalidated before reuse or signal. TERM/wait/bounded KILL targets only the still-matching
group and then proves the owned port and descendants are gone. PID-reuse, same-command foreign
process, wrapper-child survival, partial startup, and INT/TERM/HUP tests are required.

Authentication material never lives in a round directory or generated home. Preflight invokes the
absolute pinned CLI with `cli_auth_credentials_store="keyring"` in the credential-free, stable
mode-0700 canonical home and real host `HOME`, and requires `login status` to succeed; it never opens
the user's `auth.json`. File or auto mode is unsupported for this product. Seed and planner
Codex calls run sequentially before the root starts; the root and its native child agents are one
workflow stream; wait compression is local and starts no Codex process; after root exit, the two
evaluation calls run sequentially. Model-bearing root/worker contexts receive exact bounded source bytes directly. The matrix adds a
per-cell outer profile: every target is write-denied, and baseline cells additionally deny reads of
active ContextMesh/RRC runtime files and all other run artifacts. Network isolation is not claimed;
the complete stock tool schema remains visible. Cleanup first emits only the minimal strict usage/lifecycle rows
from transcripts into sealed JSONL, validates those rows and every artifact for credential field/
token-prefix patterns, fsyncs them, then removes all per-round contents while preserving the empty
canonical home path and credential-free base config required by the keyring namespace. Installed-CLI offline sentinel tests place fake auth files
at direct and discoverable paths and require model-invoked tools/provider requests/transcripts/
artifacts never contain the sentinel because the OS/profile denies access; they also mock-save
one stable-path keyring entry, delete/recreate the directory, prove login still works, and prove a
different canonical path fails. Live preflight proves keyring status and zero `auth.json` beneath the
generated home.

Every retained public shell launcher has an absolute `#!/bin/bash` shebang and every documented or
internal invocation executes that absolute interpreter, never `/usr/bin/env`. Before any keyring
lookup, the launcher resolves every executable/interpreter/utility used by
the product (`/bin/bash`, `/bin/sh`, `env`, `codex`, Python, Git, Curl, UV, Docker when selected,
copy/archive/hash/stat/readlink tools, and hook entry points) from a root-owned trusted system search
path or explicit per-tool paths, follows it to a canonical absolute path, and seals its regular-file
identity, owner/mode, version, and SHA-256. True OS utilities must be root-owned and non-group/
world-writable. Explicitly configured developer tools (`RRD_CODEX_BIN`, Python >=3.11, UV, and
Docker when selected) may instead be current-UID-owned only when their canonical paths, required
versions, and hashes are sealed before auth creation and they are non-group/world-writable; no
ambient-PATH discovery can grant this exception. Project hook files use the same current-UID,
regular, non-group/world-writable rule. Every credential-bearing
invocation uses that validated absolute path; generated hook commands likewise contain absolute
validated interpreter and hook paths. Child `PATH` is then replaced with a fixed list of system
directories and is never used to choose a credential-bearing program. Revalidation after each call
detects binary replacement. A hostile leading `PATH` fixture containing fake `codex`, `python`,
`git`, `bash`, `sh`, `env`, every manifested OS utility, and every explicitly pinned developer tool
must prove none executes and none can copy or
print or access credentials. Manually forcing a foreign interpreter (for example `bash
RRDdemo.sh`) is outside the executable-integrity guarantee and is not shown in product instructions;
credential safety remains scoped to the retained executable entry points and their children.

Every process uses a purpose-built environment rather than `{**os.environ}`. Native parent CLI
processes receive only a sealed `PATH`, the validated real host `HOME` required for macOS Keychain,
private `TMPDIR`, stable `CODEX_HOME`, required locale/terminal variables, and enumerated nonsecret
`RRD_*`/`RRC_*` paths, IDs, timeouts, backend, and model. EverOS and non-Codex helpers receive a
synthetic empty `HOME`. All
names matching key/token/secret/password/credential, provider variables (including `OPENAI_*`,
`CODEX_ACCESS_TOKEN`, `OLLAMA_*`, cloud credentials, cookies, and SSH-agent variables), and user
Python/Node injection variables are absent. Generated config sets top-level `web_search =
"disabled"`; `[features]` explicitly sets `apps`, `plugins`, `recommended_plugins`, `remote_plugin`,
`plugin_sharing`, `browser_use`, `browser_use_external`, `browser_use_full_cdp_access`,
`in_app_browser`, `computer_use`, `image_generation`, `view_image`, `in_app_updates`,
`skill_mcp_dependency_install`, and `tool_call_mcp_elicitation` all to `false`; and
contains
`[shell_environment_policy]` with `inherit = "none"` and
`ignore_default_excludes = false`, plus only explicit nonsecret `set` values; the sealed permissions
profiles enforce known credential-file denials, target write denial, and baseline intervention-read
denial, while the exact stock built-in and
five-function v1 collaboration schemas are recorded rather than misrepresented as absent.
Preflight aborts unless effective Apps/plugins/web/MCP are disabled and validates the remaining
stock tool list plus empty project-skill surface. Poison-key
tests make fake root, hook, planner, seed, evaluator, EverOS, and attempted model-tool processes dump received variable names and
require no forbidden name or value in any child or artifact.
Every model-bearing Codex invocation—planner, seed, root, and adjudicator—uses `--strict-config`.
Pinned login/features subcommands do not accept that flag, so before either is invoked a local
allowlist validator parses the generated TOML and rejects every unknown/wrong-table/wrong-type key;
an unknown-key fixture must fail before keyring or provider access.

Retire or redirect active legacy OpenCode/Ollama entry points and examples. Historical documents and
vendored provider code remain truthful history, but a repository scan over executable ContextMesh
demo scripts/configs/tests must show no active Ollama credential or endpoint dependency.

**Alternative considered and rejected — proxy native OpenAI traffic.** It would require a custom
provider and API bearer credential, would bypass native ChatGPT authentication, and recreates the
same collaboration-protocol risk the migration is meant to remove.

**Alternative considered and rejected — drop result compression and provider-visible TUI metering.** That is
smaller, but it silently removes two product claims the current combined demo and meter expose.

**Alternative considered and rejected — durable or per-process copies of `auth.json`.** They create
credential replicas and same-UID model tools can read them. Model-bearing product runs therefore
require native keyring storage and never copy or link file auth.

**Simpler option?** Native Codex config plus no stack is the simpler model path. The extra hook and
meter work is necessary only to preserve the existing ContextMesh compression and measurement
contracts honestly.

**Relevant ADRs:** `docs/decisions/0001-rrcv2-full-two-store-contract.md` remains authoritative for
Lane A/B. This migration changes product orchestration around the existing solver only; changes to
`rrc/contract.py` or its frozen stage/fallback/storage/event semantics are out of scope and require
the ADR's coordination sync point. The design ledger is otherwise silent for this subsystem.

### Frozen live quality gate

Before any live call, M4 writes and seals a deterministic parser/scorer plus its SHA-256 beside a
verified copy of the rubric. Reports have one accepted grammar: a heading exactly `##
src/handlers/<allowed-name>.js`, followed by zero or more claim bullets exactly `- <severity> |
<file>:<positive-line-or-range> | <one indivisible sentence>`. Severity is one of critical/high/
medium/low. Blank lines are ignored. A validly parsed claim may later be supported or unsupported;
every other nonblank line is a fatal syntax error, not a scored claim. A heading cannot assign another handler, a sentence cannot span bullets, and
duplicate bullets remain distinct precision-denominator claims.

Two independent blinded adjudicators receive only opaque cell/claim labels, the source-grounded
rubric with opaque finding labels, the audited source bytes/hashes, and each parsed claim—not token,
backend, intervention, or baseline labels. Each returns a sealed JSON record for every claim:
`{claim_id, finding_id|null, supported_entire_claim, rationale_hash}`. A finding match is accepted
only when both adjudicators independently choose the same single rubric ID and affirm that the
entire parsed tuple—assigned heading, reported severity, exact file, positive in-bounds line/range,
and every sentence conjunct/qualifier—is supported by that one finding. The rubric handler/file must
equal the tuple, the cited range must overlap the frozen source finding's accepted range, and reported
severity may equal or exceed but never downgrade the rubric severity. Zero,
multiple, disagreement, or a conjoined true-plus-unsupported assertion makes that valid parsed claim
unsupported. Malformed JSON or a missing claim adjudication is a fatal cell error. The deterministic scorer validates that every parsed claim occurs
exactly once in each adjudication file, then computes metrics from consensus matches. A rubric ID can
contribute at most one unit to both recall and the precision numerator, while every report claim
remains in the precision denominator; duplicate semantic claims therefore lower or preserve, never
raise, precision. A semantic nonmatch retains its parsed reported severity; syntax is never
silently converted into a claim. Fixtures cover
conjoined true/false claims, ambiguous/wrong headings, multi-ID claims, one supported claim repeated
beside one unsupported medium/low claim, missing severity,
and missing/disagreeing adjudications. For the scenario's assigned handlers:

- recall = unique matched rubric IDs / all rubric IDs in scope;
- precision = unique consensus-matched rubric IDs / all claim lines;
- handler coverage requires at least one matched ID for every assigned handler;
- category coverage requires at least one matched ID for every rubric category present in scope;
- a combined cell qualifies only with recall >=70%, precision >=90%, no unsupported high/critical
  line, and recall and precision each no lower than its same-scenario baseline.

Zero parsed claims have recall `0` and precision `0`. A syntactically valid claim that lacks
consensus is a representable unsupported claim; a malformed/unknown heading, non-claim nonblank
line, duplicate claim ID, or invalid adjudication schema is a fatal cell invalidation rather than an
ignored line. Wrong file, wrong/out-of-bounds line, and severity-downgrade fixtures must fail.

Before outputs, `sha256(run_id + rubric_sha256)` seeds a recorded permutation of opaque cell and
claim labels. The adjudicators/scorer receive no implementation label or token evidence; unblinding
and baseline comparison happen only after both adjudication files and immutable per-cell score
receipts exist. Any parser error, ambiguous handler, source/rubric/parser/scorer hash drift, missing
adjudication, or missing baseline invalidates the cell. This gate intentionally trades recall for
claim integrity: a valid paraphrase may be rejected by conservative adjudication, but one recognized
phrase cannot hide an unsupported extra assertion.

The implemented comparison uses a frozen, independently authored lexical rubric and a local
schema-exact scorer. It launches no evaluation model and has no adjudicator token threshold. The
scorer checks exact handler/line/severity anchors plus conservative lexical overlap, exposes
conjoined claims as `semantic_review_required`, and disqualifies those claims from automatic savings
eligibility. This deterministic gate is intentionally narrower than semantic review and cannot
establish audit correctness by itself. The canonical native audit prompt used identically by
baseline/local/EverOS contains the strict report grammar verbatim and its SHA-256 is part of every
cell receipt.

## Milestones

- [ ] **M1 — Native configuration, authentication, and lifecycle.** First add launcher/preflight
  tests that reject any Ollama/custom-provider dependency and require a native authenticated Codex
  home. Then generate native configs, stop sourcing `.env.local`, remove the RRD Tollgate/proxy
  lifecycle, make local `up/down` no-op-safe, and implement the owned EverOS state machine above.
  M1 owns wrapper/dispatcher/preflight/TUI, `env.example`, legacy `demo.sh`, lifecycle, and
  `Dockerfile.everos` rows only. **Acceptance:** local prep performs no
  service health check;
  EverOS checks only `:8000`; generated artifacts contain no provider URL/env key/credential copy;
  keyring-forced `codex login status` succeeds in the stable credential-free home; foreign/adopted/stale/partial/signal Docker and
  host cases are covered; poison ambient credentials reach no child/tool/artifact; effective features
  show plugins off and no MCP/project-skill loading; no active public surface reads an Ollama key.
- [ ] **M2 — Native hook result compression and token evidence.** First add hook tests for native
  `wait_agent`, fail-open preservation, sealed receipts, transcript usage parsing, malformed/FIFO/
  oversized evidence, and under-65% delivery. Exhaustive hook partitions use direct offline fixtures;
  an installed-CLI transport fixture is also offline and must not authenticate or call a model. The
  sole native-provider hook canary is deferred until after M4 freezes artifacts in M5. Implement
  compression at PostToolUse, record root and
  worker usage from pinned native transcripts, record local compression evidence, and remove the HTTP
  response proxy. M2 owns the hook/RRC/proxy inventory rows. **Acceptance:** a real installed-Codex fixture sees the compressed replacement;
  raw results remain recoverable; every exceptional path is bounded and passes the original result;
  no provider HTTP interception remains.
- [ ] **M3 — Native provider-visible meter.** First rewrite fixtures so Tollgate/proxy evidence cannot make
  READY and mutate each native usage source to prove fail-closed behavior. Then sum disjoint native
  root/worker/planner/seed usage (including cache-write input) and correlate exactly `N`
  assignments, `N` starts/stops, a complete
  wait, compression delivery, RRC packets, sources, backend, and root merge. **Acceptance:** valid
  COLD/WARM fixtures are READY with expected `N/0` and `1/(N-1)` MISS/HIT for `N=1,2,4` (including
  public `N=4` as 4/0 and 1/3); missing/duplicate/malformed usage,
  wrong transcript/receipt/hash/backend, fail-open, or incomplete worker evidence is NOT READY.
  M3 owns meter, benchmark, aggregate/analyze, Snowflake, and retained analytics rows.
- [ ] **M4 — Documentation, frozen scorer, and deterministic verification.** Implement and hash the
  deterministic quality scorer above, update README/examples/current RFC status, remove obsolete
  active OpenCode/Ollama configs/tests, and run syntax, ShellCheck, Ruff,
  Pyright, secret scan, and the entire project test suite. **Acceptance:** every deterministic check
  is green; an active-runtime scan finds no Ollama endpoint/key/Tollgate dependency; generated and
  test artifacts contain no credential bytes. M4 owns prompts, README, legacy config/plugin/script/
  test retirement, scorer/docs, and the exhaustive assertion that every inventory row has exactly
  one completed milestone disposition.
- [ ] **M5 — Live native-Codex product tests.** Verify the tracked independent rubric at
  `contextmesh/bench/rubric-independent.json` against SHA-256
  `f2c3b64825230d1862cda33b82ea5697cebb2fb00e645fd381d29e76d77c91d2` and all embedded source
  hashes. Freeze `codex-cli 0.147.0`, `gpt-5.5`, medium reasoning, generated config hashes, target
  bytes, prompts, and the intervention-only semantic diff. After native local and EverOS lifecycle
  canaries, run exactly nine controlled cells: baseline, combined-local, and combined-EverOS at one,
  two, and four workers in the declared Latin-square order. Each cell has a 12-minute wall timeout
  but no token watchdog, token ceiling, aggregate token budget, or token-triggered cancellation.
  Attempt every independent cell even after an earlier invalid result. Controlled combined cells
  use deterministic seed summaries and exercise one RRC MISS plus `N-1` HITs. Preserve exact native
  root/worker transcripts and planner model events, score the final report with the frozen lexical
  rubric contract, and label a reduction as savings only when the cell is valid and no worse than
  its scenario baseline. **Acceptance:** no Ollama process/URL/key is used; every declared worker
  overlaps and finishes; both backends meet lifecycle and evidence gates; all nine cells have an
  atomic valid/invalid/timeout summary; cleanup leaves no demo-owned process; reported usage is
  explicitly provider-visible and observational rather than exact billing.

## Definition of done

- [ ] Every disposition in the active-entry inventory is complete. Public `contextmesh/demo.sh`,
  all `RRDdemo*.sh`, and the benchmark path use native Codex authentication/models and make zero
  requests to Ollama or an Ollama/Tollgate proxy; the executable allowlist scan has no unclassified
  legacy-provider hit.
- [ ] Local and EverOS memory launchers both work; local requires no server, while EverOS starts or
  reuses only its exact owned `:8000` state, refuses foreign listeners, never calls the ordinary
  stack, and performs no LLM/embedding/rerank/multimodal request in demo paths.
- [ ] Generated Codex homes are isolated, plugin-free, credential-safe, and pass native login plus
  model canaries without requiring `OLLAMA_API_KEY`, `.env.local`, or a custom provider. Generated
  homes contain no auth file; keyring preflight succeeds before dispatch, refresh remains in native
  keyring storage, and per-round home contents are scrubbed before final archival while the canonical
  credential-free home path remains stable.
- [ ] Four native Codex workers receive one validated RRC packet and three current ContextMesh
  shared digests each, execute concurrently, and return one report per handler.
- [ ] Native PostToolUse compression implements the specified atomic multi-wait state machine,
  preserves exact raw receipts, handles partial/timeout/identical-repeat completion, rejects
  conflicting duplicates, and delivers the exact documented feedback shape at a strict UTF-8-byte
  ratio below 65%; any fail-open prevents READY.
- [ ] The meter uses only schema-valid provider-reported native Codex usage from disjoint root, worker,
  planner and seed summarizer calls; it enforces the version-2 schema,
  arithmetic/monotonicity/finality/identity invariants and published steady/setup equations.
  Malformed, missing, duplicate, failed, mixed, or uncorrelated evidence is NOT READY, with unknown
  failed-call usage labeled as a lower bound rather than zero.
- [ ] Ruff check/format, Pyright, ShellCheck, all project tests, active-runtime Ollama scan, and
  credential/secret scans pass.
- [ ] Live calls follow the frozen model/config/rubric/scorer, exact model/reasoning equality
  outside the sealed intervention diff, a 12-minute per-cell wall timeout, process-group termination,
  all-cell continuation, and ownership-safe restoration. No active token ceiling or aggregate token
  budget exists. Native-Codex baseline, local, and EverOS cells pass lifecycle/quality gates, or each
  precise blocker and provider-visible lower bound is reported without manufacturing a comparison.
- [ ] A multi-task/multi-agent observed-token table is produced only for comparable successful live
  runs and always discloses hidden-retry uncertainty; otherwise the final report explicitly states
  why the comparison is not measurable. It is never titled or described as exact billed savings.

## Verification plan

- **Types:** generate the NUL-delimited retained-Python list from the checked-in active-surface
  manifest, assert it excludes ignored `contextmesh/runs/**` without opening that tree, and pass those
  exact files plus `rrc/` and `tests/` to `uv run --locked pyright` with zero errors. The manifest test
  fails if any retained active Python file is absent.
- **Lint/format:** pass the same retained-Python manifest plus `rrc/` and `tests/` to
  `uv run --locked ruff check` and `uv run --locked ruff format --check`; ignored run evidence is
  excluded without traversal.
- **Shell:** `bash -n` and `shellcheck` for all public/current ContextMesh demo scripts.
- **Tests:** focused red/green suites for launcher/preflight, hook, meter, RRC planner, and installed
  Codex integration, followed by `uv run --locked pytest -q -p no:cacheprovider tests
  contextmesh/tests`; if the legacy test root is retired, the manifest and command are atomically
  updated so no retained test file lies outside explicit collection.
- **Runtime scan:** one test-owned exhaustive allowlist enumerates active ContextMesh executables,
  scripts, configs, README commands, tests, and RRC demo modules. `rg` for Ollama keys/endpoints,
  OpenCode, custom model providers, Tollgate, and response-proxy routing must have zero unclassified
  hits; only `docs/**`, ignored runs, submodules, Git history, and the scanner's literal patterns are
  excluded.
- **Credential safety:** exact-value scan for the current cached credential material is forbidden;
  instead assert absence of generated auth files, run the repository secret scanner, and scan artifacts for
  credential field names/token prefixes without printing values.
- **Live:** seal CLI/model/reasoning/config/rubric/scorer/source hashes; run the native local canary
  and EverOS owned lifecycle smoke; then run the nine-cell controlled matrix in its declared
  Latin-square order with 12-minute deadlines and no token-based cancellation. Preserve exact
  provider-visible transcript usage, frozen lexical-rubric scoring, process/port restoration, and a
  final artifact manifest; regenerate the aggregate report from cell summaries and compare hashes.

## Risks & one-way doors  ⚠️

- Codex transcript JSONL is documented as unstable for hooks. Mitigation: keep the CLI pin, parse a
  minimal strict token-count subset, bind file identity, cap reads, and fail NOT READY on drift.
- PostToolUse blocking feedback replaces a tool result but may wrap the reason in Codex-owned text.
  Mitigation: the exact feedback object and state transitions are fixed above; installed-CLI tests
  and the live canary must prove receipt delivery and subsequent partial waits before READY.
- Native ChatGPT subscription accounting and API-key billing have different commercial semantics.
  The meter reports provider tokens, not dollar cost, and records the login method without exposing
  credentials.
- The stock pinned CLI exposes the complete native v1 collaboration family (`spawn_agent`,
  `send_input`, `resume_agent`, `wait_agent`, `close_agent`) plus its normal built-in tool schemas;
  it has no supported per-function schema allowlist. The product accepts that stock surface, disables
  plugins/apps/MCP/web tools and installs a sealed deny-read profile for known file-based
  credential roots (`~/.codex` session/config files,
  SSH/cloud credentials, and `.env.local`). The native client must retain access to the macOS
  Keychain for ChatGPT login, so this boundary explicitly does not deny the system keyring. The
  nested RRC planner inherits the same file-deny profile.
  On macOS the outer Seatbelt profile is the sandbox authority: Codex is launched in its documented
  external-sandbox mode because a sandboxed process cannot apply a second Seatbelt profile for a
  worker. On non-macOS systems Codex retains its native read-only command sandbox. The macOS outer
  profile is deliberately a credential deny-list, not a general read-only or network-isolation
  boundary; this is a disclosed limitation of the local demo, whose target is synthetic and whose
  prompt forbids edits.
  Full handler/shared source bytes and hashes are injected into each worker's immutable context, so
  the audit needs no discretionary filesystem discovery. Offline installed-CLI tests inspect the
  exact offered schemas and prove OS-level denials for direct/discovery reads of the protected
  file roots. The threat model is the normal native-Codex boundary; it does not
  promise isolation from an already-compromised same-UID host outside the pinned sandbox.
- Removing active legacy OpenCode configs changes the old `demo.sh` UX. The public path remains as a
  compatibility redirect with an explicit message; historical artifacts remain available in Git.
- Live multi-agent tests can consume substantial tokens, and native login supplies no hard spend cap.
  The operator explicitly removed experimental token aborts. Sequence cheap canaries first, retain
  the 12-minute wall timeout for liveness, attempt all independent cells, preserve invalid evidence,
  and report unknown hidden-retry consumption without calling provider-visible totals exact billing.

## Open questions

- None blocking. The design ledger was silent; explicit user instructions resolve provider choice
  and removal of the prior token ceilings. Live testing proceeds with a wall timeout only because the
  native ChatGPT-authenticated CLI has no enforceable hard billing reservation.

## Deviations log (fill during implementation)

- 2026-08-08: Plan created before migration work.
- 2026-08-09: Active ContextMesh launchers/config/hooks/meter/benchmark/docs were migrated to
  native Codex; the retired provider configs, model-traffic proxies, plugins, analytics loaders, and
  their legacy-only tests were removed. The deterministic gate passed 162 tests, Ruff, Pyright,
  Bash syntax, ShellCheck, diff whitespace, active-surface scan, and a targeted secret scan.
- 2026-08-09: Local reset/seed and the fail-closed pre-run meter passed. A real assistant-only
  EverOS add/search smoke test passed against the listener on port 8000. The demo-owned EverOS
  launcher correctly refused to adopt that listener because it is an unrelated SSH process.
- 2026-08-09 (historical): the first paid canary attempt stopped before dispatch because the stable
  generated Codex home had no keyring login. The operator subsequently completed native Codex login.
- 2026-08-09: After login, the local canary, EverOS storage-only smoke, and all nine controlled
  matrix cells completed. All nine passed the execution/evidence protocol. Neither combined backend
  saved provider-visible tokens in this single replicate; the tracked results report contains the
  exact per-cell totals, hashes, and limitations.
