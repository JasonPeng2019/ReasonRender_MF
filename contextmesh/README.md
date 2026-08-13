# ContextMesh — CLI validation runtime, with an archived OpenCode demo

> **Active FULL-fix runtime (2026-08-10).** The measured harness uses the
> locally authenticated Codex and Claude CLIs. ContextMesh's Python MCP server
> calls a bounded, standard-tier Claude CLI digest child and stores assistant-only
> exact-key buffers in a local EverOS process. It uses no Ollama credential or
> Tollgate proxy. On Windows, EverOS runs through WSL because EverOS requires
> Linux's `fcntl` lock implementation; its data root remains under this checkout.

## Active quickstart

```powershell
# .env.local is local-only and contains non-secret CLI settings.
Get-Content contextmesh/.env.local

# Start local EverOS through WSL from this checkout.
wsl.exe bash -lc "cd /mnt/c/Users/Jason/Documents/Jason/ReasonRender-real && bash contextmesh/scripts/start_stack.sh"

# Prove the assistant-only exact-key path.
wsl.exe bash -lc "cd /mnt/c/Users/Jason/Documents/Jason/ReasonRender-real && python3 contextmesh/scripts/smoke_everos.py"

# Run the current CLI harness only after its normal provider opt-in.
$env:REASONRENDER_ALLOW_PROVIDER_EXECUTION = '1'
python -m harness.runner round live --execute
```

The launcher gives EverOS an unreachable local placeholder only to satisfy its
eager client construction. ContextMesh writes assistant-only buffers, which
EverOS parks without making an LLM request; the actual digest provider is the
Claude CLI configured in `.env.local`.

## Archived OpenCode/Tollgate demo (not the active workflow)

Side-by-side demo system: **Arm A** runs stock opencode on a fan-out task and pays
full price for every sibling subagent re-reading the same files. **Arm B** runs the
*same binary, same config, same task* with one addition — the ContextMesh plugin —
which serves EverOS-cached structural digests for repeat file reads and compresses
oversized subagent results. A production-grade token proxy (Tollgate) meters every
request in both arms; Snowflake analyzes the economics.

```
opencode (Arm A: stock | Arm B: + contextmesh plugin)
  │  custom provider "ollama" (@ai-sdk/openai-compatible)
  │  baseURL → http://127.0.0.1:8788/ollama/<runid>-<arm>-<mode>/v1
  ▼
Tollgate proxy :8788  ──────────────► https://ollama.com (deepseek-v4-flash:cloud)
  • durable JSONL, exact provider-reported usage, per-run session attribution
  ▼                                   Arm B plugin hooks:
runs/tokens.jsonl                       read.after  → digest swap (M1)
  │                                     task.after  → result compression (M3)
  ▼                                     custom tool: expand_result
snowflake/load.py → AGENT_TOKEN_EVENTS  events → metrics.jsonl
  → V_RUN_COST / V_ARM_COMPARISON     EverOS :8000 (memory layer)
  → V_METER_CROSSCHECK                  • digest KV: unflushed assistant-only buffers
                                        •   (zero-LLM parked, verbatim read-your-write)
                                        • extraction LLM routed through Tollgate
```

## Layout

```
contextmesh/
  plugin/contextmesh.ts     the Arm B plugin (M1 + M3 + expand_result + metrics)
  configs/arm-a.json        stock arm (provider + orchestrator/worker agents)
  configs/arm-b.json        identical + plugin entry
  bench/target-template/    benchmark repo (4 handlers sharing 3 core modules)
  bench/run_bench.py        hermetic A/B/warm harness
  bench/analyze.py          report: % reduction, redundancy, meter cross-check
  scripts/start_stack.sh    Tollgate :8788 + EverOS :8000
  scripts/smoke_everos.py   digest-KV production-property test
  snowflake/ddl.sql         AGENT_TOKEN_EVENTS + comparison views
  snowflake/load.py         JSONL → Snowflake loader (--dry-run works offline)
  .env.local                OLLAMA_API_KEY etc. (gitignored — never commit)
  runs/                     per-run artifacts (gitignored)
```

## Archived quickstart

