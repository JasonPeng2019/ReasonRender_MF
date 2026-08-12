from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path

import pytest
from rrc.contract import Slots, Spec, parse_task_envelope
from rrc.pipeline.template import TemplateError, render, templatize, validate_spec_for_task

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "contextmesh/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

guard = importlib.import_module("rrcv2_product_guard")
smoke = importlib.import_module("rrcv2_product_smoke")


def _base_env(tmp_path: Path) -> dict[str, str]:
    repo = ROOT
    round_root = tmp_path / "round"
    round_root.mkdir()
    for child in ("tmp", "cancellation", "cancellation/children"):
        (round_root / child).mkdir(parents=True, exist_ok=True)
    return guard.product_environment(
        repo=repo,
        native_home=ROOT / "contextmesh/.codex-rrd-native",
        user_home=Path.home(),
        codex_bin=Path("/usr/bin/true"),
        uv_bin=Path("/usr/local/bin/uv"),
        round_root=round_root,
        producer_root=tmp_path / "producer",
        nonce="b" * 32,
    )


def test_materialized_cases_have_strict_envelopes_and_closed_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = ROOT / "tests/fixtures/rrcv2_cli_smoke/manifest.json"
    rows = guard.load_fixture(fixture)["cases"]
    assert isinstance(rows, list)
    env = _base_env(tmp_path)
    monkeypatch.setattr(smoke.subprocess, "run", lambda *args, **kwargs: None)
    for row in rows:
        assert isinstance(row, dict)
        cell = tmp_path / "round" / str(row["kind"])
        cell.mkdir()
        monkeypatch.setenv("RRC_DEMO_UV_BIN", env["RRC_DEMO_UV_BIN"])
        task, prompt, marker = smoke._materialize_case(  # noqa: SLF001
            fixture_root=fixture.parent,
            row=row,
            cell=cell,
            repo=ROOT,
        )
        envelope = parse_task_envelope((cell / "task-envelope.v1.json").read_bytes())
        assert envelope.task == task
        assert marker.encode() in prompt
        assert b"Spawn exactly one worker" in prompt
        assert (
            hashlib.sha256((cell / "target" / task.artifact_path).read_bytes()).hexdigest()
            == row["files"]["starter.py"]["sha256"]
        )


def test_identifier_to_string_context_backstop_and_fresh_spec_are_both_valid() -> None:
    miss_task = smoke._task(  # noqa: SLF001
        guard.load_fixture(ROOT / "tests/fixtures/rrcv2_cli_smoke/manifest.json")["cases"][0]
    )
    near_task = smoke._task(  # noqa: SLF001
        guard.load_fixture(ROOT / "tests/fixtures/rrcv2_cli_smoke/manifest.json")["cases"][2]
    )
    miss_spec = Spec(
        "Implement the User label method.",
        "class User:\n    def label(self) -> str: ...",
        "User.label returns the string User.",
        ('def test_label():\n    assert User().label() == "User"',),
        Slots(entity="User"),
    )
    assert validate_spec_for_task(miss_spec, miss_task)
    cached = templatize(
        miss_spec,
        ('def test_user_label():\n    assert User().label() == "User"',),
        slot_values=miss_task.slot_values,
        primary=miss_task.primary,
    )
    assert cached.slot_contexts == (("entity", ("identifier", "string_content", "text")),)
    with pytest.raises(TemplateError, match="identifier-context"):
        render(cached, dict(near_task.slot_values or ()))
    fresh = Spec(
        "Implement label_entity for order item.",
        "def label_entity() -> str: ...",
        "label_entity returns the string order item.",
        ('def test_label_entity():\n    assert label_entity() == "order item"',),
        Slots(entity="order item"),
    )
    assert validate_spec_for_task(fresh, near_task)
    fresh_template = templatize(
        fresh,
        near_task.public_tests,
        slot_values=near_task.slot_values,
        primary=near_task.primary,
    )
    assert fresh_template.slot_contexts == (("entity", ("string_content", "text")),)
    assert render(fresh_template, dict(near_task.slot_values or ()))[0] == fresh
