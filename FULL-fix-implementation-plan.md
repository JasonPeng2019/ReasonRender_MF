# FULL: ContextMesh + RRCv2 Implementation Plan

Status: authoritative description of **what must exist in code**. Execution
order, commands, handoffs, and run control live only in
[FULL-fix-serial-workflow.md](FULL-fix-serial-workflow.md).

## Objective

Reduce real multi-agent compute without making the work artificial.

- Four external DeepSeek Flash workers must receive four different tasks, isolated worktrees or
  branches, separate write ownership, and their own initial source-read sets.
- Every initial source-read set must be complete for its task. A raw worker may
  not silently discover an undeclared implementation pattern; a non-raw worker
  may directly read only its declared unique files and must obtain each declared
  overlap from ContextMesh.
- ContextMesh reduces duplicated **worker** reads only where those sets naturally
  overlap. It must not force a universal common-file list.
- ContextMesh must also preserve a workflow-scoped, hash-bound brief lineage so
  later-stage workers receive an unchanged file's complete brief rather than
  rereading its body. A legitimate change refreshes that lineage once from the
  prior brief plus its Git diff; it never restores peer raw fallback.
- RRCv2 reduces Terra's repeated repository/specification reads when producing
  those four plans, including later-stage delta plans from retained prior plan
  state and validated dependency revisions.
- The `full` arm combines both mechanisms. Its compute and quality must beat the
  `contextmesh` arm materially; ContextMesh must already materially beat `raw`.

The comparison remains Codex-first. No Claude validation or digest child is
authorized until a valid Codex three-arm result passes the economy gate.

The measured lane uses no host collaboration/API subagent (for example, a
generic `Bohr` worker). Its one coordinator and four workers are only the
direct headless `codex exec` processes specified in the workflow, with their
retained JSONL streams and explicit bypass flag. An auxiliary or interrupted
collaboration agent is never a substitute for, or evidence about, Terra/DeepSeek.

This is also the durable delegated implementation policy: the current Codex
parent stays the project orchestrator, while each authorized coding, review, or
test slice uses the checked-in external DeepSeek launcher with its own session
key, retained stream, and full-access command. The workflow never falls back to
DeepSeek worker or a native host collaboration worker for that project work.

A one-stage fan-out proves only same-stage overlap handling. It is not proof of
the required persistent reuse. The promoted V23 result remains valid proof of
the original role-specific gates, but a new three-stage linked workflow is
required before claiming cross-stage ContextMesh or stage-aware RRCv2 savings.

## Product model

### Three comparable arms

The manifest fixes four task ids. The corresponding worker in every arm has the
same objective, worktree/branch, write paths, acceptance tests, and initial
read set. The four workers within an arm differ from one another. Raw and
ContextMesh additionally receive the same four retained Terra plans, identified
by one delivery-plan SHA-256; otherwise a coordinator-plan difference would be
mistaken for a ContextMesh worker effect.

| Arm | Terra plan production | DeepSeek worker source access |
| --- | --- | --- |
| `raw` | Terra reads repository/specification and constructs four plans. | Each worker directly reads its own declared sources, including duplicate overlap reads. |
| `contextmesh` | Reuses Raw's exact retained raw-Terra artifacts. It does not spend a second Terra run that could change plan quality. | One source owner reads each actual overlap once and publishes a complete brief. Participating peers consume the brief. Unique files remain local direct reads. |
| `full` | A retained, local-only RRC prewarm stores the generic template before Terra starts; Terra then performs a valid RRC HIT, skips reconstruction, and renders four concrete plans from the template and bindings. | Identical ContextMesh treatment to `contextmesh`. |

Every arm retains and dispatches its own original Terra plan artifact. The
ContextMesh arm dispatches Raw's exact retained artifacts; Full dispatches its
own exact RRC-resolved artifacts. The harness validates every artifact against
the same frozen task and source-fact contract. It is not a static fallback: a
malformed or source-contract-incomplete Terra artifact still rejects dispatch.

### Canonical ContextMesh topology

ContextMesh is a per-overlap worker-sharing mechanism, not a global queue and
not a generic digest cache.

```
Terra creates four different plans and dispatches all four DeepSeek workers together

Worker 1 / task-1 / branch-1: reads {domain, evaluator, policy catalog}; owns output-1
Worker 2 / task-2 / branch-2: reads {domain, evaluator, registry, policy catalog}; owns output-2
Worker 3 / task-3 / branch-3: reads {evaluator, registry, rules/base, errors}; owns output-3
Worker 4 / task-4 / branch-4: reads {domain, registry, rules/base, service, policy catalog}; owns output-4

one arm-local shared broker
  domain: worker 1 raw once -> complete brief -> workers 2 and 4 brief only
  evaluator: worker 3 raw once -> complete brief -> worker 1 brief only
  policy catalog: worker 2 raw once -> complete brief -> workers 1 and 4 brief only
  registry: worker 4 raw once -> complete brief -> workers 2 and 3 brief only
  rules/base: worker 3 raw once -> complete brief -> worker 4 brief only
  errors and service: unique local reads; no broker entry and no ContextMesh credit

per overlapping file: unclaimed -> owner_raw -> brief_published -> peers_served
never: peer raw fallback, hidden digest model, universal probe, copied plans,
       per-worker cache/server, or global worker serialization
```

Each headless Codex process may use a local stdio MCP bridge, but every bridge
MUST forward to the same broker for that arm. A process-local `gate` or cache is
not shared state and is invalid for the comparison.

A bridge must process independent JSON-RPC requests concurrently and serialize
only its stdout writes. A waiting peer retrieval must never queue behind the
same worker's later owner publication. When the controller has already sealed
every relevant brief into a worker packet, do not start or expose a bridge for
that DeepSeek worker; the packet itself is its complete ContextMesh source contract.

Ownership is deterministic but load-balanced across eligible readers: choose
the participating worker with the fewest already assigned overlaps, breaking a
tie by worker id. This prevents the lexicographically first DeepSeek worker from owning a
large catalog plus unrelated files. It does not add a common file or alter any
worker's independent task/read set.

### Workflow-persistent staged topology

One non-raw arm keeps the **same broker and ledger namespace for all stages of
one workflow**. Arm namespaces never mix: Raw has no broker, and the
`contextmesh` and `full` arms each retain their own broker, branch lineage, and
state. A fresh comparison never consumes another arm's state.