```bash
# 0. prerequisites: bun, uv, python3; submodules checked out; deps installed:
#    (cd opencode && bun install); (cd TokenTracker && uv sync --locked --all-groups); (cd EverOS && uv sync)
# 1. credentials
cat contextmesh/.env.local   # OLLAMA_API_KEY, OLLAMA_BASE_URL, CONTEXTMESH_MODEL

# 2. stack
contextmesh/scripts/start_stack.sh
python3 contextmesh/scripts/smoke_everos.py     # must print PASS

# 3. bench (arm A, arm B cold, arm B warm — one command)
python3 contextmesh/bench/run_bench.py --runid demo1

# 4. report
python3 contextmesh/bench/analyze.py --runid demo1
cat contextmesh/runs/demo1/report.md

# 5. Snowflake (optional; --dry-run needs no account)
python3 contextmesh/snowflake/load.py --dry-run
```

## How the optimization works (and stays safe)

**M1 — digest swap.** On every full-file `read` (no offset/limit, ≥60 lines) the
plugin hashes the content. Hit in EverOS → the read envelope's content is replaced
with a structural digest (line-anchored symbol map) plus an escape-hatch note; miss
→ raw passes through and the file is summarized once by the cheap model (metered
under `<session>-summarizer` — the overhead is counted, not hidden) and stored.
Ranged reads ALWAYS bypass the cache — that is the escape hatch, and its use is
logged (`escape_hatch` metric). A digest that exceeds the size guard is stored as
do-not-digest. Every hook fails open: any error → raw content.

**M3 — result compression.** `<task_result>` payloads over ~6000 chars are stored
in full in EverOS (key `taskresult:<child-session-id>`), compressed by the cheap
model, and the parent receives the compressed version + a pointer to the
`expand_result` tool.

**EverOS as an exact-key store.** Digests are written via `/api/v2/memory/add` as
assistant-role-only messages: EverOS parks such buffers with **zero LLM calls** and
never auto-extracts them. Reads use `/memory/search` with `method: "keyword"` and a
top-level `{"session_id": <key>}` filter, which returns the buffered message
verbatim from SQLite (read-your-write). `scripts/smoke_everos.py` proves all of
this against the live server. The in-process map in the plugin is an L1 latency
cache; EverOS is the durable cross-run layer — which is what makes **warm runs**
(same `--runid` namespace) hit from the very first read.

## Measurement honesty

- **Two independent meters.** Tollgate logs provider-reported usage per request
  (durable JSONL, exact-only in reports); opencode's own sqlite accounting is
  collected per run. The analyzer prints both and their delta. Known expected
  deltas: session-title generation calls (proxy-only; often `measurement_state:
  missing` because opencode aborts the stream early) and Arm B summarizer calls
  (reported separately, included in Arm B totals).
- **Nothing netted out.** The B-vs-A comparison includes summarizer overhead.
- **Dollar figures are modeled** at reference per-1M rates (Ollama Cloud is
  subscription-priced); token counts are the measurement.
- **Confound controls:** temperature 0 on both agents, same model both arms, fresh
  workspace + fresh `OPENCODE_DB` per run, `OPENCODE_CONFIG_DIR` isolation,
  autocompact disabled, per-run Tollgate session names.
- **Turn-parity enforcement (why B stays below A):** a reasoning model takes a
  variable number of turns per run, and each extra turn re-sends the whole
  conversation — early on this could push side B's *total* above A even though
  its reads were cheaper. The demo config enforces parity so that can't happen:
  (1) **authoritative digests** (`CONTEXTMESH_AUTHORITATIVE_DIGEST=1`) present the
  digest as complete and drop the "re-read for exact lines" invitation;
  (2) the worker prompt (identical in both arms) says read each file exactly once;
  (3) **`CONTEXTMESH_BLOCK_REREAD=1`** makes the plugin's `tool.execute.before`
  strip offset/limit from any ranged *re-read of an already-digest-served file*,
  collapsing it back to the cheap digest — this only touches files already
  digested this session, so a worker's raw read of its own audit target is
  untouched and audit quality is preserved; (4) equal `steps` caps (worker 8,
  orchestrator 12) in **both** arms as a runaway backstop. With this on, observed
  rounds run at equal turn counts (e.g. A=12 / B=12) with B ~34% below A and the
  step caps never actually hit (no truncation). `arm-a.json` and `arm-b.json` are
  byte-identical except the `plugin` field.
