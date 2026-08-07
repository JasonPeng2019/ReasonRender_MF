from rrc.pipeline.verify import run_pytest


def test_run_pytest_executes_code_and_tests_together() -> None:
    code = "def double(value: int) -> int:\n    return value * 2"
    tests = "def test_double():\n    assert double(3) == 6"

    passed, output = run_pytest(code, tests)

    assert passed is True, output


def test_run_pytest_accepts_multiple_test_artifacts() -> None:
    code = "def identity(value: int) -> int:\n    return value"
    tests = (
        "def test_one(): assert identity(1) == 1",
        "def test_two(): assert identity(2) == 2",
    )
    passed, output = run_pytest(code, tests)
    assert passed is True, output


def test_run_pytest_returns_failure_output() -> None:
    code = "def double(value: int) -> int:\n    return value"
    tests = "def test_double():\n    assert double(3) == 6"

    passed, output = run_pytest(code, tests)

    assert passed is False
    assert "FAILED" in output


def test_run_pytest_times_out_hung_code() -> None:
    passed, output = run_pytest("while True:\n    pass", "def test_never(): pass", timeout=0.1)

    assert passed is False
    assert "timed out" in output.lower()