```
stage 1: four distinct DeepSeek worker tasks -> owner raw read once -> complete brief@H1
                                  -> accepted owned changes -> stage commit C1

stage 2: four distinct DeepSeek worker tasks ask path@C1
  unchanged H1: brief@H1 served directly; no source body
  changed H2: one refresh owner gets brief@H1 + git diff C0..C1
              -> complete brief@H2 -> all other readers get brief@H2 only

stage 3: the same rule against the stage-2 commit C2
```

The stage boundary is a deterministic merge of the prior stage's disjoint,
accepted owned paths followed by a recorded Git commit. Stage `n + 1` starts
from that arm's recorded stage-`n` commit; it is not a fresh repository copy.
This makes a later request's path hash and `git diff` meaningful while keeping
the four task plans at each stage genuinely distinct and parallel.

For an unchanged source, adjacent stages use the same compact source-fact
contract. The first stage's owner therefore publishes a complete brief that
the next stage receives unchanged, without a new raw read. The catalog contract
is intentionally a two-stage pair, never a workflow-wide union: it contains
only the eight literal record anchors needed by those two stages. Each worker's
exact profile binding (source field, comparator, denial code, and expected
value) lives in its distinct Terra plan, not in the shared catalog brief. No
brief carries a later objective, output path, test, or command. A source
deliberately changed at a boundary, such as the two-worker rollout contract,
is excluded from unchanged reuse and follows the prior-brief-plus-diff refresh
path instead.

The lineage `requirements_hash` is a hash of the canonical source path, brief
schema, and literal source facts only. It must never include a stage manifest,
task id, worker objective, plan steps, or acceptance command: those values are
allowed to change between genuinely different tasks, while the source brief is
supposed to be reused. Each stage still has a new audit `brief_id`, and the
broker authorizes current workers from that stage's ledger before serving it.

The live broker advances only through a harness-only `install_stage` control
operation protected by an arm-local control token that is never placed in a
DeepSeek worker packet or bridge environment. The operation accepts a new immutable stage
ledger plus `(stage id, stage commit, parent stage commit, branch lineage)` and
refuses a parent mismatch, duplicate brief id, or unpublished prior-stage
brief. It retains the same broker process and state directory, appends the new
brief revisions, records `stage_installed`, and then resolves each new path as
unchanged reuse, diff refresh, or explicit invalidation. A DeepSeek worker can call only
claim/chunk/publish/get operations; it cannot install or replace a ledger.

The exact headless MCP configuration is part of the workload contract. Codex
does not reliably forward a dispatcher's `PYTHONPATH` into an MCP child, so the
bridge configuration must set `mcp_servers.contextmesh.cwd` to the repository
root that contains `contextmesh/`; a launcher-side environment variable is not
proof. Its `env_vars` allowlist must explicitly forward
`CONTEXTMESH_BROKER_HOST`, `CONTEXTMESH_BROKER_PORT`, and
`CONTEXTMESH_WORKER_ID` to the stdio child, and the server must set
`required = true` so a missing bridge cannot silently degrade the model tool
surface. The measured Codex DeepSeek surface exposes this server directly as
`mcp__contextmesh__claim_source` (and its companion tools), not through a generic
resource listing or an `ALL_TOOLS` discovery function. Packets must issue the
named ContextMesh call directly and treat an MCP-visible endpoint error as
surface evidence; they may not fall back to raw source.

## Required code changes

### 1. Four-plan and overlap data model

Add a concrete plan record for each worker. It must contain:

- `task_id`, worker id, isolated worktree/branch, objective, owned write paths,
  named acceptance command, and rendered task packet;
- an initial read set with canonical source paths; and
- only the overlaps relevant to that worker, not the other workers' full plans.

Terra computes the overlap ledger from the four read sets before dispatch. A
file enters the ledger only when at least two different plans request it. Every
ledger entry contains:

```json
{
  "brief_id": "round/arm/source/requirements",
  "canonical_path": "...",
  "manifest_hash": "...",
  "requirements_hash": "...",
  "plan_steps": ["task-1:step-x", "task-2:step-y"],
  "source_owner": "worker-01",
  "peer_workers": ["worker-02"]
}
```

The same task-to-worker mapping, read sets, owned writes, acceptance checks,
and coordinator-plan schema are frozen across all three arms. Raw/ContextMesh
Terra reconstruction and Full RRC render each produce four retained concrete
plans; the dispatcher sends those exact retained plans to the matching DeepSeek workers.
It must never replace them with a generic or static delivery packet. Each arm
records a hash of the actual four-plan delivery set, while comparison validates
the fixed worker-contract hash and task outcomes across arms. The `contextmesh`
and `full` packets add only their relevant ledger entries.

For a staged workflow, the ledger additionally records immutable lineage, not
just a round-local claim. A published brief revision contains at minimum:

```json
{
  "workflow_id": "ruleforge-staged/<arm>/<cohort>",
  "lineage_id": "canonical-path plus branch lineage",
  "stage_id": "stage-01|stage-02|stage-03",
  "canonical_path": "...",
  "commit_sha": "...",
  "content_sha256": "...",
  "parent_content_sha256": "...|null",
  "requirements_hash": "...",
  "brief_facts_hash": "...",
  "brief_schema_version": "file-brief/v1",
  "refresh_kind": "raw|unchanged_reuse|diff_refresh"
}
```

`workflow_id` scopes a retained staged run; `lineage_id` prevents a same-named
path on another branch from being treated as the same file. A request is an
unchanged reuse only when workflow/lineage/path and content hash match, the
prior complete brief covers every current required fact (it may safely contain
additional source facts), and the schema remains compatible. It may then serve the already validated brief
directly, with no owner claim and no model-visible source body. A changed path
creates a new revision under the same lineage; it must not overwrite or mutate
the older revision.

### 2. Plan-bound file brief contract

The brief replaces raw source for a participating peer's dependent plan step.
It is not a loose outline and must have no verbatim file body. The owner
publishes exactly this compact, **broker-held** payload:

| Field | Required content |
| --- | --- |
| `purpose_and_api` | Relevant symbols, signatures, behavior, mutation, and side effects. |
| `data_and_dependencies` | Relevant data shapes, literals/configuration, imports, collaborators, and direction. |
| `behaviour_and_failures` | Ordered control rules, validation, invariants, errors, fail-open paths, and surprising edges. |
| `plan_step_facts` | Source-derived consumption facts: exact call/constructor shape, public result fields, collection shape, and source errors where present. |
| `anchors` | Relevant source symbol or fact locators for all material facts; line ranges are useful when easy but optional. |

