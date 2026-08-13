# FULL: ContextMesh + RRCv2 Implementation Workflow

Status: authoritative description of **how to implement, validate, and measure**
the code described in [FULL-fix-implementation-plan.md](FULL-fix-implementation-plan.md).
That plan is the sole source for architecture, required data, product behavior,
and acceptance criteria.

## Operating objective

Build the smallest working system that demonstrates real compute savings on a
real four-worker task:

- Terra gives four external DeepSeek Flash workers four different plans, isolated worktrees/branches, and
  separate write ownership.
- Worker read sets differ and only naturally overlap in some files.
- ContextMesh serializes only an overlapping file's broker state; the four workers
  otherwise work concurrently.
- The same non-raw broker persists through three linked stages. Later workers
  consume a hash-matched brief, or one refresh owner updates it from the prior
  brief plus Git diff; peers never reread the raw file.
- RRCv2 eliminates Terra reconstruction on a valid plan-template HIT.
- RRCv2 also retrieves prior stage-plan state and renders later delta plans
  without new Terra repository reconstruction reads.
- Every token-producing process is retained and measured.

The code target is the plan's canonical topology. Do not revive the historical
per-worker stdio digest server, generic probe, detached Python-prepared worker
packet, or unmetered digest child.

## 1. Non-negotiable execution rules

1. Implement and validate locally with Codex before any Claude comparison.
2. Use the existing workspace and local EverOS service only. Do not reintroduce
   Tollgate or a provider API proxy. The measured DeepSeek workers use the
   shipped local Ollama provider profile directly; it is not an extra proxy.
3. The measured lane must not use a host collaboration/API subagent. Launch the
   one Terra coordinator and four DeepSeek workers only through their direct
   headless `codex exec` commands; retain those five JSONL streams. A generic
   auxiliary agent, including an interrupted one, is not a Terra/DeepSeek process
   and may not execute, meter, or validate an arm.
4. Preserve every artifact. A task-test, launcher, stream, handoff, scaffold, or
   orchestration failure is a degraded diagnostic, not an automatic restart.
   Resume unfinished work from its existing artifact directory where possible.
5. Only a confirmed product-code failure under `contextmesh/` or `rrc/` blocks
   the remaining arms. Diagnose it once, make the smallest repair, and continue.
6. The old R7/R8/provider fragments are retained evidence only. Never rerun them
   to repair topology or obtain a better number.
7. The only parallel project-agent deployment is the measured comparison: one
   Terra coordinator dispatching four DeepSeek workers in one batch. Outside a
   measured arm, the current Codex parent remains the orchestrator and every
   delegated coding, review, or test slice uses the external DeepSeek launcher
   in `.codex/scripts/Invoke-DeepSeekDelegate.ps1` (never a host collaboration
   subagent). Those ordinary work slices are explicitly
   parent-authorized and serial unless isolated worktrees make their ownership
   genuinely independent.
8. V23 and V24 are immutable retained evidence. Never rerun either to increase
   a token count. A new staged cohort may be added to a cumulative report once,
   under its own workflow and arm namespaces.

The Terra coordinator remains the current direct-Codex coordinator contract:

```powershell
codex exec --ignore-user-config --enable fast_mode --model gpt-5.6-terra `
  --config model_reasoning_effort=high --config model_auto_compact_token_limit=230000 `
  --config service_tier=priority `
  --dangerously-bypass-approvals-and-sandbox --json
```

Each measured worker is an external Codex DeepSeek Flash delegate. The checked-in
`.codex/delegates/deepseek.toml` and catalog bind
`deepseek-v4-flash:0731-cloud`, the Ollama provider, a 1,048,576-token context
window, and a 230,000-token automatic-compaction threshold. A fresh worker uses:

```powershell
codex exec --strict-config --ignore-user-config --model deepseek-v4-flash:0731-cloud `
  --config model_reasoning_effort="high" --config model_context_window=1048576 `
  --config model_auto_compact_token_limit=230000 `
  --config model_auto_compact_token_limit_scope="total" `
  --config model_catalog_json="<absolute repo>/.codex/delegates/deepseek-model-catalog.json" `
  --oss --local-provider ollama --dangerously-bypass-approvals-and-sandbox --json
```

An interrupted DeepSeek worker uses the same model/catalog/compaction/MCP
contract with `codex exec resume <retained-thread-id>`, but replaces the
startup-only `--oss --local-provider ollama` pair with
`--config model_provider="ollama"`. Each `arm/stage/worker-id` owns an
independent JSONL stream and retained thread id; four simultaneous workers must
never share a delegate pointer or resume one another's session. Terra High
remains the measured coordinator. The coordinator starts all four DeepSeek
commands in the same dispatch turn. Do not use Claude until the Codex economy
gate passes.

For every non-raw DeepSeek worker whose packet still contains an owner or peer broker route,
append the ContextMesh MCP contract to that same direct `codex exec` command:

```text
--config mcp_servers.contextmesh.command='python'
--config mcp_servers.contextmesh.args=['-m','contextmesh.mcp.bridge']
--config mcp_servers.contextmesh.cwd='<absolute repository root>'
--config mcp_servers.contextmesh.env_vars=['CONTEXTMESH_BROKER_HOST','CONTEXTMESH_BROKER_PORT','CONTEXTMESH_WORKER_ID']
--config mcp_servers.contextmesh.required=true
```

