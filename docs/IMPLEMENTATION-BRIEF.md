# ContextMesh — Implementation Brief (post source-dive)

Date: 2026-08-07 · Baseline: opencode v1.18.15 (`opencode-snowflake-hack`), EverOS v1.1.x, Snowflake
Verdict: **build it — the thesis survives contact with the source.** Two spec assumptions are wrong (EverOS storage model, LSP availability) and both have clean workarounds. Several things are *easier* than the spec assumed.

---

## 1. Assessment of the proposed solution

**What's strong.** The core claim — sibling subagents re-read and re-pay for the same files, and prefix caching can't help them — is *verified true in this codebase*: `task.ts` spawns child sessions that receive only a system prompt + the task string. No parent messages, no file context, no manifest (`packages/opencode/src/tool/task.ts:200-214`, `session/prompt.ts:157-191`). Each subagent explores from scratch. Fan-out is parallel and width-unbounded (tool dispatch is delegated to the AI SDK; each task forks its own background job). The experimental design (A0/A/B, B-vs-A headline, success-with-cost reporting, netting out summarizer cost) is exactly what makes this defensible to a judge who has built agents. Keep all of it.

**What's wrong in the spec.**
1. **G2 fails as written.** EverOS has no keyed write / exact-key read. Everything enters via `/api/v2/memory/add` (conversation-message shaped, buffered per `session_id`), passes through *LLM extraction* that rewrites content into "episodes," and lands in an eventually-consistent index (sub-second typical, ~10–15 s under load). A digest stored naively would come back paraphrased, late, or both. **Workaround exists and is legitimate** — see §3.
2. **M2's LSP assumption is mostly wrong.** Plugins cannot reach the LSP subsystem; over HTTP only `GET /find/symbol` (workspace symbols, capped 10/client) and `GET /lsp` (status) exist. No references/call-hierarchy from plugin land. M2 must be redesigned around EverOS-held repo maps, not "OpenCode's symbol graph."
3. **G3's real risk isn't capability, it's inclination.** Nothing prevents 4+ subagents, but the *model chooses* whether to fan out. Mitigate with an orchestrator agent definition + task phrasing that induces fan-out, identical across arms (legitimate — the arms differ only by plugin).

**Net:** the idea is sound, the moat is the honest measurement layer, and the codebase cooperates. Biggest schedule risks are EverOS integration subtleties and benchmark stability, not opencode.

---

## 2. Gate verification results

### G1 — Plugin API: **PASS**
- `tool.execute.after` **can fully rewrite tool output.** The hook mutates the exact object returned to the AI SDK (`session/tools.ts:111-129`); mutations reach the model in-turn and on every replay (`processor.ts:257-276`, `message-v2.ts:290-320`). It runs *after* truncation, so we rewrite final strings.
- `tool.execute.before` mutates args **in place only** (`tools.ts:109` vs `:111`; reassigning `output.args` is a no-op). No short-circuit except `throw` (surfaces as tool *error*). Fine: the local fs read is free; savings come from what reaches the LLM.
- Plugins can register custom tools, and a custom tool with a builtin's id **overrides it** (`registry.ts:253`, `tools.ts:92-99`).
- Subagent spawns observable via task-tool hooks + `session.created` bus events carrying `parentID` (`session.ts:537`). Plugin factory receives a pre-authed SDK client, `$` shell, project/worktree paths; runs in-process, unsandboxed.
- `chat.headers` hook exists but is **not needed for attribution** — see the free-attribution finding in §4.
- Caveat: the `event` hook is fire-and-forget; a hook that throws in `tool.execute.*` aborts the tool call — wrap everything in try/catch and fail open to raw content.

### G2 — EverOS keyed store: **FAIL as specified → PASS with redesign** (§3)

### G3 — Fan-out on stock opencode: **PASS mechanically, verify behaviorally on day 1**
- Parallel task calls in one turn are truly concurrent; no width cap; `subagent_depth` default 1 (orchestrator→workers only — actually *good* for clean attribution).
- Built-in subagents: `general` (full tools) and `explore` (read-only). Custom agents via config/markdown.
- Day-1 checklist stands: run 3 candidate tasks on stock opencode, count child sessions (`GET /session/:id/children` or the sqlite `session.parent_id`), measure redundancy before writing optimizer code.

---

## 3. EverOS: how to actually use it (the biggest design change)

EverOS = local-first Python server (`everos server start`, port 8000; Markdown + SQLite + LanceDB; no auth, loopback). Two mechanisms matter:

**(a) Exact-key digest store — the `unprocessed_messages` side door.**
`/add` buffers messages per `(session_id, app_id, project_id)` without extraction until a boundary/flush. `/search` with a *top-level scalar* filter `{"session_id": "<key>"}` returns `unprocessed_messages` — the raw buffered messages, **verbatim, no LLM rewrite**, straight from the SQLite buffer (read-your-write, not subject to the LanceDB index lag). So:

- Write: `POST /add` with `session_id = "digest:" + sha256(content)[:56] + ":" + task_class` (≤128 chars fits), one message whose `content` is the digest JSON. Never flush these sessions.
- Read: `POST /search` with `filters: {"session_id": key}`, read `unprocessed_messages[0].content`.
- Invalidation is free: content change ⇒ new hash ⇒ new key, exactly as the spec designed.
- **Day-1 test:** confirm the boundary detector doesn't auto-extract single-message buffers (if it ever trips, fallback: `/api/v2/knowledge/documents` — `PUT`/`GET /documents/{doc_id}` is a true ID-keyed store).
- Keep an **L1 in-process map** in the plugin (content-hash → digest) as read-through cache; EverOS is the durable, cross-session L2 and the system of record. This is defensible: "EverOS is the memory layer; the map is a latency cache over it."

**(b) Agent-track memory — the sponsor-native win.** EverOS's agent track (`agent_id` → `agent_case` / `agent_skill`, hybrid BM25+vector retrieval, self-evolving skills) is what "the agent remembers the codebase" *should* mean. After each run, `/add` + `/flush` a condensed run report (task, files touched, key findings) under `agent_id = "contextmesh:" + repo`. At orchestrator start and in M2 manifests, `/search` (`method: "hybrid"`) for task-relevant repo knowledge. This powers the cross-session cost-decline curve and uses EverOS the way EverMind intends — extraction, retrieval, skills — not just as an abused KV store. Configure EverOS's own extraction LLM **through our proxy** so its LLM cost lands in Snowflake and is netted out (total honesty, zero extra work).

---

## 4. Findings that reshape the build

1. **Attribution is free in both arms.** Every LLM request to a non-`opencode` provider already carries `X-Session-Id` and (for subagents) `x-parent-session-id` headers (`session/llm/request.ts:196-201`). The proxy reads them; Arm A stays *literally stock*. No telemetry shim plugin needed.
2. **The proxy must be an Anthropic-Messages passthrough, not an OpenAI-compatible shim.** opencode's prompt caching (`transform.ts:357-406`: `cacheControl` on first 2 system + last 2 messages) only fires for Anthropic-ish models, and on the OpenAI-compatible path **cache-write tokens are never reported** — the honest Arm A baseline would be crippled or unverifiable. Instead: keep `provider: anthropic` and set `options.baseURL: "http://127.0.0.1:8787"` (config `baseURL` wins; `{env:VAR}` substitution supported). The proxy forwards verbatim to `api.anthropic.com` and parses SSE `message_start`/`message_delta` for `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`. Native caching preserved; full cache split logged.
   - **A0 (caching off):** proxy env flag `CONTEXTMESH_STRIP_CACHE=1` strips `cache_control` blocks. Binary identical across arms; only env differs (env already injects run_id/arm).
3. **The read tool's output envelope is the digest's contract.** Read returns `<path>…</path><type>file</type><content>` with `N: line` numbering, 2000-line / 50 KB caps, and a trailer. The digest substitution must preserve the envelope and the *trailer format*, and keep **line anchors** — because the escape hatch is: **only full-file reads (no offset/limit) get digests; ranged reads always pass raw.** The digest note says "need exact code? re-read with offset=N, limit=M" — no schema changes, no fake params, and `escape_hatch_rate` becomes directly measurable (ranged read of a file within a session after a digest was served for it).
4. **Return path (M3) is one string in one place.** The task tool returns the child's final text inside `<task_result>` tags. `tool.execute.after` on `task` compresses it; the full text goes into EverOS keyed by child session id; a plugin-registered `expand_result(task_id)` tool is the escape hatch.
5. **Harness is a solved problem.** `opencode run --auto --format json` is fully non-interactive and emits per-step `cost` + `tokens {input, output, reasoning, cache{read,write}}`. `opencode export <sessionID>` dumps full traces. Sessions/messages live in sqlite (`opencode db "<sql>" --format json`) with per-session token columns. Use `OPENCODE_DB=/abs/bench.db` per run for isolation and `OPENCODE_CONFIG` for hermetic arm configs. **Gotcha:** parent session rollups exclude children — always sum the session tree. Cross-check proxy numbers against opencode's own accounting (two independent meters agreeing = credibility slide).
6. **Cost math:** opencode computes cost from models.dev rates (per-1M, reasoning billed at output rate). The proxy computes `cost_usd` from a **pinned rate table** checked into the repo so numbers don't drift mid-hackathon.
7. **Confound controls:** temperature 0 on agent defs; `OPENCODE_DISABLE_AUTOCOMPACT=1` both arms; fixed task order; fresh clone + pinned commit per run; same model both arms.
8. This fork ships a `snowflake-cortex` provider plugin (OAuth to Cortex-hosted models) — stretch: run `explore` subagents on Snowflake-hosted models for a deeper sponsor tie-in.

---

## 5. Amended architecture