- **Workload sizing:** savings scale with *(workers − 1) × shared-file size*.
  `bench/target-template` has 4 handlers sharing three core modules (~43K chars).
  Bigger/more shared modules → bigger, steadier win.

## Interactive TUI demo (side-by-side, same prompt, two backends)

Both sides run the **installed `opencode` binary** (v1.18.15 — the real CLI, which
renders the TUI). The ONLY difference is `arm-b.json` loading the ContextMesh
plugin; both configs pin the same model (`ollama/deepseek-v4-flash:cloud`) and
default agent (`orchestrator`), so each TUI opens demo-ready. Requires the
installed opencode to be v1.18.15+ (matches the pinned submodule) — `opencode
--version` to check; override the binary with `CONTEXTMESH_OPENCODE_BIN`.

```bash
cd contextmesh
./demo.sh prep         # start stack + new round + seed digests + copy prompt + preflight
# terminal 1:  ./demo.sh a        # STOCK opencode
# terminal 2:  ./demo.sh b        # opencode + ContextMesh
# terminal 3:  ./demo.sh meter    # live token race for THIS round
```

`demo.sh prep` wraps `start_stack.sh` + `demo_tui.sh reset` + `demo_tui.sh seed`
+ clipboard + `demo_preflight.sh`. The individual scripts still work standalone
if you prefer.

In each TUI the agent + model are preselected; just paste (⌘V) the prompt and
press Enter on both. The live meter reads the current round only (from
`runs/demo-tui/round`), so it counts just this head-to-head. Run
`demo_tui.sh reset` before each fresh comparison.

> Why not run from source? `bun run src/index.ts` starts the TUI but its
> renderer draws nothing (blank screen) — the reliable path is the compiled
> `opencode` binary, which is the same v1.18.15 and loads the plugin identically.

## Demo-day flow

1. `start_stack.sh`, smoke test, then `run_bench.py --runid live` before the slot
   (or pre-run and keep `runs/<id>/` as the fallback).
2. Show `report.md`: Arm A's token burst (subagents × duplicate reads), Arm B cold
   (digests being built, partial savings), Arm B warm (full savings — "the agent
   remembers the codebase").
3. Show EverOS markdown/SQLite artifacts (`contextmesh/everos-root/`) and
   `runs/<id>/b-*/metrics.jsonl` for digest hit/miss/escape-hatch events.
4. Load Snowflake (`snowflake/load.py`) and chart `V_ARM_COMPARISON` /
   `V_METER_CROSSCHECK` in a Streamlit-in-Snowflake worksheet.

## Archived provider switching

Everything is env/config driven: `CONTEXTMESH_MODEL` + `OLLAMA_API_KEY` in
`.env.local`, provider block in `configs/arm-*.json`, `--model` on the harness.
To use Anthropic instead: change the Tollgate route to
`anthropic=anthropic@https://api.anthropic.com`, set the provider to `anthropic`
with `baseURL` pointing at the proxy path, and set the summarizer URL to an
Anthropic-protocol endpoint (the plugin's summarizer speaks OpenAI chat
completions; swap `chatComplete` or front it with an OpenAI-compatible shim).

## Troubleshooting

- `tollgate: already running` but nothing on :8788 → another app owns the port;
  the health check hits `/healthz` which any app may answer. Change the port in
  `start_stack.sh` + `run_bench.py`.
- EverOS 422 on search → you used `method: "hybrid"` without an embedding
  provider; the plugin always uses `keyword`.
- Empty `unprocessed_messages` on a key you wrote → check the filter is a
  top-level scalar `{"session_id": key}` (no operator wrappers) and `app_id` /
  `project_id` match the write.
- Plugin not loading → `CONTEXTMESH_PLUGIN_PATH` must be a `file://` URL to
  `plugin/contextmesh.ts`; check `metrics.jsonl` for `plugin_loaded`.
- opencode reads files outside the run workspace → the workspace must be its own
  git repo (`run_bench.py` runs `git init` in each copy); otherwise opencode
  walks up to the enclosing repo.