The `cwd` is required because headless Codex does not reliably pass the
dispatcher's `PYTHONPATH` into its MCP subprocess. Workers must discover
ContextMesh first: this Codex DeepSeek surface exposes the bridge directly as
`mcp__contextmesh__claim_source` and companion tools. Its first action is that
named MCP call, not resource listing or `ALL_TOOLS` discovery; an MCP-visible endpoint
error proves the surface is present. It may not reply text-first or fall back to
raw source.

## 2. Build order

Perform these stages in order. Finish each stage's local proof before beginning
the next; do not spend a paid comparison run to debug an unfinished stage.

### Stage A: Freeze the three-stage, four-task-per-stage workload

1. Select three linked RuleForge stages. Each stage has four genuinely
   independent tasks; stage 2 and stage 3 must require naturally overlapping
   files produced or consumed by an earlier stage, while preserving some unique
   local reads.
2. Give each task a unique stage-qualified id, branch/worktree, owned write paths, objective,
   focused acceptance command, and initial source-read set.
3. Verify output ownership does not overlap. Read sets may overlap naturally.
   Make each set complete for that task's implementation. The Raw arm is not
   allowed to discover extra source patterns outside this declared set, and the
   non-raw arms may directly read only their declared unique subset.
4. Materialize a stage manifest and worker-contract hash that are byte-identical for
   `raw`, `contextmesh`, and `full` except for the arm's access mechanism. Retain
   each Terra-produced or RRC-rendered plan for audit, then project every valid
   artifact onto the same frozen worker-facing ids, source facts, and generic
   execution steps before dispatch. A malformed or source-incomplete Terra
   artifact still rejects dispatch; the projection is not a static fallback.
5. Write a deterministic fixture with a mix of overlapping and unique reads,
   such as `{A,B,Catalog}`, `{A,B,C, Catalog}`, `{C,D}`, and
   `{A,C,D,E,Catalog}`. The catalog is a substantive versioned source of
   profiles required by workers 1, 2, and 4; it is not a universal common file.
6. Define the stage boundary: after all four focused tests pass, merge only the
   four disjoint owned-path changes into an arm-local baseline commit. Record
   its commit SHA, per-path hashes, and `git diff` from the prior stage. The
   next stage must start from that commit, never a newly materialized initial
   fixture.
7. Include at least one unchanged overlapping source requested in stage 2/3
   and at least one legitimate changed overlapping source whose diff changes a
   required brief fact. This creates both `unchanged_reuse` and `diff_refresh`
   evidence in a normal successful run.
8. Before a paid run, calculate the actual declared duplicate source-body bytes
   for every stage and cumulative total. A workload below the configured
   capacity floor is a local topology failure, not a reason to spend a paid
   round. This floor is a feasibility screen only, never a claimed token saving.

Output: frozen three-stage manifests, worker-plan fixtures, a recorded stage
transition contract, and deterministic assertions that each stage's four task
objectives/output paths differ while their arm-to-arm mappings do not.

### Stage B: Implement the overlap ledger and file-brief schema

1. Add typed records for worker plans and initial read sets.
2. Have Terra's planning path calculate the overlap ledger from those four sets.
3. Emit no ledger entry for a unique file.
4. For an overlap, assign one participating worker source owner by least current
   owner-load among eligible readers, breaking ties by worker id, and bind a
   `brief_id` to manifest hash, canonical path, plan steps, peers, and the
   requirements union. This distributes natural overlap work; it must not add
   a shared file or change any worker's task/read set.
5. Add immutable workflow lineage to every brief record: workflow/arm
   namespace, stage id, parent stage id, canonical path, branch lineage, stage
   commit, content hash, parent content hash, requirements hash, brief-facts
   hash, schema version, and refresh kind. A new revision appends to its
   lineage; it never mutates the previous brief.
6. Implement the plan-bound brief validator for exactly five **broker-held**
   compact strings: APIs/purpose, data/dependencies, behavior/failures,
   plan-step facts, and source symbol/fact locators (line ranges optional).
   The broker binds all source and plan metadata after validation and persists
   it for audit only. Owners submit named fields as a documented fixed-order
   array. For each sealed worker, the broker returns only that plan's literal
   required fact strings by canonical path, after checking them against the
   complete five-field brief; it never sends every field or another worker's
   facts merely because they share a source.

Local proof:

- Overlap calculation yields ledger entries only for real intersections.
- Changing a plan's read set or requirements invalidates its old `brief_id`.
- An exact later-stage path/hash/requirements/schema match resolves to the
  previous brief revision without opening the source body.
- A changed descendant resolves to one designated refresh owner and retains a
  predecessor link; another arm or branch with the same path cannot resolve it.
- A brief missing a required compact field, a source locator, a plan-step fact,
  or the fact-derived **returned peer-envelope** byte budget is rejected as
  `brief_incomplete`.

### Stage C: Implement the arm-local shared broker

