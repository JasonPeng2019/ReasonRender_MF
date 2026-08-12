# EverOS — Repository Rundown

> **Historical / non-RRCv2 / superseded.** This research snapshot is retained only for
> reproducibility; it is not current RRCv2 product guidance. ADR 0002 makes EverOS an optional index
> adapter, and `docs/RRCv2.md` governs the active algorithm.

> `EverMind-AI/EverOS` — "One portable memory layer for every AI agent: local-first, Markdown-native, user-owned, and self-evolving across apps, tools, and workflows."
>
> Sources: the repo README, the EverOS product site, EverMind's public-beta announcement, and independent write-ups (MarkTechPost, DeepWiki, encorp.ai). Performance figures are vendor-reported unless noted.

---

## 1. At a glance

| Field | Value |
|-------|-------|
| Project | EverOS (part of the EverMind ecosystem) |
| What it is | Python library + local-first memory runtime for AI agents |
| License | Apache 2.0 |
| Language | Python (3.12+) |
| Popularity | ~11.9k stars, ~882 forks (at time of reading) |
| Storage stack | Markdown (source of truth) + SQLite (state/queues) + LanceDB (vector + BM25 index) |
| Retrieval | Hybrid: BM25 (lexical) + dense vectors + scalar filtering, with a reranker; marketed as "mRAG" |
| Deployment | Self-hosted (OSS) or EverOS Cloud (managed); same SDK/API/format |
| Core dependency | External models via OpenAI-compatible endpoints (OpenRouter, DeepInfra, vLLM, Ollama, etc.) |

---

## 2. What problem it targets

LLMs are stateless: when a session ends, the context is gone. Most agent stacks paper over this by replaying history into the context window, which is expensive and degrades as it grows. EverOS's proposition is to move memory out of the prompt (and out of an opaque vector DB) and into **plain Markdown files** that act as the canonical, human-readable, Git-versionable source of truth, then index those files locally for fast retrieval.

It is deliberately **not** a full agent framework. It's a library/runtime you drop into an existing loop, expose over an HTTP API, and point at model backends that already speak the OpenAI protocol.

---

## 3. Architecture

Three architectural pillars:

### 3.1 Markdown as source of truth
Every memory record lands as a `.md` file on local disk (default under `~/.everos`). You can open, edit, `grep`, diff, Git-version, or view it in Obsidian. Direct edits to a `.md` file are picked up by a **cascade file-watcher** that re-syncs the indexes, so the human-editable layer and the machine index stay aligned.

### 3.2 Lightweight three-piece storage
No MongoDB / Elasticsearch / Redis. Instead:
- **Markdown** — truth / content
- **SQLite** — state and change queues
- **LanceDB** — high-performance store for both dense vectors and BM25 search

### 3.3 EverAlgo decoupling
The extraction / ranking / parsing logic is offloaded to **EverAlgo**, a *stateless* set of algorithm libraries. EverOS itself is the orchestrator + persistence layer; EverAlgo holds no state and is storage-agnostic. This is why EverOS needs external model credentials — EverAlgo calls out to the configured LLM / embedding / rerank models and hands results back for EverOS to persist.

---

## 4. Memory model

EverOS separates memory into four types (cognitive-science framing) and, importantly, into **two tracks**.

| Type | Answers | Example |
|------|---------|---------|
| Episodic | "What happened?" | A past support ticket or project decision |
| Semantic / Profile knowledge | "What's true / who is this?" | User prefers Python; formal comms style |
| Procedural (Skills) | "How is this done?" | A multi-step deployment SOP |
| Profile | Long-term identity/preferences | Role, tone, standing preferences |

**Two tracks kept separate as first-class surfaces:**
- **User track** → `episodes/profile`
- **Agent track** → `cases/skills`

Retrieval is **orthogonal**: you can scope a query by `user_id`, `agent_id`, `app_id`, `project_id`, and `session_id` — which matters for isolation in multi-agent / multi-tenant setups.

---

## 5. The headline feature: Self-Evolving Skills

This is what EverMind positions as unique. The pipeline turns raw agent activity into reusable procedures.

### 5.1 The four stages

1. **Case extraction.** Each completed task is recorded as a *Case* (an execution trajectory). Rather than the raw transcript, EverOS extracts structured fields: **Task Intent, Approach, Key Insights, and a Quality Score**. The quality score is how later stages tell a "win" from a failed/mediocre run.
2. **Semantic clustering.** Cases are grouped by **vector-based semantic clustering** — Skills emerge from a *cluster* of related successful Cases, not a single lucky run.
3. **Distillation (offline).** Clustered experiences are consolidated into reusable Skills, expressed as **Standard Operating Procedures (SOPs)**. This runs asynchronously between sessions, not in the live loop.
4. **Incremental evolution.** Skills aren't frozen: repeated successes reinforce steps, while failures add "trap warnings." A `Reflection` lifecycle component (added in 1.1.x) further refines profiles, episodes, and skills while the system is idle/offline.

### 5.2 How the data is actually processed (the mechanism)