The broker, not the DeepSeek worker, attaches immutable audit metadata: `schema_version`,
canonical source path/language/SHA-256/line count/raw size, manifest and
requirements hashes, `brief_id`, owner and peer ids, and a compact worker-id to
step-index `plan_scope`. That metadata is persisted for audit but never sent to
a peer. This prevents repeated source/binding/plan text from consuming the
worker savings that the brief is meant to create.

The owner-only contract names the five fields, byte budget, target, and an
authoring rule. It contains **no peer task prose**: each peer already receives
its distinct plan in its packet, so copying objectives, file paths, test
matrices, or commands into a tiny source brief wastes the available budget and
can make a valid publication impossible. The owner records exact source facts
that a consumer needsâ€”call/constructor shape, result fields, collection shape,
and source errorsâ€”rather than a generic outline that forces test repair.
The complete plan-to-overlap ledger remains broker/audit data and is never
copied into a DeepSeek worker prompt. A sealed worker receives neither routing identity
nor a full summary: the broker projects only that worker's literal required
facts, grouped by canonical path, from the complete validated brief.

The owner-only `brief_template` also carries the union of compact
`required_source_facts` for participating plans. Each is a literal,
punctuation-insensitive source-contract string naming a needed API, data shape,
configuration key, result field, or error condition; it is not peer task
prose. Publication is rejected unless the five summary fields contain every
required source fact. This lets the owner of a large file cover every
consumer's needed facts without receiving or copying their objectives, paths,
tests, or commands. A selector that is merely shown to an owner but not checked
is not a contract and is prohibited.

The complete broker-held brief has a compact budget derived from the union of
literal `required_source_facts`, with fixed headroom for all five required
fields. It must **never** be derived from raw-file size: a 130 KB generated
catalog does not justify a 110 KB "summary." The owner publishes the five named
fields as a fixed ordered array `[purpose_and_api, data_and_dependencies,
behaviour_and_failures, plan_step_facts, anchors]`. The broker then makes a
second, smaller decision for each sealed worker: it returns only the exact
fact strings requested by that worker's distinct plan and proves each one is
covered by the complete owner publication. It does not copy all five fields,
the union of other workers' facts, anchors, or a `brief_id` into every packet.
The owner contract's target is the **total across all five values**, never a
per-field allowance; for a source below 512 bytes it gives the explicit
one-short-phrase-per-field rule. The owner contract also gives a smaller target
character count and requires terse semicolon-separated fact fragments rather
than prose, especially for a small source. A brief that is merely short but
insufficient is not valid: every worker's requested fact projection must let
it complete its declared step and acceptance test without source access.

For a natural overlap that is too large for an efficient owner read, the broker
may send the source owner a **plan-scoped raw excerpt** rather than its
irrelevant generated tail. The excerpt contains the module header, tail API
helpers, and raw windows proving every literal required-fact selector. It is
accepted only when every selector is found; otherwise the broker falls back to
the complete file. The full-file SHA-256 still binds the brief and the broker
records full size, owner-view size, and view kind. This is not a hidden
summarizer or peer fallback: one declared DeepSeek worker remains the only recipient of
raw source, and peers receive only their broker-validated fact projection.

For a later stage whose path hash changed, the normal refresh input is **not** a
second raw file read. The deterministic refresh owner receives the immediately
prior validated five-field brief and the bounded
`git diff --no-ext-diff <prior-stage-commit>..<current-stage-commit> -- <path>`
for that lineage, plus the next revision's required facts. It publishes a new
five-field brief whose coverage is validated against the changed requirements.
The diff and prior brief are owner-only; peers receive only their new compact
fact projection. The broker records the old/new hashes and
`refresh_kind=diff_refresh`.

A fresh raw owner view is allowed only when there is no valid ancestor brief,
the diff is unavailable, the bounded diff cannot establish every required fact,
or the change is classified invalidating (rename/merge ambiguity, excessive
changed regions, or changed required-fact coverage). The replacement owner
receives the smallest plan-scoped source view that resolves that condition and
the broker records the explicit invalidation reason. Peers still wait for the
new brief; they never receive a diff or raw fallback.

### 3. Shared broker and MCP bridges

Replace the current per-invocation stdio server topology with one durable,
arm-local broker and a small bridge for each Codex worker. The broker exposes:

| Operation | Required behavior |
| --- | --- |
| `claim_source(brief_id)` | Atomically selects the declared owner before an await. For a first revision or recorded invalidation, only that owner receives one plan-complete raw source view, its full-file SHA-256, chunk count, and a small `brief_template` contract: five required headings, fact-derived byte budget, target, and source-only authoring rule. For a changed descendant with a valid ancestor, the refresh owner instead receives `{prior_summary, prior_content_sha256, current_content_sha256, git_diff, brief_template}` and no raw body. A large raw source uses a header/tail/required-record excerpt only when every fact selector is present; otherwise it stays a full-file view. A peer never becomes a second raw reader. |
| `read_source_chunk(brief_id, chunk_index)` | Owner only, after `claim_source`. Returns the next bounded owner-view chunk in strict ascending order. The broker will not accept a brief until every chunk for that one logical claim was delivered. The normal plan-scoped view is deliberately single-turn; chunks are a conservative full-view fallback, never a peer raw fallback. |
| `publish_file_brief(brief_id, source_hash, brief)` | Accepts only the owner and validates source hash, all five compact fields, byte budget, and coverage of every literal `required_source_fact`. The bridge keeps exactly those five fields and ignores copied claim audit metadata; the broker binds and persists authoritative metadata itself. A bad payload returns `brief_incomplete` but retains `owner_raw`: the owner repairs the same compact payload once without another raw claim. |
| `get_ready_worker_briefs(requests, worker_id)` | Receives declared `brief_id` plus that worker's literal required facts. For each exact valid revision, validates that the complete owner brief covers those facts, then returns only those fact strings keyed by canonical path. A sealed packet contains no raw source, `brief_id`, full summary, diff, or audit metadata. It waits for a current refresh rather than falling back to raw reads. |

Every operation resolves a copied `brief_id` only if it is one insertion,
deletion, or substitution from exactly one entry that the caller is authorized
to use in that operation's current state. The broker records supplied and
canonical ids and returns/uses the canonical binding. It never guesses a
multi-edit or ambiguous id, and reconciliation never grants a raw reader or
peer that the ledger did not already authorize.