1. Start one long-lived broker for a non-raw arm before launching workers.
2. Implement `claim_source`, `read_source_chunk`, `publish_file_brief`,
   `get_file_brief`, and `get_file_briefs` exactly as defined by the
   implementation plan. An owner must finish one source transaction
   (`claim -> chunks -> publish/one repair`) before it can claim another;
   peers retrieve only published briefs.
3. Make the state transition atomic per `brief_id`:

   ```text
   unclaimed -> owner_raw -> brief_published -> peers_served
   ```

   For a later-stage revision, add only these transitions:

   ```text
   exact predecessor -> unchanged_reuse -> peers_served
   changed predecessor -> refresh_owner_diff -> brief_published -> peers_served
   invalidated predecessor -> owner_raw -> brief_published -> peers_served
   ```

4. Return raw source only to a declared first-revision or invalidation owner. A
   changed descendant with a valid predecessor instead gives its one declared
   refresh owner the predecessor's five-field brief, prior/current hashes, and
   bounded `git diff --no-ext-diff <prior-commit>..<current-commit> -- <path>`.
   The refresh owner publishes the next five-field revision; the diff itself is
   never served to a peer. For a raw source, first form
   a single plan-scoped raw excerpt containing the module header, tail helpers,
   and every raw record selected by the literal required facts. Use it only when
   every selector is present; otherwise fall back to the full file. The full
   source SHA-256 remains the binding identity. `claim_source` returns that
   owner view and its total chunk count; `read_source_chunk` returns remaining
   chunks in strict order only for the conservative fallback. Do not use a
   filesystem peer fallback or hidden summarizer.
5. Return a small `brief_template` contract with the first raw chunk: exactly five
   required summary headings, a maximum peer-visible byte budget derived from
   the literal required facts (never from raw source size), a smaller target
   character count, and a source-only authoring
   rule plus the union of literal source-fact contracts required by its participants.
   Each contract names a needed API, data shape, configuration key, result field,
   or error condition and must occur in the published five fields; it is not peer plan prose. Do not copy a peer's plan,
   paths, test matrix, or command into the brief: that peer already receives
   its own distinct plan packet. The owner publishes only the five concise
   source-fact strings. The bridge strips copied claim metadata and the broker
   attaches source/binding metadata for audit; peers receive none of that metadata. A validation error retains the owner
   claim for one repair; it never authorizes a second raw claim. Do not invoke a
   separate model or a background digest child.
   The worker's overlap packet contains only broker-routing identity, not the
   ledger's peer plan steps; retain the full ledger solely as an audit artifact.
6. Make peers wait for a valid brief for ten minutes, within the fifteen-minute
   arm limit, so a large owner-only source can be read and distilled. An
   unrepaired owner deadline produces `brief_missing`, stops the dependent step
   visibly, never returns raw source, and is a ContextMesh product failure that
   prevents the next paid arm.
7. Persist or recover broker state only by its workflow/arm namespace, lineage,
   stage commit, bound manifest/brief identity, and content/requirements hash.
   Keep it alive across all three stages of one arm; clear stale state only
   between fresh arm roots. On a harness-only resume, rehydrate only an exactly
   bound, already validated publication and serve it without rereading raw
   source. An unknown owner or
   peer `brief_id` may resolve for claim, chunk, publish, or retrieval only
   when it is one edit from exactly one currently authorized ledger entry;
   return/use its canonical id and reject ambiguity.

Local proof:

- Four independent clients connect to one broker.
- For each overlap, exactly one raw owner claim and exactly one successful brief
  publication occur; a large source may have ordered owner-only chunk-delivery
  events, but never a second claim.
- The generated policy catalog records a single `plan_scoped_excerpt` owner
  view within the configured cap, plus the full-file SHA-256 and size. A
  multi-chunk or full-file catalog claim is retained as failed efficiency
  evidence rather than promoted as a valid ContextMesh comparison.
- A peer receives a brief only; it cannot acquire raw source after waiting.
- An unchanged stage-2/3 request is served from its exact prior revision with
  no `source_claim_raw` event.
- A changed stage boundary supplies exactly one refresh owner a predecessor
  brief and recorded Git diff, then serves the new brief to another worker; no
  peer receives the diff or raw body.
- Missing predecessor, unavailable/oversized diff, or changed required-fact
  coverage yields one explicit invalidation and one declared raw owner, not
  peer fallback.
- Two unrelated overlaps advance concurrently, proving the broker is not a
  global serial queue.
- An owner that also needs a peer brief completes each owned file as
  `claim -> every required source chunk -> publish -> one repair if required` before it claims another or
  waits for a peer, so a downstream peer cannot time out behind a batch claim.
- A unique file produces no broker event.

### Stage D: Build the Codex MCP bridges and source-read enforcement

1. Configure every `contextmesh`/`full` worker command with a local stdio bridge
   that forwards to the persistent broker selected for its arm. The endpoint
   and workflow/arm namespace stay fixed through stage 1, 2, and 3; only the
   stage id and path revision binding change.
2. Negotiate the bridge `initialize` protocol version from the requesting Codex
   client; do not hard-code an old MCP revision.
3. Prove bridge process ids may differ while their broker id and `brief_id`
   state are identical.
4. Process independent bridge JSON-RPC requests concurrently and serialize only
   stdout writes. Prove that a waiting peer read cannot block the same DeepSeek worker's
   later owner publication; this is a product deadlock, not a test retry.
