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

Pyright runs in `basic` mode, matching the RRCv2 verification contract. The generated-code verifier
will run Ruff formatting and auto-fixes before Pyright and pytest.

## Layout

```text
rrc/
  contract.py       Frozen interface shared by Lane A and Lane B
  pipeline/         Lane A solve, model-stage, template, and verification modules
tests/
  pipeline/         Offline Lane A tests and fixtures
docs/               RRCv2 design and lane build sheets
```