A common misconception is that this is one mechanism. It's a **layered pipeline** mixing generative, learned, and deterministic steps:

| Step | Mechanism | Deterministic? |
|------|-----------|----------------|
| Transcript → Case fields | Generative **LLM** (`EVEROS_LLM__*` slot) | No — model-dependent, non-deterministic |
| Case text → vector | Learned **embedding model** (`EVEROS_EMBEDDING__*`, a separate neural net) | No — depends on the embedding model |
| Vectors → clusters | Vector-space clustering algorithm | Yes, for a fixed set of vectors |
| Query-time retrieval | BM25 (lexical/token stats) + dense vectors + learned **reranker** (`EVEROS_RERANK__*`) | BM25 yes; embeddings/rerank no |

Key points:
- **Case extraction is LLM-driven**, not rule-based. Fields like "Task Intent" and "Quality Score" are semantic abstractions no deterministic parser could emit. The test suite confirms this — extraction tests are marked `live_llm` (they need real model credentials to run).
- **Clustering vectors are learned embeddings**, not deterministic token/TF-IDF vectors, and they are produced by a *different* model than the chat LLM (a dedicated embedding model).
- The only genuinely deterministic pieces are the **clustering math** (given fixed vectors) and **BM25** at retrieval time.
- Net effect: a Skill's provenance passes through **two non-deterministic learned stages** before any deterministic step. The same raw experience can distill differently depending on which models fill the slots — which is exactly why "how a Case became a Skill" is hard to audit.

> The exact clustering algorithm (k-means vs. HDBSCAN vs. threshold agglomerative) and the promotion thresholds are **not** published in the README / press material — they live in `src/everos` + EverAlgo and would need source inspection to confirm.

---

## 6. Retrieval ("mRAG")

A single LanceDB query blends three signals:
- **BM25** keyword matching (sparse/lexical)
- **Dense vector** similarity (semantic)
- **Scalar filtering** (by the orthogonal scope IDs above)

Results are then passed through a **rerank** model. EverMind markets the multimodal variant of this path as **mRAG**. The pitch is precise retrieval at inference speed without loading the whole context window.

---

## 7. Multimodal ingestion

A single `/memory/add` call can take text, images, PDFs, audio, office docs, slides, HTML, email, and URLs. Requires the optional extra:

```bash
uv pip install 'everos[multimodal]'   # pulls in everalgo-parser
```

- Non-text content is parsed and fed to a **multimodal LLM** (default `google/gemini-3-flash-preview` via OpenRouter, `EVEROS_MULTIMODAL__*`).
- **Office formats** (`.doc/.docx/.ppt/.pptx/.xls/.xlsx`) require **LibreOffice** on the host — the parser shells out to headless `soffice` to convert to PDF first. Without it, office uploads return HTTP 415. PDF/image/audio/HTML/email are unaffected.

---

## 8. Quick start

**Prerequisites:** Python 3.12+. No API keys needed for the demo.

```bash
# 1. Install
uv pip install everos          # or: pip install everos

# 2. Play with the offline visualizer (no keys, no server)
everos demo                    # also: --cinematic, --plain

# 3. Generate .env and fill provider keys
everos init                    # writes ./.env  (--xdg for ~/.config/everos/.env)

# 4. Start the server
everos server start
curl http://127.0.0.1:8000/health     # -> {"status":"ok"}

# 5. Make the demo real against the running server
everos demo --live
```

**Contributor setup:**
```bash
git clone https://github.com/EverMind-AI/EverOS.git
cd EverOS
uv sync
source .venv/bin/activate
everos demo --plain
make test
```

---

## 9. HTTP API

Business endpoints live under `/api/v2` (`/api/v1` is a legacy alias that still resolves but may be removed). Core loop:

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Liveness check |
| `POST /api/v2/memory/add` | Ingest messages / files into a session |
| `POST /api/v2/memory/flush` | Force extraction (useful for local demos) |
| `POST /api/v2/memory/search` | Retrieve memories by query + scope |

Minimal add → flush → search example:
```bash
# add
curl -X POST http://127.0.0.1:8000/api/v2/memory/add -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-001","app_id":"default","project_id":"default",
       "messages":[{"sender_id":"alice","role":"user","timestamp":0,"content":"I climb in Yosemite every spring."}]}'
# flush (force extraction)
curl -X POST http://127.0.0.1:8000/api/v2/memory/flush -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-001","app_id":"default","project_id":"default"}'
# search
curl -X POST http://127.0.0.1:8000/api/v2/memory/search -H 'Content-Type: application/json' \
  -d '{"user_id":"alice","app_id":"default","project_id":"default","query":"Where do I climb?","top_k":5}'
```

Markdown is written synchronously; the local index catches up in the background, so a first search may lag slightly.

---

## 10. Configuration / providers

Four model capabilities, each an OpenAI-compatible slot you can point anywhere:

| Capability | Env prefix | Default provider | Role |
|------------|-----------|------------------|------|
| Chat | `EVEROS_LLM__*` | OpenRouter | Case/memory extraction, reasoning |
| Multimodal | `EVEROS_MULTIMODAL__*` | OpenRouter (`gemini-3-flash-preview`) | Parsing non-text content |
| Embedding | `EVEROS_EMBEDDING__*` | DeepInfra | Vectors for dense search + clustering |
| Rerank | `EVEROS_RERANK__*` | DeepInfra | Re-ordering retrieval candidates |

The Chinese quick-start defaults to Alibaba DashScope (`qwen-plus`, `text-embedding-v4`, `gte-rerank-v2`). Override any `*__BASE_URL` to use OpenAI / vLLM / Ollama / etc. `.env` search order: `--env-file` → `./.env` → `${XDG_CONFIG_HOME}/everos/.env` → `~/.everos/.env`.

---

## 11. Deployment & pricing

| Plan | Cost | Notes |
|------|------|-------|
| Self-hosted (Community) | $0, Apache 2.0 | Unlimited; your compute; every layer inspectable |
| Cloud Free | $0 | 3 memory spaces; 50k MCU/mo; 100k retrieval calls/mo |
| Cloud Pro | $25/mo (free during beta) | 8 spaces; 250k MCU/mo; 500k calls/mo; Self-Evolving Skills enabled |
| Cloud Enterprise | Custom | Custom quotas, private deployment, dedicated support |

Cloud and self-hosted share the same SDK, retrieval engine, and memory format — switch endpoints, not code.

---

## 12. Repository layout (top level)

```
.claude/            benchmarks/     data/           docs/
examples/langfuse/  scripts/        src/everos/     tests/
use-cases/          .env.example    config.example.toml
CHANGELOG.md        QUICKSTART.md   README.md       README.zh-CN.md
CITATION.md         CONTRIBUTING.md LICENSE (Apache-2.0)
```

Key docs: `docs/how-memory-works.md` (Markdown/SQLite/LanceDB + recall flow), `docs/engineering.md` (build/test/CI), `docs/use-cases.md`, `docs/migration-to-1.0.0.md`, `QUICKSTART.md`.

---

## 13. The EverMind ecosystem

EverOS is one repo in a "research-to-runtime" stack:

| Component | Role |
|-----------|------|
| **EverOS** | The memory runtime (this repo) |
| **Raven** | Self-improving agent harness for terminal-native agents |
| **EverAlgo** | Stateless extraction / ranking / parsing operators powering EverOS |
| **HyperMem** | Hypergraph memory for long conversations (topic → episode → fact) |
| **EverMemBench / EvoAgentBench** | Benchmarks for conversational memory and agent self-evolution |
| **MSA (Memory Sparse Attention)** | Long-context research targeting ~100M-token contexts |
| **EverMe** | Personal cross-device / cross-agent memory layer |
| **evermem-claude-code / everos-plugins** | Integration plugins & migration tooling |

---

## 14. Performance claims (vendor-reported — verify independently)

- **93.05%** on the LoCoMo long-term-memory benchmark
- **83.00%** on LongMemEval
- **<500ms** p95 retrieval latency
- **~7–15×** lower token cost vs. full-context baselines
- Up to **+234.8%** complex-task success rate from self-evolving skills (on EverMind's own EvoAgentBench)
- Core algorithms cited as featured at ACL 2026

All of the above are produced by the vendor and/or measured on vendor-built benchmarks. Treat as directional and validate on your own workload.

---

## 15. Critical assessment

**Strengths**
- Genuinely inspectable: Markdown truth + local SQLite/LanceDB means no black-box state.
- Low operational footprint (no heavy server dependencies) and provider-agnostic via OpenAI-protocol slots.
- Clean separation of user vs. agent memory and orthogonal scoping is well-suited to multi-agent/multi-tenant use.
- No lock-in: export is just the Markdown you already own.

**Caveats**
- **"Token efficiency" is a side effect, not the purpose.** It's fundamentally a persistence + retrieval layer; the cost savings come from retrieving a slice instead of replaying full history (standard RAG economics). It also *relocates* rather than eliminates cost — extraction and embedding are LLM/model calls done at write time, amortized over later reads. Read-heavy workloads win; write-heavy, rarely-recalled ones win less.
- **Self-evolving skills are the least-proven part.** Distillation pipelines can drift — over-generalize from small samples, preserve brittle steps, or carry forward a decision that merely looked successful once. Independent reviewers frame it more soberly as "workflow compression" than self-improvement.
- **Auditability gap.** A Skill's lineage runs through two non-deterministic learned stages (LLM extraction → embedding) before deterministic clustering. Promotion thresholds and the clustering method aren't documented publicly; roll-back ergonomics are unclear.
- **Benchmarks are self-reported** on partly self-built suites.

**Bottom line:** a solid, transparent, open memory/retrieval substrate for agents, with a more speculative capability layer (skill evolution) bolted on top. The core is the reliable value; the self-evolution is the interesting-but-unproven bet.

---

## 16. Links

- Repo: https://github.com/EverMind-AI/EverOS
- Product page: https://evermind.ai/everos
- Docs: https://docs.evermind.ai/introduction
- PyPI: https://pypi.org/project/everos/
- License: Apache 2.0