5. Run exactly one source-free DeepSeek/ollama/full-access eligibility probe for
   the exact CLI version, model, flags, and bridge config. It calls the named
   ContextMesh tool with a deliberately invalid id. Admission requires a real
   tool-call record in the DeepSeek JSONL, not merely a successful `codex mcp get`
   or a direct bridge `tools/list`. Retain this probe separately from measured
   task-worker compute. If it fails, retain its stream and do not launch any
   non-raw task workers.
6. Omit every relevant overlap path from a non-raw packet's
   `local_read_paths`; allow normal direct reads only for those remaining
   unique paths. Later-stage unchanged consumers retrieve the retained brief;
   changed peers wait for the refresh owner. A failed retrieval ends the task
   visibly and never falls back to a raw source command.
7. Split each non-raw packet into `contextmesh_owner_overlaps` and
   `contextmesh_peer_overlaps`. The worker must complete one owner entry as
   `claim -> every required source chunk -> publish -> one repair if required` before claiming its next owner
   entry, and publish every owner entry before making any peer retrieval. This
   is an execution rule, not merely a suggested plan order.
7. Enforce the overlap policy in tools and stream validation: peer attempts to
   use `Read`, `Glob`, `Grep`, `Get-Content`, `cat`, `type`, `sed`, or an
   equivalent shell read on an overlapping source become invalid evidence.
8. Record owner/peer ids, workflow/lineage/stage ids, stage commit,
   predecessor/current hashes, diff size, refresh kind, waits, publication,
   service, and all missing/incomplete events.
9. Put the harness virtualenv `Scripts` directory first on every worker process
   `PATH`. This makes the worker's literal `python -m pytest` use the fixture
   compiler runtime; otherwise sealed `.pyc` fixtures can fail collection
   under an unrelated machine Python before product code is exercised.
10. For an unchanged sealed overlap, attach only the broker-validated literal
    facts required by that worker's distinct plan, grouped by canonical path.
    Do not attach a full brief, opaque brief ids, another worker's facts, or
    validation-only anchors to the packet, and do not expose an MCP bridge.
    Retain all five fields in broker state for audit and source-fact
    validation.

Local proof:

- A direct peer raw-read attempt is rejected or marked invalid.
- An owner can continue its own task after publication.
- A peer can continue its dependent step from the brief without source access.
- The raw arm has no broker and remains a fair direct-read baseline.
- A current headless DeepSeek JSONL contains the expected ContextMesh tool call;
  a passing bridge subprocess alone does not promote this check.

### Stage E: Wire RRCv2 into Terra's real planning path

1. Remove the legacy Python-prepared generic packet from the measured route.
2. In `raw`, make Terra perform repository/spec preflight and construct four
   retained coordinator-plan artifacts. Dispatch those exact artifacts to Raw's
   workers, then dispatch the **same files and delivery-plan SHA-256** to the
   ContextMesh workers. ContextMesh launches no second Terra model process: a
   separately generated raw plan would confound worker-source-sharing savings
   with plan-quality variance. Attribute Raw's retained Terra usage as the
   logical raw-planner cost for both workflow totals, while retaining one
   physical stream only once.
3. Before measured `full` Terra begins, run the retained local `warm_rrc_cache`
   preflight. It writes the generic template to the local SQLite store and
   parks an unflushed assistant-role EverOS buffer keyed by the stable case
   shape. The buffer holds only the case shape and opaque template reference;
   it uses EverOS's `filters.session_id` read-your-write path, so it makes no
   extraction/provider-model call. This setup stage is not a Terra model turn.
4. In `full`, call the RRC lookup only. It must require the retained warm record
   and SQLite entry, and must not index or populate a cache during the measured
   Terra turn. On a valid HIT, retain the lookup event, skip reconstruction,
   render its concrete plan with all template fields resolved, then deliver that
   exact rendered plan and emit the same overlap topology. A cached plan may
   contain ordered task steps and the frozen source-fact anchors needed to
   replace Terra reconstruction; it must never contain an import, code snippet,
   function/class body, prewritten test, `MODULE PATTERN`, `TEST PATTERN`, or
   other worker solution body. It also may not tell DeepSeek workers to reread or directly
   inspect source: only the arm packet decides Raw direct reads versus
   ContextMesh claim/brief access. Verify that the retained artifact covers
   every anchor, then bind delivery back to the exact frozen anchor list so
   incidental planner observations cannot make a brief redundant. Reject either
   condition before DeepSeek worker launch,
   so RRC can be credited only with Terra savings and not a cheaper pre-solved
   worker handoff.
5. After a successful stage dispatch, persist stage-plan state outside the
   measured Terra turn: workflow/arm namespace, stage and parent-stage ids,
   template version, four distinct task bindings, delivery-plan hash,
   plan-anchor hash, and ordered ContextMesh dependency revisions
   `(lineage, content hash, brief-facts hash)`. Store no raw source, full brief,
   code, or worker solution body.