State is keyed by `brief_id` before a claim and durably by `(workflow id,
lineage id, canonical path, content SHA-256, requirements hash, schema version)`
after publication. A revision links immutably to its prior revision and stage
commit. Locks are per `brief_id`; unrelated source owners and unrelated diff
refreshes proceed concurrently.

After a harness-only interruption, a replacement arm-local broker rehydrates
only persisted briefs whose immutable binding exactly matches the current
ledger. It serves those peers without rereading their raw source. Unpublished
overlaps remain unclaimed for their original owner; this is continuation, not a
second-reader fallback.

The same rehydration rule applies at a normal stage boundary: retain the broker
state, verify the next stage's recorded parent commit and requested path hash,
then serve an exact unchanged revision or begin the single declared diff
refresh. Do not clear this state between stages of one workflow. Clear it only
when a fresh arm/workflow namespace begins, so a later cohort cannot receive a
brief from V23, V24, Raw, or another arm.

Owner failure or timeout produces `brief_missing`. An incomplete publication is
a repairable owner error while the owner keeps its one raw claim; only an
unrepaired deadline produces `brief_missing`. It preserves the partial round
and stops the dependent peer step visibly. It never triggers peer raw fallback
or automatic paid-run restart.

The broker's publication deadline is ten minutes, within the dispatcher's
fifteen-minute arm bound. It must accommodate a DeepSeek worker reading and distilling a
large natural overlap. A peer timeout is a confirmed ContextMesh protocol
failure: retain the arm, classify it as product evidence, and prevent a later
paid arm from repeating the same broken broker state.

Each owner consumes every overlap it owns and publishes all of those compact
briefs before it calls `get_file_brief` for an overlap owned by another DeepSeek worker.
This owner-first rule prevents a worker that both owns and needs overlaps from
waiting before it has released the source a downstream peer needs. Independent
overlap owners still proceed concurrently; only the natural per-file dependency
waits.

For an owner with more than one overlap, this is a strict serial transaction:
`claim one -> read every required owner chunk -> publish that one -> repair once
if necessary -> claim next`. Batching raw claims is prohibited. An owner must
never ask for a peer brief or return `CONTEXTMESH_BRIEF_UNAVAILABLE` while it
has an unpublished claimed source.

The source-owner DeepSeek worker creates the brief from its own single raw read. Do not
launch an uncounted summarizer model or a fifth task-worker process.

One exception is permitted before a paid non-raw comparison: a single,
source-free **headless MCP eligibility probe** for the exact Codex CLI version,
model, flags, and bridge configuration. It is not a task worker, has no
RuleForge source access, and makes one deliberately invalid `claim_source`
call. Its purpose is to prove that the model-visible tool surface includes the
bridge. Retain its stream and token use separately; it is infrastructure-test
overhead, not worker primary compute. A launcher/config/stdio `tools/list`
check alone is never sufficient.

### 4. Source-read policy and instrumentation

For an overlapping source in `contextmesh` and `full`:

- an initial or invalidated owner may receive source only through
  `claim_source`; a diff-refresh owner receives only its ancestor brief plus
  Git diff through that operation;
- participating peers and later-stage unchanged consumers use
  `get_file_brief` only;
- a worker publishes every owned overlap before retrieving any peer overlap;
- non-raw packets omit overlap paths from `local_read_paths`; direct reads are
  allowed only for those remaining unique paths, and any peer brief error ends
  that worker's dependent step without a raw fallback;
- direct raw access through `Read`, `Glob`, `Grep`, `Get-Content`, `cat`,
  `type`, `sed`, `python`/`node` file reads, or equivalent shell commands is
  rejected or recorded as invalid peer evidence.

Unique sources remain normal reads for the one worker whose plan needs them.
The harness must not pretend that this has produced ContextMesh savings.

The worker worktree itself enforces this model-visible boundary: only permitted
direct source paths are materialized as text `.py` files. Every other fixture
dependency needed by Python at test time is sourceless `.pyc`; its raw body is
available only through `claim_source` for an owner or a broker brief for a peer.
The harness records this source view and rejects a worker view exposing an
unexpected source body. Reading or disassembling bytecode is a source-policy
violation, not a fallback.

Record at minimum:

`source_claim_raw`, `brief_reused_unchanged`, `brief_refresh_diff`,
`brief_refresh_invalidated`, `brief_published`, `brief_served`, `brief_wait`,
`brief_missing`, `brief_incomplete`, `packet_insufficient`, `brief_id`,
workflow/lineage/stage ids, stage commit, parent/current source hashes,
requirements and brief-facts hashes, diff/brief/raw size, owner id, peer id,
wall time, and per-role token use.

### 5. RRCv2 integration

Wire the existing RRC runtime into the actual Terra coordinator path.

- `raw`: Terra performs raw preflight and plan reconstruction for the four
  tasks exactly once.
- `contextmesh`: dispatches Raw's exact retained plan artifacts and records the
  raw Terra stream as inherited planning evidence and logical planner cost. It
  never launches a second Terra model process.
- `full`: before measured Terra starts, `warm_rrc_cache` persists the generic
  template in the local SQLite store and parks an EverOS assistant buffer under
  a deterministic case-shape session key. That buffer is deliberately **not
  flushed**: lookup uses EverOS's exact `filters.session_id` read-your-write
  path, so no extraction/provider model is invoked. It contains only the stable
  case shape and an opaque SQLite reference—never source bodies, bindings, or
  a rendered worker packet. The retained warm record is required.
- `full`: measured Terra performs lookup only, retains HIT/MISS evidence, skips
  reconstruction on a HIT, and uses the existing exact renderer to produce four
  resolved packets. It must not populate the cache during its measured turn; a
  missing warm record, local SQLite template, or exact-key lookup is a failed
  cache preflight that prevents paid Terra/DeepSeek worker launch. `generic_packet` plus
  detached `slot_values` in the frozen task manifest is prohibited.
- RRCv2 also persists an immutable **stage-plan state** after each successful
  Terra dispatch: workflow/arm namespace, stage id and parent stage id,
  template/version, four distinct task bindings, delivery-plan hash, frozen
  plan-anchor hash, and the ordered ContextMesh dependency revisions
  `(lineage id, content hash, brief-facts hash)`. It stores plans and bindings,
  never source bodies, worker briefs, code, or tests.
