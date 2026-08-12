# ReasonRender for Code v2

ReasonRender for Code v2 amortizes an expensive generated specification across a repeating coding
workload. This repository contains the frozen shared contract, the Lane A solve pipeline, and the
Lane B memory, measurement, and evaluation integrations described in [`docs/RRCv2.md`](docs/RRCv2.md).

## Development setup

The project targets Python 3.11 or newer and uses `uv` for environment and dependency management.

```bash
uv sync --dev
```

Run the project quality checks with:

```bash
uv run ruff format --check rrc tests
uv run ruff check rrc tests
uv run pyright
uv run pytest
```

These checks validate the repository itself. Candidate code uses the sealed RRCv2 verifier: ordered
assembly, pinned Ruff normalization, signature conformance where the Spec supplies a signature,
Pyright basic, and pytest collection/execution inside the capability-probed sandbox. Verification
evidence is content-bound and fail-closed; it is not the earlier temporary-subprocess verifier.

The canonical pipeline supports COLD and WARM, classifies retrieval as EXACT, NEAR, or MISS, and
runs REUSE, PRIME, or a fresh strong SPEC before small-model implementation. It records one
`CostEvent` per model call, performs two bounded repairs and a fresh strong fallback when needed,
and stores templates/index rows only after acceptance. SQLite is authoritative and works offline;
EverOS is optional. ContextMesh transports exact source/Spec inputs to source-blind native Codex
workers without duplicate source delivery. See the current
[ADR 0002](docs/decisions/0002-rrcv2-full-contextmesh-profile.md), the
[requirement map](docs/rrcv2-requirement-map.md), and the
[convergence report](docs/rrcv2-convergence-report.md). ADR 0001 is superseded and retained only as
a historical fixture.

## Layout

```text
rrc/
  contract.py       Frozen interface shared by Lane A and Lane B
  pipeline/         Solve, model-stage, template, and sealed verification modules
  retrieval.py      EXACT/NEAR/MISS local retrieval and classification
  journal.py        Durable attempts, calls, evidence, acceptance, and local index
  contextmesh*.py   Exact-context native-worker transport
tests/
  pipeline/         Offline Lane A tests and fixtures
docs/               RRCv2 design and lane build sheets
```