6. For Full stage 2 and 3, use `lookup_stage_plan` with the prior stage-plan
   key, current task shape, template version, and dependency-revision tuple.
   A HIT renders a delta plan from retained plan structure and stage bindings
   without Terra repository reconstruction reads. Unchanged brief revisions
   retain their dependency key; a refreshed hash invalidates only plans that
   cite that lineage. Terra receives only plan state and the frozen
   task/anchor delta, never raw files or ContextMesh brief bodies.
7. On a MISS, failed cache preflight, unresolved dependency revision, or
   cross-arm/workflow cache key, retain the diagnostic and do not launch
   paid Full-arm Terra/DeepSeek workers or mislabel the round as an RRC HIT saving.
8. Retain Terra stream events separately from DeepSeek worker/broker events.
9. The frozen task manifest must contain no rendered plan or worker packet, but
   it does contain literal source-fact verification anchors for each declared
   read. Raw Terra must confirm every anchor during raw preflight; ContextMesh
   reuses the resulting verified artifacts,
   and dispatch rejects an artifact that omits one. A plan-missing, malformed,
   or source-contract-incomplete dispatcher error ends the retained arm promptly;
   it never launches DeepSeek workers with a fallback packet.
10. Store distinct **plan anchors** and **brief facts** for each declared source.
   Terra retains only the small literal anchors in a coordinator plan, proving
   the plan source binding without reciting the source. The broker validates the
   complete task-relevant fact set in the five-field brief. Raw DeepSeek workers obtain
   that detail from their allowed files; ContextMesh peers obtain it only from
   the owner brief. A coordinator plan that carries the complete brief facts
   invalidates the worker-economy measurement because it makes source sharing
   redundant.

Local proof:

- Raw shows Terra preflight reads; ContextMesh records Raw as its inherited
  plan/usage source and matches Raw's delivery-plan SHA-256.
- Full HIT shows an earlier retained local cache-warm record, a measured
  lookup-only Terra event with no reconstruction reads, and four resolved
  packets.
- Full stage 2/3 shows a retained stage-plan HIT, exact prior-stage key and
  dependency revisions, four distinct resolved delta plans, and no Terra
  repository reconstruction read. A changed brief hash invalidates only its
  dependent plan record.
- Every arm records the actual delivery-plan SHA-256 before its DeepSeek workers launch,
  plus the same frozen worker-contract SHA-256 across all arms.
- No packet contains unresolved placeholders or detached slot values.
- All four workers still receive different tasks and output ownership.
- Full plan source facts exactly equal the frozen source-fact contract; no RRC
  plan includes a solution-body or worker-reread marker.
- Full and Raw plan facts equal the frozen **plan-anchor** contract, while each
  ContextMesh brief covers the corresponding complete **brief-fact** contract.

### Stage F: Replace the suspended comparison runner and report

1. Keep the legacy `harness/codex_compare.py` execution guard until Stages A-E
   are green.
2. Replace its per-invocation ContextMesh setup with broker startup plus bridge
   configuration for `contextmesh` and `full` only. Start one broker per
   non-raw arm at stage 1 and retain it through the stage-3 report; it may be
   rehydrated only from exact bound lineage state after a harness interruption.
   At each accepted boundary, advance that same broker through its
   harness-only, token-protected `install_stage` operation with the next
   immutable ledger and exact stage/parent commits. The control token stays in
   the detached harness process, never in a worker prompt, worker environment,
   or MCP bridge configuration. A parent mismatch, duplicate stage ledger, or
   unpublished prior brief is retained as a stage diagnostic; it never causes
   a fresh broker or a peer raw fallback.
3. Have the real Terra coordinator produce planning/dispatch events at every
   stage, then launch that stage's four DeepSeek worker processes together in one
   dispatch batch. Apply accepted disjoint owned-path changes, record the
   stage commit/hash inventory/diff, and only then prepare the next stage.
4. Retain, per arm and stage: manifest and overlap ledger, stage baseline and
   boundary commit, path-hash inventory and Git diff, Terra stream/final
   handoff, four DeepSeek streams/final handoffs, broker/bridge logs, usage, wall
   clock, child records, test gates, and focused acceptance output.
5. Extend the report to separately show Terra tokens, owner-worker tokens,
   peer-worker tokens, gross token context, cache fields, and aggregate
   marginal compute:

   ```text
   input_new_tokens + output_tokens + reasoning_output_tokens
   ```

   Show those values per stage, for later stages combined, and for the complete
   staged workflow. ContextMesh is accepted only by its DeepSeek-worker marginal reduction; RRCv2 is
   accepted only by its Terra marginal reduction. Require at least 15% for each
   mechanism and 15% Full-versus-Raw aggregate marginal reduction (working
   target approximately 50%). Gross `input_tokens` is reported separately and
   never substitutes for either mechanism score.

6. Make the report reject promotion for unequal stage/task outcomes,
   plan/read-set drift, missing Terra evidence, missing broker events, missing
   unchanged-reuse or diff-refresh evidence, peer raw source access, uncounted
   model work, or incomplete usage streams.
   The visible evidence gate must separately prove exact Raw/ContextMesh plan
   pairing, manifest/read-set parity, valid shared MCP admission, per-overlap
   publication/reuse/diff-refresh events, strict Full HIT bindings, and
   accepted worker-ID sets; percentages alone never promote a cohort.