- For stage 2 or 3, Full Terra performs `lookup_stage_plan` using the workflow
  lineage, current task shape, prior stage-plan key, plan-template version, and
  the current dependency-revision tuple. On a valid HIT it renders a **delta
  plan** from prior structure plus the stage task delta, without repository
  reconstruction reads. Unchanged brief hashes preserve the matching plan
  dependencies; a changed hash invalidates only plans that cite that lineage.
  Terra receives retained plan state and the approved task/anchor delta, not a
  raw file, ContextMesh brief body, implementation recipe, or worker solution.
- A stage-plan MISS, a changed dependency without a validated refreshed brief,
  an unresolved template field, or a cache record from another arm/workflow is
  a retained diagnostic. It may not be mislabeled as an RRC saving and may not
  silently fall back to raw reconstruction in the measured Full stage.
- Terra evidence is separate from worker evidence. RRCv2 is measured from Terra
  plan production; ContextMesh is measured from worker overlap handling.
- The frozen manifest is a task contract only. It must not contain a rendered
  worker packet or prewritten coordinator steps. Raw Terra writes four
  validated coordinator-plan artifacts after preflight; ContextMesh reuses
  those exact files; full Terra writes the same artifact shape through the
  actual RRC runtime renderer. Only then may the dispatcher derive the overlap
  ledger and four DeepSeek worker prompts from those files.
- The frozen task contract includes literal source-fact verification anchors for
  each declared read. They are neither a prewritten plan nor a worker packet:
  raw/ContextMesh Terra must still inspect the sources and create its own steps.
  The dispatcher rejects a Terra artifact that omits any anchor, so a plausible
  but invented API cannot be handed to four paid workers. The same anchors bind
  ContextMesh brief coverage for their peers.
- A worker contract has two deliberately separate fact sets. **Terra plan
  anchors** are small, literal read-verification anchors retained in a
  coordinator plan; they prove the preflight/HIT plan binds the right sources
  but intentionally do not reproduce the operational API or policy values.
  **Brief facts** are the complete task-relevant symbols, data, behavior, and
  edge facts validated in the five-field ContextMesh brief. Raw workers acquire
  those operational details from their permitted source reads; ContextMesh
  peers acquire exactly them from the owner brief. Do not put the complete
  brief-fact set into `TerraPlan.source_facts`, or source sharing becomes pure
  extra ceremony with no worker-read saving to measure.
- RRC only saves **Terra's** reconstruction reads. It must not lower DeepSeek worker work
  by handing DeepSeek workers implementation code. The dispatcher verifies that each retained
  coordinator artifact covers every frozen source-fact anchor and dispatches that
  exact valid plan. Extra
  non-solution observations remain audit-only and cannot make a brief redundant.
  A delivered plan may give ordered
  task steps, but it must contain no imports, code snippets, function or class
  body, prewritten test, `MODULE PATTERN`, `TEST PATTERN`, or "write exactly"
  instruction. A plan may not direct a worker to re-read or directly inspect a
  source file: the Raw or ContextMesh worker packet exclusively owns that
  source-access decision. The dispatcher rejects either violation before any
  DeepSeek worker is launched. Thus worker-token changes are attributable to ContextMesh,
  not an RRC plan that accidentally pre-solves the worker task.

### 6. Comparison runner and report

Terra remains `gpt-5.6-terra` High/priority/bypass with a 230,000-token
auto-compaction threshold. Every measured worker is
the external-Codex profile `.codex/delegates/deepseek.toml`:
`deepseek-v4-flash:0731-cloud` through the local Ollama provider, a
1,048,576-token context window, and a 230,000-token auto-compaction threshold.
The catalog must be supplied on both fresh and resumed commands. A fresh worker
uses `codex exec --oss --local-provider ollama`; because `codex exec resume`
does not accept `--oss`, a resumed worker instead explicitly sets
`model_provider="ollama"`. Both forms use strict config, ignored user config,
full access, JSONL output, and the same model/compaction/catalog settings.
Each `arm/stage/worker-id` owns its own stream and session id: parallel workers
never share a named delegate session. A changed worker provider, model, catalog,
context window, or compaction threshold requires a fresh measured cohort.

Replace the suspended `harness/codex_compare.py` topology only after the broker
and Terra integration exist. The runner must:

- start EverOS locally when the selected broker backing requires it;
- start one broker per non-raw arm, then four independent DeepSeek commands in one
  Terra dispatch batch;
- run a staged workload as three linked four-DeepSeek worker batches. Preserve the
  non-raw arm's broker, ledger, stage commits, and RRC state across those
  batches; Raw performs its own declared direct reads at every stage. Merge
  only accepted disjoint owned-path changes into the next stage baseline and
  retain each boundary commit, diff, and source-hash inventory;
- retain five streams, handoffs, overlap ledger, manifest hash, bridge/broker
  logs, usage, wall-clock, task results, and test evidence per arm;
- retain an immutable attempt/child record before every Terra or DeepSeek launch;
  a timeout terminates and reaps that retained child before any single-thread
  resume, so two clients can never run one Codex session;
- attribute Terra, worker, and any counted broker work separately, then report
  aggregate primary compute as `input + output + reasoning` tokens; and
- reject a comparison from promotion when arm outcomes differ, plans/read sets
  drift, broker evidence is incomplete, peers read raw overlap bodies, or Terra
  lacks raw-preflight/HIT-render evidence.
- persist every delivered worker packet as `workers/<id>/packet.json` before
  launch and every stream-derived direct local-read set as
  `workers/<id>/local-read-set.json` afterward. Promotion validates those
  retained artifacts and their hashes against the frozen task contract; it
  never calls a renderer to recreate a packet as proof.
- validate every non-raw `(stage, brief)` independently: the declared owner
  must have the one applicable raw/diff/reuse claim, every raw/diff revision
  must have one matching owner publication, every declared peer must have a
  retained brief service/projection, and service must follow publication or
  unchanged-reuse installation. The aggregate reuse/diff gate passes only
  after every row is valid in both non-raw arms.

The report's evidence gate is explicit and independently visible: complete
stage statuses and exact accepted worker IDs; valid stream collection;
source-policy validation; the single retained DeepSeek MCP admission probe and
health result; Raw/ContextMesh exact delivery-plan pairing; manifest/worker
contract parity; required broker publication/reuse/diff-refresh events; and
fully bound Full RRC HIT records. Promotion requires every evidence condition
and every role-specific economy condition. It also shows gross input/cache,
owner/peer/both worker totals, per-stage totals, and stages-2+3 totals.

