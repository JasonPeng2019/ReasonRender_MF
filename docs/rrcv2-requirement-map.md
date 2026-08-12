# RRCv2 requirement map

Authority: `docs/RRCv2.md` SHA-256
`2a036584574a610ebcf1f32517249166d3f802b1026b83f3cfab4d41a1c5a80e`.

The implementation retains three intentional product changes: ContextMesh is the native-worker
transport, EverOS is optional with SQLite as the offline store/index, and model calls use native
Codex authentication rather than Snowflake Cortex. Everything else below maps the original design
to an active implementation or an explicit evidence limitation.

| RRCv2 requirement | Implementation | Primary tests/evidence | Status |
|---|---|---|---|
| Retrieve, own-store lookup, EXACT/NEAR/MISS branch (§1, §4.6) | `rrc/retrieval.py`, `rrc/pipeline/solve.py` | `tests/test_rrc_retrieval.py`, `tests/pipeline/test_solve.py` | Implemented |
| Strong SPEC on MISS with plan, signatures, contracts, tests, and slots (§2) | `rrc/pipeline/prompts.py`, `rrc/pipeline/stages.py`, `rrc/contract.py` | `tests/pipeline/test_stages.py`, `tests/test_rrc_contract.py` | Implemented |
| Cheap PRIME from one or two own-store neighbours, with unfit fallback (§2) | `rrc/pipeline/prompts.py`, `rrc/pipeline/solve.py` | `tests/pipeline/test_solve.py`, `tests/test_rrc_engine.py` | Implemented |
| Cheap IMPLEMENT receives the Spec, not raw source (§2) | `rrc/contextmesh_runtime.py`, `contextmesh/scripts/rrd_codex_hook.py` | `tests/test_rrc_contextmesh_runtime.py`, `tests/test_rrd_codex_hook.py` | Implemented through retained ContextMesh transport |
| Independent implementation-blind tests on MISS/PRIME; rendered tests on REUSE (§2) | `rrc/pipeline/solve.py`, `rrc/pipeline/template.py` | `tests/pipeline/test_solve.py`, `tests/pipeline/test_template.py` | Implemented |
| Tier-minus-one rendered-Spec sanity check (§3) | `rrc/pipeline/template.py`, `rrc/pipeline/solve.py` | `tests/pipeline/test_template.py`, `tests/test_rrc_engine.py` | Implemented; deliberately loose as designed |
| Ruff fix/format, signature conformance, Pyright basic, then pytest (§3) | `rrc/pipeline/verify.py`, `rrc/pipeline/sandbox.py` | `tests/test_rrc_verify.py`, `tests/test_rrc_sandbox_capability.py` | Implemented in the sealed verifier |
| Two cheap repairs, then a fresh strong fallback (§2-§3) | `rrc/pipeline/solve.py`, `rrc/pipeline/stages.py` | `tests/pipeline/test_solve.py`, `tests/test_rrc_engine.py` | Implemented and journalled |
| ACCEPT before template/index storage; no storage on failure (§1, §4) | `rrc/journal.py`, `rrc/pipeline/solve.py` | `tests/test_rrc_acceptance.py`, `tests/test_rrc_journal.py` | Implemented transactionally in SQLite |
| Deterministic own store keyed by content-derived external reference (§4.2-§4.3) | `rrc/journal.py`, `rrc/pipeline/template.py` | `tests/test_rrc_journal.py`, `tests/pipeline/test_template.py` | Implemented |
| EverOS is index-only; returned content is never the artifact (§4.1-§4.5) | `rrc/everos.py`, `rrc/retrieval.py` | `tests/test_rrc_everos.py`, `tests/lane_b/test_lane_b_integration.py` | Implemented as an optional retained integration |
| Offline operation without EverOS (§4 retained change) | `rrc/retrieval.py`, `rrc/journal.py`, `contextmesh/RRDdemo-local.sh` | offline smoke selection in `PLAN.md`; `tests/test_rrc_retrieval.py` | Implemented; SQLite is sufficient |
| EverOS outbox/retry and read-your-write handling (§4.7) | `rrc/journal.py`, `rrc/everos.py` | `tests/test_rrc_everos.py`, `tests/test_rrc_journal.py` | Implemented without making EverOS authoritative |
| Five comparable arms: baseline, cheap-alone, cascade, cold, warm (§6) | `rrc/workload.py`, `contextmesh/bench/run_rrcv2_bench.py` | `tests/test_rrcv2_bench.py`, `tests/test_rrc_runner.py` | Implemented in one harness |
| Repeating workload, shared oracle, hit accounting, cost per solved (§5-§6) | `contextmesh/bench/rrcv2_workload.json`, `contextmesh/bench/rrcv2_oracles.json`, `contextmesh/bench/rrcv2_analyzer.py` | `tests/test_rrcv2_workload_manifest.py`, `tests/test_rrcv2_economic_authority.py` | Implemented; live savings remain unclaimed |
| Count every model call and separate input/cache/output/reasoning (§5-§6) | `rrc/contract.py`, `rrc/journal.py`, `contextmesh/bench/run_rrcv2_bench.py` | `tests/test_rrc_contract.py`, `tests/test_rrcv2_bench.py` | Implemented with native Codex usage fields |
| Snowflake Cortex calls and credit reconciliation (§7) | Replaced by `rrc/model.py`, dispatch permits, and native Codex transcripts | capability/setup evidence and `tests/test_model_adapter.py` | Intentional native-Codex deviation; no Snowflake billing claim |
| ContextMesh root/worker hierarchy and bounded result delivery (retained change) | `rrc/contextmesh.py`, `rrc/contextmesh_runtime.py`, `rrc/cell_journal.py`, `contextmesh/scripts/rrd_codex_hook.py`, `contextmesh/scripts/rrd_result_reader.py` | `tests/test_rrc_contextmesh.py`, `tests/test_rrc_cell_journal.py`, `tests/test_rrd_result_reader.py` | Implemented |
| Native worker cannot reread already delivered source (retained change) | `contextmesh/scripts/rrd_codex_hook.py` | `tests/test_rrd_codex_hook.py`, installed fake-server tests | Implemented |
| HIT, MISS, and structural-backstop live credibility beats (§9) | `contextmesh/scripts/rrcv2_product_smoke.py`, `tests/fixtures/rrcv2_cli_smoke/` | v19/v20 attempt evidence under `.generated/state/rrcv2-convergence/verify/cli-smoke/` | Not yet demonstrated end to end; both reviewed attempts failed and are preserved |

## Explicitly quarantined compatibility surfaces

`rrc/multiagent_demo.py`, `rrc/orchestrator_contract.py`, `rrc/orchestrator_policy.py`,
`rrc/orchestrator_runtime.py`, and the generic handler-audit scripts reproduce the historical
ContextMesh audit experiment. They are not read by canonical RRCv2 retrieval or product dispatch,
and their results must be labelled non-RRCv2.