7. Classify every broker `brief_missing` timeout as a ContextMesh product
   failure. Retain the broken arm and do not launch a later paid arm from that
   known-bad protocol state.
8. At preparation, write `runner-source.json` with hashes of the runner,
   dispatcher, plan renderer, packet code, collector, RRC runtime, and bridge/
   broker code. Recheck it immediately before each arm. A mismatch retains the
   current artifacts and prevents a later arm from mixing old parent imports
   with a newer detached-dispatcher import. Include stage-merge/lineage code in
   this snapshot.
9. Start the long-running comparison root as a detached local process with
   driver stdout/stderr in a sibling directory until preparation has created
   the round root; never pre-create an incomplete round root merely to hold
   driver logs. Poll the retained broker, child, stream, and
   result artifacts every 30 seconds. Never let a short controlling-terminal
   timeout end the run or trigger a restart; resume only an unfinished arm from
   the retained artifacts. If no worker started, run only the retained dispatcher.
   If some workers stopped, restart the broker from bound persisted briefs and run
   `codex exec resume <retained-thread-id>` with
   `--config model_provider="ollama"` only for unfinished/explicitly
   unavailable DeepSeek sessions, concurrently. Never replay Terra or a completed worker;
   append resumed JSONL to its original session stream so all paid compute stays
   in the measurement. When every brief owned by a resumed DeepSeek worker is already
   restored, instruct it to skip claim/chunk/publish and continue only from its
   peer briefs and retained worktree. A resume at a stage boundary restores the
   exact broker lineage and stage-plan state before launching only that stage's
   unfinished workers.

   Use the retained driver rather than a terminal-bound `--run` command:

   ```powershell
uv run --project .codex/dev --locked python -m harness.staged_driver launch <fresh-id> --from-stage stage-35
   ```

   It writes `metrics/<fresh-id>-staged-driver/{launch.json,controller.stdout.log,
   controller.stderr.log,monitor.jsonl}` without pre-creating the cohort root.
   Its detached controller polls only retained broker, child-attempt, stream,
   stage-result, progress, and report artifacts every 30 seconds; it does not
   restart, terminate, or reinterpret any measured session.

   `--from-stage <independent-stage>` is always a **separate append-only
   cohort**. Preparation materializes a new baseline from that stage's fixture,
   writes `cohort.json` with `fresh_independent_baseline`, and accepts only its
   own later stages. It never reuses a predecessor arm baseline, broker state,
   boundary commit, or RRC stage-plan state. A round whose retained
   `cohort.json` disagrees with the requested `--from-stage` fails closed.

Local proof:

- Fixture streams cover a valid three-arm result and every invalid condition.
- The report attributes ContextMesh worker savings and RRC Terra savings
  separately before calculating the aggregate tier ordering, and labels every
  later-stage dependency as raw, unchanged reuse, diff refresh, or invalidated
  raw.
- Legacy R7-shaped evidence is marked diagnostic-only, never `proved`.

### Stage G: Prove staged reuse locally before paid Codex work

Run the retained no-provider gate once into a fresh proof directory:

```powershell
uv run --project .codex/dev --locked python -m harness.staged_local_proof --output metrics/local-staged-proof/<fresh-id>
```

It must finish before any direct `codex exec` cohort begins. The retained
`report.json` is the evidence bundle reviewed by the pre-compute gate.

Prepare, but do not spend, the direct cohort with:

```powershell
uv run --project .codex/dev --locked python -m harness.staged_codex <fresh-id> --from-stage stage-35
```

This writes the three arms × three stages plan. Every stage reserves one
`gpt-5.6-terra` High/priority/bypass coordinator command and four concurrent
external `deepseek-v4-flash:0731-cloud` Ollama/full-access worker commands with
the 230,000-token auto-compaction threshold. `contextmesh` and `full`
receive only their one arm-local broker control state; Raw receives none.

1. Run a no-provider, three-stage RuleForge fixture using the same persistent
   broker process and a real temporary Git repository. Stage 1 publishes the
   overlap briefs; stage 2 requests one unchanged overlap and changes another
   through an accepted owned-path commit; stage 3 consumes both resulting
   revisions.
2. Assert exact unchanged reuse: a later worker receives the earlier
   five-field brief, and the event/stream has no raw claim or source-body
   access for that path.
3. Assert exact diff refresh: one refresh owner receives only the prior brief
   and `git diff`, publishes the next hash-bound brief, and a peer consumes
   only that replacement. Exercise the explicit invalidation path separately
   for missing predecessor or inadequate diff; assert it produces one raw
   owner and never a peer fallback.
4. Run the local RRC runtime through stage 1, persist stage-plan state, then
   prove stage-2 and stage-3 `lookup_stage_plan` HITs render distinct resolved
   delta plans with no reconstruction read. Change one brief revision and
   assert only the dependent cache key is invalidated.
5. Run the same tests through the comparison collector and assert its report
   separates stage-1 overlap from later `unchanged_reuse`/`diff_refresh`, and
   preserves V23/V24 as read-only prior evidence rather than stream inputs.

Output: a retained local proof bundle with broker lineage, commits/diffs,
stage-plan keys, exact event assertions, and a source snapshot. Do not launch a
paid three-arm staged cohort until this bundle is green.

## 3. Test and review cadence