Each arm attempt owns immutable streams. Before starting any Codex process,
write an attempt record. An attempt with any stream, child record, or attempt
record is never relaunched into the same paths. A zero-worker failure can run
only the retained dispatcher; a partial-worker failure continues only the
interrupted Codex session ids with `codex exec resume`, in parallel, while
completed workers are retained untouched. The resumed JSONL is appended to its
same-session prior stream so its token cost is counted, never discarded.
Collection requires UTF-8 or UTF-16 JSONL with one or more completed turns for
one retained session and no NUL-byte/sparse-file corruption. A dispatcher
plan/MCP failure writes a retained error and ends the parent poll immediately.
Every worker process must put the harness virtualenv's `Scripts` directory
first on `PATH`. The sealed fixture view deliberately stores unavailable
modules as bytecode built by that interpreter, so an unqualified worker
`python -m pytest` must use the same runtime rather than an unrelated machine
Python with incompatible bytecode magic. A worker-side test/scaffolding failure
is retained as evidence and never causes a completed stream to replay; the
independent harness acceptance command remains the authority on whether that
worker's owned change merges.

Launch the long-running comparison root as a detached local process with
stdout/stderr retained in an external sibling driver directory; do not
pre-create a partial round root for logs. Poll its child records,
JSONL terminal events, broker log, and result files every 30 seconds. A client
or terminal timeout must never be used as the process lifetime: retain and
resume the unfinished arm from its artifacts instead of restarting completed
arms.

At round preparation, hash the parent, dispatcher, Terra-plan, packet,
collector, RRC, and ContextMesh bridge/broker source files into
`runner-source.json`. Check that snapshot immediately before every arm launch.
If it changes, retain the completed evidence and mark remaining arms degraded;
never combine the long-lived parent's imported code with newly edited code
imported by a detached dispatcher.

The report must show both mechanism layers: Terra reconstruction versus RRC
HIT/render, and raw owner/brief peers per real overlap. Historical digest-hit
metrics are supplemental only and cannot prove cross-process worker sharing.
It must additionally report worker and Terra marginal compute by stage and
classify every dependency use as `raw`, `unchanged_reuse`, `diff_refresh`, or
`invalidated_raw`. The cumulative suite total may add retained, disjoint
cohorts once each, but it must never re-count or replay a completed V23/V24
stream.

### Append-only measured expansion

The post-repair clean cohort uses strict per-file ContextMesh ownership. Each
owner performs `claim_source -> every required source chunk -> publish_file_brief`
for one overlap before it can claim another. Only after its owner entries are
published or unchanged-reused may it retrieve peer summaries. This preserves one
source owner, a complete peer brief, and no peer raw fallback.

The RuleForge catalog uses a stable reuse contract. Terra puts each DeepSeek worker's
exact `PolicyProfile` binding into that DeepSeek worker's plan; the catalog owner brief
contains only the reusable catalog API. That makes the brief complete for a
worker without copying new profile records into every later stage: once its
owner publishes it, all later unchanged stages reuse it rather than paying a
new catalog-owner read/publish tax.

At dispatch, the shared broker may attach an already validated, authorized
unchanged brief directly to that worker's sealed packet. This is still the
broker's one-owner brief, never a controller-made digest or raw file. A worker
with only such prefetched briefs makes no MCP call at all; a worker with a new
or changed overlap uses the strict per-file owner/peer route only for that remaining
overlap. This avoids spending multiple DeepSeek worker turns merely to fetch a summary
that the broker had already validated before the batch was launched.

The broker retains and validates all five brief fields. A sealed worker packet
receives only the exact worker-required fact projection, keyed by canonical
path. That projection must contain every import path, symbol, constructor or
call shape, return or collection shape, anchor, and failure semantic needed for
the current task stage. Audit-only metadata stays broker-side, but a fact needed
to implement or test the assigned change is never omitted merely to shorten the
brief.

A complete file brief is an executable interface contract, not a topic label.
For every source a peer must use, its required facts must state the importable
module path, public symbol names, exact constructor/call/key shape, return or
collection shape, and relevant failure semantics.  The broker validates those
literal facts against the owner publication.  A summary that says only
"registry stores rules" or "domain has Decision" is incomplete even when its
anchors match: it makes a worker guess and converts saved reads into test
repair loops.

When a retained staged round contains completed work plus a recovery-contaminated
segment, do not replay its completed tasks to improve the number. Append new,
stage-qualified RuleForge tasks to the same three arm baselines instead. The
next stage begins at the retained commit and contributes only its new Raw,
ContextMesh, and Full streams to the cumulative token ledger. It must use new
owned modules and named tests (for example, distinct operational policy
controls from the existing substantive catalog), not a duplicate task under a
new directory name.

Run a clean append-only segment first. Its operational catalog
profiles remain a natural four-worker overlap; its later two stages must reuse
the same complete catalog brief when the hash is unchanged. A failed or
replayed predecessor remains visible in gross historical usage but is excluded
from mechanism-promotion numerator/denominator. Only after this clean segment
shows the ContextMesh DeepSeek worker tier may additional new segments be appended until
Raw's cumulative, non-replayed usage reaches 2,000,000 tokens. RRC stage-plan
state must continue from the retained parent key, so later Full Terra plans are
source-free delta renders rather than a new reconstruction.

## Local proof and comparison acceptance

Before a paid arm, deterministic tests must prove:

1. Four independent clients use one broker while their read sets differ.
2. For every overlap: exactly one owner raw claim, exactly one valid publish,
   and brief-only peer service.
3. Two unrelated overlaps can progress at once; no global serial queue exists.
4. A unique file has no broker entry and remains local to its worker.
5. A malformed, late, or misbound brief cannot leak raw source to a peer.
6. A stream that directly reads a declared-source candidate outside its packet's
   allowed read set is invalid in every arm. This prevents Raw from learning an
   extra pattern that ContextMesh correctly denies.
7. The four distinct tasks complete in isolated output areas from their real
   packets and named acceptance commands.
8. Raw/ContextMesh Terra preflight and full Terra HIT/render evidence are
   retained and attributable.
9. The exact headless Codex MCP configuration registers the ContextMesh bridge,
   a no-provider stdio bridge health check exposes exactly
   `claim_source`, `read_source_chunk`, `publish_file_brief`, `get_file_brief`, and `get_file_briefs`, **and** the
   source-free DeepSeek worker eligibility probe retains an actual ContextMesh tool-call
   event before any of the four task DeepSeek workers can launch.
