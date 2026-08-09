# ContextMesh + ReasonRenderCoding on native Codex

This demo runs the installed **Codex CLI** with its normal OpenAI authentication. It does not use a
custom model provider, compatibility endpoint, bearer-key environment variable, or model-traffic
proxy. ContextMesh supplies sealed shared-source context and compresses oversized native
`wait_agent` results; ReasonRenderCoding supplies a validated per-worker audit packet.

## Product shape

- `RRDdemo-local.sh` stores ContextMesh digests in the sealed round manifest and RRC cases in a
  round-local SQLite database. It starts, checks, and stops no memory service.
- `RRDdemo-everos.sh` stores those records in a demo-owned EverOS instance on `127.0.0.1:8000`.
  The paths used here are assistant-only add and keyword search, so no embedding or model API key is
  required by EverOS.
- `RRDdemo.sh` is an alias for the EverOS variant.
- `demo.sh` is a compatibility redirect to the local native-Codex variant.

Both variants use the same installed Codex version, model, prompt, four-worker collaboration flow,
hook implementation, and strict final-report grammar. Only the memory backend differs.

## Prerequisites and login

- Codex CLI `0.147.0`
- model `gpt-5.5` available to the signed-in account
- Python 3.11+ and `uv`
- `git`; Docker is optional for EverOS because the launcher can use the checked-out EverOS project

The demo uses a stable, credential-free `contextmesh/.codex-rrd-native` configuration directory and
requires native OS-keyring authentication. It never copies or links an `auth.json` file.

```bash
cd contextmesh
./RRDdemo-local.sh login       # prints the exact keyring-login command
# run the printed command once, complete login, then:
./RRDdemo-local.sh canary
```

`RRD_CODEX_BIN` may point to an absolute Codex executable. `RRD_CODEX_MODEL` and
`RRD_CODEX_REASONING` are optional nonsecret settings; see `env.example`. Launchers deliberately do
not source `.env.local`.

## Run the local variant

```bash
cd contextmesh
./RRDdemo-local.sh prep
# terminal 1
./RRDdemo-local.sh a
# terminal 2
./RRDdemo-local.sh b
# terminal 3
./RRDdemo-local.sh meter
./RRDdemo-local.sh down
```

## Run the EverOS variant

```bash
cd contextmesh
./RRDdemo-everos.sh prep
# terminal 1
./RRDdemo-everos.sh a
# terminal 2
./RRDdemo-everos.sh b
# terminal 3
./RRDdemo-everos.sh meter
./RRDdemo-everos.sh down
```

`prep` creates a backend-bound round, copies the benchmark target, deterministically seeds three
shared-file digests for each arm, and copies the exact audit prompt. Paste that prompt into both
TUIs. Side A is RRC COLD (four misses); side B is RRC WARM (one miss and three exact case hits).
For reproducible noninteractive testing, `run-a` and `run-b` execute the same arms through
`codex exec --json` and save the root event stream in the corresponding arm directory.

EverOS 1.2.3 eagerly constructs an LLM client at startup even for storage-only traffic. The EverOS
launcher supplies a public sentinel credential pointed at a closed loopback port; the demo's
assistant-only writes and keyword reads make no external model or embedding request.

Each root must launch exactly four native workers in parallel, one for each file under
`src/handlers/`. A PreToolUse hook validates the assignment and attaches the handler plus RRC packet.
A SubagentStart hook attaches the sealed digest and exact current bytes for `src/models.js`,
`src/utils.js`, and `src/middleware.js`. A PostToolUse hook stores full completed reports as mode-0600
receipts and can replace a large wait result with a deterministic summary below both 2,000 UTF-8
bytes and 65% of the canonical raw response. Any compression error fails open and makes the meter
NOT READY.

## Measurement

The meter reports native Codex provider-visible token usage from root, worker, and RRC planner
evidence. It never presents those values as exact billed consumption because the stock CLI does not
expose internal retry accounting. A comparison is valid only when all expected workers, source
hashes, backend identities, RRC branches, wait receipts, final merge, and usage records correlate.
Missing or malformed evidence is `NOT READY`; it is never converted to zero.

Token-efficiency claims must use comparable successful cells: same model/reasoning, same prompt and
source hashes, the same worker count, and a frozen quality rubric. Compare a native baseline with no
ContextMesh/RRC intervention against local and EverOS combined cells at worker counts 1, 2, and 4.
Report provider-visible totals and the observational reduction
`(baseline - combined) / baseline`, with setup costs separate and hidden-retry uncertainty stated.

Run the complete controlled comparison from the repository root:

```bash
python3 contextmesh/bench/run_bench.py --run-matrix
```

The runner executes the nine baseline/local/EverOS cells at one, two, and four workers in a
Latin-square order. It has no token watchdog, per-cell token ceiling, or aggregate token budget; a
12-minute wall timeout remains to terminate hangs. Every independent cell is attempted, and invalid
or timed-out evidence is preserved rather than converted to zero. Interrupted source-identical runs
can be continued with `--run-dir <path> --resume`. Raw artifacts remain under ignored
`contextmesh/runs/`; only the concise result report is intended for Git.

## Deterministic verification

```bash
uv run --locked pytest -q -p no:cacheprovider
uv run --locked ruff check rrc tests contextmesh/scripts
uv run --locked ruff format --check rrc tests contextmesh/scripts
uv run --locked pyright rrc contextmesh/scripts tests
bash -n contextmesh/RRDdemo*.sh contextmesh/demo.sh contextmesh/scripts/rrd_*.sh
```

The live canary requires the native keyring login and makes a real model request:

```bash
contextmesh/RRDdemo-local.sh canary
contextmesh/RRDdemo-everos.sh canary
```

Run the local canary first. EverOS startup refuses a healthy foreign listener on `:8000`, records
the process/container it owns, and tears down only that verified identity.