After every code stage:

1. Run the focused unit/integration tests for the changed module.
2. Run the repository gate from the root:

   ```powershell
   uv run --project .codex/dev --locked python .codex/scripts/verify.py
   ```

3. Retain command output and the changed-file list in the stage handoff.
4. Review only the current stage against the implementation plan. Do not expand
   scope with cosmetic refactors or provider experimentation.

A stage handoff contains:

```json
{
  "stage": "A-G",
  "status": "complete|degraded|blocking_product_error",
  "changed_paths": ["..."],
  "tests": [{"command": "...", "result": "..."}],
  "evidence_paths": ["..."],
  "remaining_risk": "..."
}
```

## 4. Pre-compute topology gate

Before a paid Codex comparison, review one local proof bundle and require all
of the following:

- four distinct task plans and isolated output ownership;
- stable manifest/read-set mapping across arms;
- one broker per ContextMesh/full arm and all four bridges connected to it;
- one owner raw claim, one complete compact publication, owner-first ordering,
  and brief-only peer service for every real overlap;
- one retained broker lineage per non-raw arm across all three stages, with an
  exact later-stage unchanged brief reuse and one-owner Git-diff refresh;
- one recorded stage commit and path-hash inventory at each boundary, with no
  state lookup across arms or workflow namespaces;
- no broker state for unique files and no global serialization;
- no direct peer raw-source access;
- no direct declared-source-candidate read outside a worker's packet scope in
  any arm;
- raw/ContextMesh Terra preflight evidence and full Terra HIT/render evidence;
- full stage-2/3 RRC stage-plan HIT/render evidence with no reconstruction read
  and cache invalidation limited to changed dependency lineages;
- a retained source-mass artifact passes the duplicate-source capacity screen;
- no uncounted digest child or provider call;
- the exact headless MCP config and a no-provider stdio bridge health check
  expose all five ContextMesh tools before workers are started **and** the
  separately retained source-free DeepSeek worker probe contains an actual ContextMesh
  tool-call record;
- the runner-source snapshot still matches the executing code; and
- every stream is complete, JSONL-valid, and free of sparse/NUL corruption;
  an attempt record means its paths are immutable and never a restart target;
- passing focused tests plus `VERIFY: PASS`.

Failure here is a local implementation result. Repair it before spending a new
comparison run.

## 5. Codex three-arm staged comparison

Only after the topology gate passes:

1. Create a fresh staged-cohort directory and record the rotated arm order.
2. Run the same frozen **three-stage** RuleForge workload as `raw`,
   `contextmesh`, and `full`. Do not rerun V23 or V24; their streams remain
   immutable prior evidence.
3. For each arm, start a fresh stage-1 target/worktree. For `contextmesh` and
   `full`, start one arm-local broker before stage 1 and retain its namespace,
   ledger, and stage-plan state through stage 3.
4. At every stage, keep the four DeepSeek workers concurrent. Per-file waits are allowed
   only when a peer reaches its own dependent overlap. After all stage tests
   pass, merge only accepted disjoint owned paths, commit the new baseline,
   retain hashes/diff, then dispatch the next stage from that baseline.
5. Raw workers reread their declared files at later stages. Non-raw later
   workers receive exact existing briefs for unchanged hashes; a changed file
   has one refresh owner using prior brief plus Git diff, while peers wait for
   its refreshed brief. No stage may use a peer raw fallback.
6. In Full, require stage-plan HITs for later stages and render four distinct
   delta plans before each corresponding DeepSeek worker batch. Do not allow a measured
   raw Terra reconstruction fallback.
7. Run each task's named acceptance command and retain all streams regardless of
   success, timeout, or launcher failure.
8. Collect usage and topology evidence without replaying a completed arm or
   stage. Add a new cohort to cumulative counts once only after its report is
   valid; never count a historical stream again.

For post-repair ContextMesh stages, each DeepSeek worker processes its distinct owner IDs
one at a time. Only owners receive bounded raw/diff inputs and author a
five-field summary, and each owner publishes before claiming another file.
After every owner transaction, the broker seals each peer packet with only its
required fact projection.

For the RuleForge catalog, pair adjacent stages under one identical compact
source-fact contract. Terra includes the exact profile binding in each DeepSeek worker's
different plan, while the source owner publishes only the catalog API and the
pair's record anchors. The following stage must receive that validated brief;
it must not re-open the catalog merely because it has a different task.
The reuse hash binds only source path, schema, and literal source facts; do not
put a stage manifest or task-plan field in it, or the following stage would be
forced to republish the same source brief.

Before launching a DeepSeek worker batch, ask the single shared broker for each worker's
already-published authorized fact projection and seal only those facts into
its packet. The worker reads that projection directly and makes no
model-visible MCP call for it. Only unready owner/peer entries stay in the
lists for a minimal tool call: peer-only workers use one brief retrieval; an
owner uses the strict per-file claim/publish transaction. This is not a
fallback: no
raw source, full summary, or another worker's facts enter a peer packet.

Before a paid cohort, make the file-brief required-fact contract implementation
complete. Each shared-source brief must identify the importable module path,
public symbols, exact constructor/call/key shape, returned value or collection
shape, and failure behavior a consuming worker needs. Encode those as literal
owner-required facts and reject a generic topical summary even if it mentions
the right file. Otherwise a worker replaces a source read with trial imports
and repeated focused-test repairs, which defeats the economy objective.

