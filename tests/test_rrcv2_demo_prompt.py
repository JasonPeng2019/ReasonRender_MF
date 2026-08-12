from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from contextmesh.scripts.rrcv2_demo_prompt import render
from rrc.contextmesh import coding_assignment_from_message
from rrc.contract import parse_task_envelope


def test_demo_prompt_materializes_one_general_source_referencing_assignment(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[1]
    target = tmp_path / "target"
    target.mkdir()

    envelope_path = tmp_path / "task-envelope.json"
    prompt = render(
        repository=repository,
        target=target,
        mode="cold",
        root_sentinel="root-private",
        parent_history_sentinel="parent-private",
        task_envelope_output=envelope_path,
    )

    marker_lines = [
        line for line in prompt.splitlines() if line.startswith("RRCV2_CODING_ASSIGNMENT_V1:")
    ]
    assert len(marker_lines) == 1
    task_ids: list[str] = []
    for marker in marker_lines:
        assignment = coding_assignment_from_message(marker)
        assert assignment is not None
        task_ids.append(assignment.task.task_id)
        assert assignment.mode == "cold"
        assert isinstance(assignment.source_path, str)
        assert assignment.source_path == assignment.task.artifact_path
        assert assignment.task.public_tests == ()
        assert assignment.task.oracle_tests == ()
        assert (target / assignment.source_path).is_file()
        assert (target / assignment.public_test_path).is_file()
        assert assignment.oracle_test_path is not None
        assert (target / assignment.oracle_test_path).is_file()
    assert task_ids == ["rrcv2-general-demo-001"]
    envelope = parse_task_envelope(envelope_path.read_bytes())
    assert envelope.task.task_id == task_ids[0]
    assert envelope.task.verification_profile == "rrcv2_general_v1"
    assert "Root-only source sentinel: root-private" in prompt
    assert "Root-only parent-history sentinel: parent-private" in prompt


def test_demo_prompt_reseals_the_current_target_without_reverting_an_applied_result(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[1]
    target = tmp_path / "target"
    target.mkdir()
    first = render(
        repository=repository,
        target=target,
        mode="cold",
        root_sentinel="root-private",
        parent_history_sentinel="parent-private",
    )
    assignment = coding_assignment_from_message(
        next(line for line in first.splitlines() if line.startswith("RRCV2_CODING_ASSIGNMENT_V1:"))
    )
    assert assignment is not None and assignment.source_path is not None
    applied = b"def accepted() -> int:\n    return 7"
    source = target / assignment.source_path
    source.write_bytes(applied)
    source.chmod(0o644)

    second = render(
        repository=repository,
        target=target,
        mode="warm",
        root_sentinel="root-private-2",
        parent_history_sentinel="parent-private-2",
    )
    resealed = coding_assignment_from_message(
        next(line for line in second.splitlines() if line.startswith("RRCV2_CODING_ASSIGNMENT_V1:"))
    )

    assert resealed is not None
    assert source.read_bytes() == applied
    assert resealed.target_preimage.sha256 == hashlib.sha256(applied).hexdigest()
    assert resealed.target_preimage.bytes == len(applied)
    assert "Spawn exactly one native worker" in second


def test_demo_prompt_rejects_mutated_or_special_test_authority(tmp_path: Path) -> None:
    repository = Path(__file__).parents[1]
    target = tmp_path / "target"
    target.mkdir()
    prompt = render(
        repository=repository,
        target=target,
        mode="cold",
        root_sentinel="root-private",
        parent_history_sentinel="parent-private",
    )
    assignment = coding_assignment_from_message(
        next(line for line in prompt.splitlines() if line.startswith("RRCV2_CODING_ASSIGNMENT_V1:"))
    )
    assert assignment is not None
    public = target / assignment.public_test_path
    public.write_bytes(b"foreign")
    public.chmod(0o600)
    with pytest.raises(ValueError, match="authority differs"):
        render(
            repository=repository,
            target=target,
            mode="cold",
            root_sentinel="root-private",
            parent_history_sentinel="parent-private",
        )
