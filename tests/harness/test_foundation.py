from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from harness.packet import PacketValidationError, validate_packet
from harness.workspace import (
    create_module_artifact_dir,
    materialize_worker_workspace,
    materialize_workspace,
)

PACKET_ID = "sha256:foundation"
ALLOWED_PATHS = {"harness/packet.py", "tests/harness/test_foundation.py"}


def packet(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "packet_id": PACKET_ID,
        "module": "M0-shared-foundation",
        "round_id": None,
        "required_slots": ["module", "round_id"],
        "slot_values": {"module": "M0-shared-foundation", "round_id": None},
        "owned_paths": ["harness/packet.py"],
    }
    value.update(changes)
    return value


def test_null_slot_is_valid_and_result_is_detached() -> None:
    source = packet()
    result = validate_packet(source, PACKET_ID, ALLOWED_PATHS)

    assert result["slot_values"]["round_id"] is None  # type: ignore[index]
    source["owned_paths"] = ["tests/harness/test_foundation.py"]
    assert result["owned_paths"] == ["harness/packet.py"]


def test_missing_required_slot_is_explicit() -> None:
    value = packet(slot_values={"module": "M0-shared-foundation"})

    with pytest.raises(PacketValidationError, match="missing required slot: round_id"):
        validate_packet(value, PACKET_ID, ALLOWED_PATHS)


def test_wrong_packet_id_is_explicit() -> None:
    with pytest.raises(PacketValidationError, match="expected packet id"):
        validate_packet(packet(packet_id="sha256:wrong"), PACKET_ID, ALLOWED_PATHS)


@pytest.mark.parametrize("path", ["../outside.py", "/outside.py", "tests/other.py"])
def test_declared_path_must_be_safe_and_allowed(path: str) -> None:
    with pytest.raises(PacketValidationError):
        validate_packet(packet(owned_paths=[path]), PACKET_ID, ALLOWED_PATHS)


@pytest.mark.parametrize("malformed", [None, [], {"packet_id": PACKET_ID}, {"packet_id": PACKET_ID, "required_slots": "module"}])
def test_malformed_fixture_is_rejected(malformed: object) -> None:
    with pytest.raises(PacketValidationError):
        validate_packet(malformed, PACKET_ID, ALLOWED_PATHS)


def test_materialized_target_is_isolated_and_artifacts_are_writable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.txt").write_text("source\n", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / "value.txt").write_text("value\n", encoding="utf-8")
    target = tmp_path / "target"

    materialize_workspace(source, target)
    (target / "config.txt").write_text("target\n", encoding="utf-8")
    assert (source / "config.txt").read_text(encoding="utf-8") == "source\n"
    assert (target / "nested" / "value.txt").read_text(encoding="utf-8") == "value\n"

    nested_artifact = create_module_artifact_dir(tmp_path / "artifacts", "module/nested")
    assert nested_artifact.is_dir()

    artifact = create_module_artifact_dir(tmp_path / "run", "M0-shared-foundation")
    artifact_file = artifact / "result.json"
    artifact_file.write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")
    assert json.loads(artifact_file.read_text(encoding="utf-8")) == {"ok": True}


def test_worker_workspace_exposes_only_declared_raw_source_and_keeps_runtime_importable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    package = source / "fixturepkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "raw.py").write_text("VALUE = 'raw'\n", encoding="utf-8")
    (package / "hidden.py").write_text("VALUE = 'hidden'\n", encoding="utf-8")
    target = tmp_path / "target"

    materialize_worker_workspace(
        source,
        target,
        raw_source_paths=("fixturepkg/raw.py",),
        owned_write_paths=("fixturepkg/new_rule.py", "tests/test_new_rule.py"),
        python_executable=shutil.which("python"),
    )

    assert (target / "fixturepkg" / "raw.py").is_file()
    assert not (target / "fixturepkg" / "hidden.py").exists()
    assert (target / "fixturepkg" / "hidden.pyc").is_file()
    completed = subprocess.run(
        [shutil.which("python") or sys.executable, "-c", "from fixturepkg.hidden import VALUE; assert VALUE == 'hidden'"],
        cwd=target,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