If an in-progress staged round has a recovery-contaminated segment, append a
new linked RuleForge segment to its retained arm baselines rather than creating
a disguised replay. It must contain at least one fresh-source producer stage
and a later unchanged consumer stage. Every appended DeepSeek worker owns a new module
and named test, while its read topology remains naturally overlapping. Treat
the first clean appended segment as the ContextMesh/RRC diagnostic denominator;
leave the contaminated historical spend visible but out of the promotion
comparison. The appended stages continue the same broker lineage and Full RRC
stage-plan key. Continue appending distinct segments only after that segment reaches the
ContextMesh worker tier, until cumulative non-replayed Raw usage reaches 2M.

The report can promote only if every arm reaches equal acceptance outcomes and:

```text
ContextMesh worker marginal compute < Raw worker marginal compute
Full Terra marginal compute         < Raw Terra marginal compute
Full aggregate marginal compute     < ContextMesh aggregate marginal compute
Full aggregate marginal compute     materially below Raw aggregate marginal compute
```

The working objective is approximately 50% full-versus-raw reduction. A small
strict result such as R7's 4.71% does not pass. Persistent ContextMesh savings
must be visible in stage 2/3 `unchanged_reuse`/`diff_refresh` usage, and RRC
savings must be visible in stage-2/3 Terra plan-HIT usage. If the ordering
fails, use retained evidence to identify whether Terra/RRC, worker/ContextMesh,
prompt shape, or task topology caused it; repair the responsible product code
or runner and use a fresh **new** cohort only after the local gate is green.

## 6. Progress-first failure handling

| Situation | Required response |
| --- | --- |
| Test, scaffold, launcher, handoff, stream, or orchestration error | Preserve artifacts, mark degraded, continue unfinished work when possible; no automatic restart. |
| Partial DeepSeek batch interrupted by harness/tooling | Rehydrate only already validated bound briefs, continue only the affected retained `codex exec resume` session ids with `model_provider="ollama"`, and retain completed workers untouched. Count prior and resumed turns together; never replay Terra or raw-owner reads. |
| Broker brief missing because the publication deadline expired | Preserve the dependent worker's result and broker state; no peer raw fallback or replay. Classify it as a ContextMesh product failure and stop later paid arms. |
| Broker brief incomplete but repairable | Preserve the owner claim and let the one same-source repair proceed; never authorize another raw claim or a peer fallback. |
| Stage-boundary predecessor/diff is unavailable or cannot cover required facts | Retain the lineage diagnostic, classify the revision `invalidated_raw`, and let only its declared owner receive the minimal raw view. Peers wait for the new brief; do not restart prior stages or create peer raw reads. |
| Full stage-plan lookup misses or references an unvalidated brief revision | Retain the exact cache key and dependency diagnostic; do not launch that Full-stage Terra/DeepSeek worker batch as an RRC HIT or silently reconstruct raw source. |
| Headless DeepSeek worker cannot see ContextMesh tools | Preserve the eligibility-probe stream, do not launch non-raw task workers, and treat it as a Codex integration diagnostic rather than a ContextMesh/RRC product-code failure. Raw may continue if its source snapshot is intact. |
| External provider rate limit/session limit | Preserve evidence and stop that provider lane. It does not block Codex implementation. |
| Confirmed `contextmesh/` or `rrc/` implementation error | Stop remaining paid arms, repair serially, run local tests, then resume only unfinished work or start a fresh round if comparability requires it. |
| Economy gate fails with valid evidence | Diagnose once from retained streams; do not call it a win, invoke Claude, or loop reruns. |

## 7. Provider guard

Claude is a later provider validation only. It remains disabled until a retained
Codex result satisfies the entire topology and economy gate. When it is
eventually attempted, translate the same four-plan, per-overlap broker contract
and preserve separate provider evidence; do not use historical provider results
as proof of the Codex design.

## Completion

This workflow completes when the implementation plan's definition of done is
met: a local-proven broker/RRC/runner system has produced a valid, retained,
three-stage, three-level Codex comparison result. It must prove both later
worker brief reuse/diff refresh and later Terra stage-plan HITs. Until then,
the next action is always the smallest unfinished stage above, not a new
provider experiment or a rerun of historical evidence.

### Verified completion: `FULL-fix-codex-v23`

The retained V23 direct-headless Codex run met the **previous one-stage**
condition. All three arms
completed the same four distinct DeepSeek worker tasks with valid topology and focused
acceptance evidence. ContextMesh lowered DeepSeek worker marginal compute from `130,370`
to `100,465` (22.9%); the local RRC HIT lowered Terra marginal compute from
`33,540` to `6,361` (81.0%); and Full aggregate marginal compute was `114,936`
versus Raw's `163,910` (29.9% lower, and below ContextMesh's `134,005`). The
formal 15% gates pass. The approximately 50% Full-versus-Raw objective remains
a working target, not a claimed V23 result. It does not meet the expanded
three-stage completion condition because it has no later-stage reuse, diff
refresh, or stage-plan delta evidence.
