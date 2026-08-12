from dataclasses import replace

import rrc.pipeline.sandbox as sandbox_module
from rrc.pipeline.sandbox import (
    CommandObservation,
    SandboxInvocationEvidenceV1,
    SandboxResultV1,
    SandboxTierExecutionV1,
)
from rrc.pipeline.verify import run_pytest


class CompatibilitySandbox:
    def __init__(self, *, passed: bool = True) -> None:
        self.passed = passed

    def run(  # type: ignore[no-untyped-def]
        self,
        *,
        tier,
        verification_profile,
        artifact_path,
        source,
        tests,
        selected_node_ids=(),
        limits,
    ):
        result: SandboxResultV1
        if tier == "ruff":
            result = SandboxResultV1("ruff", 0, b"", b"", normalized_source=source)
        elif tier == "pyright":
            result = SandboxResultV1("pyright", 0, b"passed\n", b"")
        else:
            nodes = tuple(f"test_{index}.py::test_value" for index in range(len(tests)))
            if tier == "pytest_collect":
                result = SandboxResultV1("pytest_collect", 0, b"", b"", collected_node_ids=nodes)
            else:
                result = SandboxResultV1(
                    "pytest",
                    0 if self.passed else 1,
                    b"passed\n" if self.passed else b"FAILED test_value\n",
                    b"",
                    collected_node_ids=nodes,
                    completed_node_ids=nodes if self.passed else (),
                )
        argv = ("fixture-sandbox", result.tier)
        identity = "1" * 64
        return replace(
            result,
            invocations=(CommandObservation(result.exit_code, result.stdout, result.stderr, argv),),
            execution_evidence=SandboxTierExecutionV1(
                tier=result.tier,
                backend_before_sha256=identity,
                backend_after_sha256=identity,
                capability_sha256=sandbox_module.CAPABILITY_SHA256,
                runtime_lock_sha256=sandbox_module.RUNTIME_LOCK_SHA256,
                image_config_digest=sandbox_module.IMAGE_CONFIG_DIGEST,
                invocations=(
                    SandboxInvocationEvidenceV1(
                        argv_sha256=sandbox_module._sha(sandbox_module._canonical(list(argv))),
                        exit_code=result.exit_code,
                        stdout_sha256=sandbox_module._sha(result.stdout),
                        stderr_sha256=sandbox_module._sha(result.stderr),
                    ),
                ),
                normalized_source_sha256=(
                    sandbox_module._sha(result.normalized_source)
                    if result.normalized_source is not None
                    else None
                ),
            ),
        )


def test_run_pytest_executes_code_and_tests_together() -> None:
    code = "def double(value: int) -> int:\n    return value * 2"
    tests = "def test_double():\n    assert double(3) == 6"

    passed, output = run_pytest(code, tests, sandbox=CompatibilitySandbox())

    assert passed is True, output


def test_run_pytest_accepts_multiple_test_artifacts() -> None:
    code = "def identity(value: int) -> int:\n    return value"
    tests = (
        "def test_one(): assert identity(1) == 1",
        "def test_two(): assert identity(2) == 2",
    )
    passed, output = run_pytest(code, tests, sandbox=CompatibilitySandbox())
    assert passed is True, output


def test_run_pytest_returns_failure_output() -> None:
    code = "def double(value: int) -> int:\n    return value"
    tests = "def test_double():\n    assert double(3) == 6"

    passed, output = run_pytest(code, tests, sandbox=CompatibilitySandbox(passed=False))

    assert passed is False
    assert "FAILED" in output


def test_run_pytest_times_out_hung_code() -> None:
    passed, output = run_pytest("while True:\n    pass", "def test_never(): pass", timeout=0.1)

    assert passed is False
    assert "unsupported pytest timeout" in output.lower()