10. The frozen workload retains enough actual repeated source-body capacity to
    make the 15% worker gate plausible. This is a conservative byte-only
    preflight, never a claimed token saving: it records Raw direct-source bytes,
    one-read-each bytes, and eliminated duplicate bytes, and rejects a paid run
    below the configured minimum.
11. A large overlap can keep peers waiting through the configured publication
    deadline while its owner claims and publishes it, and no owner has more
    concurrent raw claims than one.
12. The generated policy catalog's retained `source_claim_raw` event proves a
    single `plan_scoped_excerpt` no larger than the configured owner-view cap,
    while retaining the full-file size and SHA-256. A full-file or multi-chunk
    catalog delivery is a failed ContextMesh efficiency guard, not proof.
13. A stage-2/3 unchanged request with the same workflow lineage, content hash,
    requirements, and schema is served from the prior brief. Its model stream
    contains neither a raw claim nor a direct source read.
14. A legitimate stage-boundary change selects one refresh owner, supplies that
    owner exactly one ancestor brief plus the recorded Git diff, and produces a
    new hash-bound brief. At least one later peer completes from that refreshed
    brief; no peer receives the diff or raw source.
15. A missing ancestor, unavailable/oversized diff, or changed required fact
    produces an explicit invalidation event and exactly one declared raw owner
    view; it never silently turns every later worker into a raw reader.
16. Full stage 2/3 retains a stage-plan HIT with the prior plan-state key and
    dependency revision tuple, renders distinct delta plans, and shows no
    Terra repository reconstruction read. A changed brief revision invalidates
    only the plan records that name its lineage.

A fresh Codex round passes only when it executes the same three linked stages
in all arms, all arms have equal stage acceptance outcomes
and valid topology evidence, with the mechanisms scored at the roles they
actually affect:

- ContextMesh must reduce **DeepSeek-worker marginal compute** (`input_new + output +
  reasoning`) versus Raw by at least 15%.
- RRCv2 must reduce **Terra marginal compute** by at least 15% versus the one
  raw Terra planner whose retained artifacts are paired to both Raw and
  ContextMesh.
- Full must reduce aggregate marginal compute by at least 15% versus Raw and
  remain below ContextMesh. The working target remains approximately 50%
  Full-versus-Raw; a nominal 4-5% win does not promote the provider lane.

Gross input (`input_tokens`) and cache fields remain in the report for
transparency, but cached base context is not treated as new mechanism compute.
Do not call a gross-token change a ContextMesh or RRC win without the matching
role-specific marginal reduction.

The report must show the mechanism reduction both for the complete staged
workflow and for stages 2+3. A worker saving claimed as persistent reuse is
valid only if it includes `unchanged_reuse` and/or `diff_refresh` events in a
later stage; a stage-1-only saving remains same-stage overlap evidence.

## Retained evidence and diagnosis

These artifacts remain evidence; never rerun them merely to repair scaffolding.

