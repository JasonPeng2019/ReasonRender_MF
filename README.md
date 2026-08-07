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

These checks validate the repository itself. The deliberately small ship-it-fast generated-code
verifier runs pytest in a temporary subprocess with a timeout; Ruff and Pyright stages are deferred.

Lane A's frozen public call is `solve(task, *, mode, model, retrieval, cfg)`. It supports COLD and
WARM, emits one `CostEvent` per model call, stores only canonical generic templates after WARM
success, and exposes persistence failures as `StoreFailure` with the completed outcome attached.
See [ADR 0001](docs/decisions/0001-rrcv2-full-two-store-contract.md).

## Layout

```text
rrc/
  contract.py       Frozen interface shared by Lane A and Lane B
  pipeline/         Lane A solve, model-stage, template, and verification modules
tests/
  pipeline/         Offline Lane A tests and fixtures
docs/               RRCv2 design and lane build sheets
```