```
opencode (Arm A: stock | Arm B: + contextmesh plugin)
  │  anthropic provider, baseURL → proxy      │ plugin hooks (B only):
  ▼                                           │  read.after  → digest swap (M1)
Token Proxy (FastAPI, ~250 lines)             │  task.before → manifest inject (M2)
  • verbatim forward to api.anthropic.com     │  task.after  → return compress (M3)
  • SSE usage parse; X-Session-Id headers     │  custom tools: expand_result,
  • hash read-tool payloads in requests       │                codebase_memory
  • JSONL spool → Snowflake loader            ▼
                                        EverOS (local, :8000)
                                          • digest KV (unflushed session buffers)
                                          • agent_case/skill repo memory (flushed)
Snowflake: AGENT_TOKEN_EVENTS + SESSION_TREE dims
  → V_REDUNDANCY / V_RUN_COST / V_SUBAGENT_ATTRIBUTION / V_SUCCESS_COST
  → Streamlit-in-Snowflake dashboard → Cortex Analyst/Agent (NL cost Q&A)
```

**Proxy notes.** Stream pass-through (no buffering); tag rows with `run_id`/`arm`/`task_id`/`trial` from env, `agent_id` = `X-Session-Id`, `parent_agent_id` = `x-parent-session-id`. File-read redundancy in *both* arms: walk request `messages`, map `tool_use` id→name, hash `tool_result` content for `read`; emit a read event only on **first appearance of (session, hash)** to avoid counting conversation replay. Schema additions to the spec DDL: `event_type ('llm_call'|'file_read'|'digest_gen')`, `session_id`, `parent_session_id`, `provider`, `request_id`, `finish_reason`, `error`.

**M1 flow (plugin, `read.after`):** skip if offset/limit set, file < 60 lines, or file matches the task's stated target → hash content → L1 → EverOS lookup → **hit:** replace `<content>` with digest + escape-hatch note (keep envelope + trailer) , set `metadata.was_digest`; **miss:** pass raw through, then fire-and-forget: summarize with cheap model (haiku via the proxy, `agent_id="contextmesh-summarizer"`), store to EverOS. Guard: digest must be ≤ ~35% of raw tokens or store a "do-not-digest" marker. Task-class for the key: coarse regex over the subagent's task prompt (explore/implement/test/fix/migrate).

**M3 flow (`task.after`):** if `<task_result>` > ~1,500 tokens, compress via cheap model; stash full text in EverOS under the child session id; note "full result: expand_result('<id>')".

**M2 (de-scoped, stretch):** `task.before` prepends a manifest to `args.prompt`: relevant EverOS repo-map entries + `GET /find/symbol` hits for keywords. No dependency resolver, no LSP promises.

**Benchmark.** Repo with 8–15 sibling modules sharing core files (models/utils/middleware) — e.g. a RealWorld Express/Nest implementation or the full-stack-fastapi-template; shared-file overlap is what drives redundancy. 8–10 tasks phrased to fan out ("spawn a subagent per handler…"), n=3, both arms, success = tests/build exit code, scored blind. Harness = bash/TS over `opencode run --format json` + sqlite session tree + proxy JSONL → Snowflake loader.

**Extra headline (new):** run the suite twice in Arm B — **cold vs warm**. Pinned commit ⇒ same hashes ⇒ second run hits digests from message one. That's the "cost per task falls as the agent remembers the codebase" line, on a chart.

---

## 6. Build order

0. **Day-1 gates (½ day):** stock fan-out count on 3 tasks; EverOS unflushed-buffer verbatim round-trip + latency; proxy SSE passthrough smoke test. Kill/pivot criteria per spec §10 stay.
1. **Proxy + DDL + loader (1 day).** Route own dev usage through it immediately (free "what we burned" slide).
2. **Redundancy analysis on stock runs (½ day).** This is the insurance policy and the ceiling computation — if dup reads < 10–15% of spend, pivot to measurement-and-attribution per spec.
3. **Plugin M1 (1–1.5 days)** → pilot A/B on 2 tasks → full bench n=3.
4. **M3 (½ day).** 5. **Dashboard + Cortex Agent (1 day, parallel-safe).** 6. **M2 + Cortex-hosted subagents (stretch).** 7. **Record demo video the first time a full B run works.**

Parallel-agent scoping as in spec §9 (proxy/bench/dashboard/plugin) — hand every agent the DDL + the read-envelope contract.

---

## 7. Top risks (updated)

| Risk | Mitigation |
|---|---|
| EverOS boundary detector auto-extracts digest buffers | Day-1 test; fallback to knowledge-doc store (`GET /documents/{doc_id}`) |
| Model won't fan out on benchmark tasks | Orchestrator agent def + explicit fan-out phrasing, identical both arms; verified day 1 |
| Digest too lossy → retries eat savings | Escape hatch via ranged reads; θ guards; `escape_hatch_rate` + `retry_count` tracked; raise θ or ship M3-only |
| SSE proxy bugs corrupt runs | Passthrough-only design; verbatim header forward; smoke test with long streams; arms share the binary |
| Hook exception kills a tool call | try/catch everything; fail open to raw |
| Cortex/Streamlit setup drags | Dashboard team starts day 1 against DDL with synthetic rows |

**Judge-proofing:** headline is B vs A (caching on); summarizer + EverOS extraction costs netted out (they flow through the same proxy); proxy vs opencode-native accounting cross-check; success reported next to every cost number.