| Evidence | What it established | Why it cannot promote the gate |
| --- | --- | --- |
| `harness/artifacts/FULL-fix-codex-v2` | Terra generated real plans and Raw's four DeepSeek worker workers performed distinct task changes. The `full` arm retained `622,264` primary tokens and Raw retained `843,650`. | Invalid diagnostic only: ContextMesh's first dispatcher used a task-id plan filename the old parent resolver rejected; the next full DeepSeek worker sessions did not receive ContextMesh tools; Raw was assessed by a parent process whose imported resolver predated the on-disk repair. A later isolated DeepSeek worker probe confirmed the missing model-visible MCP tool surface. |
| `harness/artifacts/M14-codex-arm-comparison/.../M14-codex-r7` | Raw primary compute `574,784`, ContextMesh `862,954`, full `547,733`; all task gates passed. | Each Codex invocation launched its own stdio ContextMesh process, all workers cold-read an irrelevant probe, Terra only launched a script, and full bypassed exact RRC rendering. |
| `metrics/FULL-fix-codex-v13` | The source-view topology worked: Raw was valid at 109,561 DeepSeek worker marginal tokens and 25,434 Terra marginal tokens; ContextMesh's completed worker streams were 99,062 DeepSeek worker tokens (a directional 9.6% reduction). | ContextMesh could not publish the 359-byte `rules/base.py` brief because verbose peer field names consumed the tiny contract budget, causing `brief_missing`; the completion was invalid. The workload's roughly 112KB duplicate body mass also fell below the subsequently raised 240KB capacity floor. Full Terra was stopped before worker dispatch once that ContextMesh product-contract failure was confirmed. |
| `metrics/FULL-fix-codex-v14` | Raw was valid at 119,137 DeepSeek worker and 24,351 Terra marginal tokens. The directional signals were substantial: ContextMesh DeepSeek worker compute was 62,107 (47.9% lower) and full Terra compute was 5,592 (79.9% lower than ContextMesh Terra). | Not proof: the prior first-reader owner rule assigned DeepSeek worker 1 the catalog plus two other overlaps, while peers had a 60-second broker deadline. Only two of five briefs published; three workers visibly ended unavailable, so ContextMesh and full failed focused acceptance. The repair is balanced ownership, one-file claim/publish transactions, a ten-minute deadline, and product classification for a timeout. |
| `metrics/FULL-fix-codex-v15` | Raw was valid at 97,824 DeepSeek worker and 40,258 Terra marginal tokens. Balanced owners claimed all five natural overlaps, and the small owner transactions published successfully. | Retained product diagnostic, stopped before Full: owners naturally copied `schema_version`/binding fields from `claim_source` into `brief`, which the bridge rejected despite the five valid summaries. More importantly, the 134KB catalog overflowed the headless MCP response limit after its sole owner claim was locked. The repair normalizes only the five summary fields at the bridge and delivers a large source as strictly ordered owner-only chunks under one claim. |
| `metrics/FULL-fix-codex-v16` | All three arms were topology-valid. RRC reduced Terra marginal compute from 24,629 to 6,782 (72.5%), and full aggregate marginal compute was 28.7% below raw. | The required ContextMesh worker tier failed: ContextMesh DeepSeek worker marginal compute was 163,595 versus raw 123,257 (32.7% worse). Root cause was not duplicate raw reads: the frozen plans omitted the markets evaluator dependency, Terra was allowed to invent `PolicyService.normalize_payloads`, and owners could publish a five-field brief without the constructor/call facts a peer required. These errors caused worker repair/test loops. The repair makes the read set complete, carries literal verified source-contract anchors into Terra, and rejects both incomplete Terra plans and incomplete ContextMesh briefs. |
| `metrics/FULL-fix-codex-v17` | Raw completed validly and the full arm recorded an actual local EverOS/RRC lookup. | Diagnostic only: the paired ContextMesh path skipped its worker dispatcher, and the validator incorrectly treated a sorted RRC task-id list as invalid. Both harness defects were repaired before V18; V17 is retained and never replayed. |
| `metrics/FULL-fix-codex-v18` | All three arms were topology-valid with four direct DeepSeek worker/high/priority/fast workers, one arm-local broker, no peer raw fallback, and valid actual RRC lookup. RRC cut Terra marginal compute from 32,967 to 10,541 (68.0%); full aggregate marginal compute was 82,275 versus Raw's 122,421 (32.8% lower). | The hierarchy still fails: ContextMesh DeepSeek worker marginal compute was 135,048 versus Raw's 89,454 (51.0% worse), although full DeepSeek worker compute was 71,734. Retained JSONL identified the product defect: the 134 KB catalog granted a 113,505-character "summary" target and required nine 16 KB owner MCP turns, repeatedly recontextualizing the growing source. The repair makes both brief budget and owner source view plan-fact-bound; no new paid comparison is justified until its local proof is green. |
| `metrics/FULL-fix-codex-v19` | All arms had valid source topology; the catalog was a one-chunk 8,246-byte plan-scoped owner excerpt from a 133,762-byte file, and actual local EverOS/RRC cut Terra marginal compute from 23,692 to 5,513 (76.7%). | Not a hierarchy result. ContextMesh workers were still 26.9% above Raw. More critically, the Full RRC artifact included literal imports, a `register` function body, and a test recipe, lowering Full DeepSeek worker work for the wrong reason. The repair removes that prepared source body, freezes plan source facts to the worker contract, and rejects solution-body or worker-reread plan text before dispatch. |
| `metrics/FULL-fix-codex-v20` | All three arms were valid with the repaired RRC solution-body guard. RRC reduced Terra marginal compute from 25,879 to 11,560 (55.3%); Full workers were effectively equal to Raw (106,447 vs 106,593), confirming no remaining hidden pre-solved-worker advantage. | The hierarchy still fails: ContextMesh workers were 125,349 versus Raw 106,593 (17.6% worse), and Full total was only 10.9% below Raw. The retained plans gave workers the entire detailed API/profile fact set that the brief was supposed to replace, so raw source reads added little while ContextMesh added ownership/peer ceremony. The repair separates small Terra verification anchors from complete broker-validated brief facts; no paid rerun occurs until local proof covers that separation. |
| `metrics/FULL-fix-codex-v21` and `metrics/FULL-fix-codex-v22` | V21/V22 exposed harness recovery paths before a usable comparison: V22 restored four validated briefs, recovered the evaluator owner brief without a second raw read, and resumed the original DeepSeek worker sessions until all four focused task tests passed. | Never promote these numbers. A broad plan marker first caused pre-dispatch diagnostics; V22 then paid multiple typo/retry turns and an orphaned dispatcher leak. ContextMesh marginal compute is therefore inflated (302,176 vs Raw 110,204) and not a mechanism measurement. The repair accepts only a one-edit, uniquely authorized `brief_id` correction on every broker operation, rehydrates bound publications, resumes only affected session ids, and records/clears exact process trees. |
| `metrics/FULL-fix-codex-v23` | **Promoted Codex result.** All three arms completed four distinct DeepSeek worker tasks with equal focused-test outcomes, valid direct-headless topology, broker evidence, and complete retained streams. ContextMesh reduced DeepSeek worker marginal compute from `130,370` to `100,465` (22.9%). The local EverOS/RRC HIT reduced Terra marginal compute from `33,540` to `6,361` (81.0%). Full aggregate marginal compute was `114,936`, below Raw's `163,910` (29.9%) and ContextMesh's `134,005`. | This passes the formal 15% role-specific and aggregate promotion gates. It does **not** meet the non-binding working target of roughly 50% Full-versus-Raw, so it is retained as measured shipped proof rather than represented as a 50% result. |
| `metrics/FULL-fix-codex-v24-growth` | A new, non-replayed RuleForge cohort completed all three arms with four distinct tasks and valid topology. It retained Raw `148,491` worker marginal compute, ContextMesh `154,491`, and Full `159,880`; its broker recorded five owner claims, ten brief services, and two brief repairs. | V24 is a one-stage fan-out and is diagnostic only for persistent reuse. Provider-cached Raw reads plus ContextMesh prompt/tool/repair ceremony erased the expected worker saving. It contains no stage-2/3 consumer, no hash-bound unchanged reuse, no diff refresh, and no stage-plan delta HIT; do not use it as evidence against or for the new staged design, and do not replay it. |
| Provider R8 retained under `harness/artifacts/WP6-provider-validation` | A shared process topology can produce gate evidence. | ContextMesh and full parent prompts both contained RRC material, so it is not a ContextMesh-only baseline. |
| Historical demo rounds under `contextmesh/runs/demo-tui` | RRC-style planning reduced coordinator discovery reads substantially; cold-cache collisions and repair/ceremony turns were observable. | They use a superseded topology, packet contract, and provider flow. They are diagnostic only. |

The durable lessons folded from the former post-mortem are:

- Completion must be test-gated; a hard turn cap without a completion path
  creates expensive rescue/repair behavior.
- Prompt wording alone does not prevent unnecessary reads; the broker and source
  access policy must enforce the boundary.
- A verbatim solution packet makes arms incomparable. The headline arm uses a
  structural plan and file briefs, not prewritten output bodies.
- ContextMesh and RRC savings must be separately observable before they are
  aggregated.

## Out of scope

- Replaying degraded or rate-limited paid runs.
- Claude comparison work before the Codex gate passes.
- Ollama, Tollgate, or unmetered provider helpers.
- Extra workers, hidden summarizer agents, universal probe reads, or work that
  changes task difficulty between arms.
- Treating cache text savings computed from a manifest as billed model savings.

## Definition of done

The implementation is done when the shared broker, bridges, four-plan overlap
ledger, validated file-brief lineage, stage-boundary merge/diff refresh,
stage-aware RRC plan state, runner, collector, and report satisfy the local
proof above and produce a retained valid **three-stage** Codex result meeting
the three-level economy gate. The result must separately prove later-worker
unchanged reuse, one-owner diff refresh, and Terra stage-plan HITs; V23/V24
alone cannot satisfy this expanded definition. The workflow defines how to
reach and verify that state.
