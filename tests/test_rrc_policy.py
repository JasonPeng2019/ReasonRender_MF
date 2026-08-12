from __future__ import annotations

import pytest
from rrc.policy import PolicyKind, SyntheticPolicyError, validate_synthetic_source


def test_candidate_positive_language_accepts_representative_facades_and_control_flow() -> None:
    source = """def solve(values: list[int]) -> int:
    cleaned = sorted(values)
    total = 0
    for value in cleaned:
        if value < 0:
            continue
        total += math.isqrt(value)
    return total
"""
    result = validate_synthetic_source(source, kind=PolicyKind.CANDIDATE)
    assert result.declared_functions == ("solve",)


@pytest.mark.parametrize(
    "source",
    [
        "import os\n",
        "def f(x: int) -> int:\n    return open('x')\n",
        "def f(x: int) -> int:\n    return x.__class__\n",
        "def f(x: int) -> int:\n    return json.codecs.loads(x)\n",
        "def f() -> str:\n    return json.codecs.sys.modules['os'].environ.get('HOME', '')\n",
        "def f():\n    return json\n",
        "def f(x: int) -> int:\n    return getattr(x, 'value')\n",
        "def f(x: int) -> int:\n    return (lambda: x)()\n",
        "class F:\n    pass\n",
        "def _f(x: int) -> int:\n    return x\n",
        "def f(x: int) -> int:\n    raise RuntimeError('x')\n",
        "def f(x=open('/etc/passwd')):\n    return x\n",
        "def f(x=__import__('os').system('id')):\n    return x\n",
        "def f(x: (lambda: int)()) -> int:\n    return 1\n",
        "def f(x: open('x')) -> int:\n    return 1\n",
    ],
)
def test_candidate_policy_rejects_hostile_capabilities(source: str) -> None:
    with pytest.raises(SyntheticPolicyError):
        validate_synthetic_source(source, kind="candidate")


def test_test_policy_accepts_one_positive_target_test() -> None:
    source = """def test_value():
    assert clamp_value(11, 0, 10) == 10
"""
    validate_synthetic_source(source, kind="test", target="clamp_value")


def test_test_policy_accepts_one_immediate_exception_expectation() -> None:
    source = """def test_value():
    try:
        parse_value("bad")
    except ValueError as exc:
        assert exc.args == ("bad",)
    else:
        raise AssertionError("ValueError required")
"""
    validate_synthetic_source(source, kind="test", target="parse_value")


@pytest.mark.parametrize(
    "source",
    [
        "def helper():\n    return 1\n",
        "def test_a():\n    assert other() == 1\n",
        "def test_a():\n    assert f() == 1\n\ndef test_b():\n    assert f() == 1\n",
        "pytest_plugins = ['evil']\ndef test_a():\n    assert f() == 1\n",
        "def test_a():\n    return f()\n",
        "def test_a():\n    for value in [1]:\n        assert f() == value\n",
        "def test_a():\n    while True:\n        assert f() == 1\n",
        "def test_a():\n    assert 1 == 1\n",
        "def test_a():\n    assert True or f()\n",
        "def test_a():\n    assert all(f(x) for x in [])\n",
        "def test_a():\n    assert [f(x) for x in []] == []\n",
        "def test_a():\n    assert {f(x) for x in []} == set()\n",
        "def test_a():\n    assert {x: f(x) for x in []} == {}\n",
        "def test_a():\n    try:\n        raise ValueError('x')\n        f()\n    except ValueError:\n        pass\n    else:\n        raise AssertionError('expected')\n",
        "def test_a():\n    try:\n        raise ValueError('x')\n    except ValueError:\n        pass\n    else:\n        f()\n",
    ],
)
def test_test_policy_rejects_noncanonical_modules(source: str) -> None:
    with pytest.raises(SyntheticPolicyError):
        validate_synthetic_source(source, kind="test", target="f")


def test_policy_rejects_non_nfc_and_unknown_kind() -> None:
    with pytest.raises(SyntheticPolicyError):
        validate_synthetic_source("def f():\n    return 'e\u0301'\n", kind="candidate")
    with pytest.raises(SyntheticPolicyError):
        validate_synthetic_source("def f():\n    return 1\n", kind="general")
