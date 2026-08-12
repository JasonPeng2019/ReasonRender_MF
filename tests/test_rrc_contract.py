from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from rrc.contract import (
    ArtifactRefV1,
    InlineTaskInputV1,
    ReferencedTaskInputV1,
    ScoreV1,
    SealedTaskMaterialsV1,
    StructuralShapeV1,
    TargetPreimageV1,
    Task,
    canonical_json_bytes,
    canonical_test_artifact_bytes,
    parse_task_envelope,
    reopen_task_inputs,
    seal_task_input,
    task_envelope_bytes,
    task_envelope_projection_bytes,
)

PUBLIC = "def test_public():\n    assert f(1) == 1"
ORACLE = "def test_oracle():\n    assert f(2) == 2"
SOURCE = "def f(x: int) -> int:\n    return x"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _golden_task(**changes: object) -> Task:
    values: dict[str, object] = {
        "task_id": "golden-general",
        "text": "Return x.",
        "oracle_tests": (ORACLE,),
        "public_tests": (PUBLIC,),
    }
    values.update(changes)
    return Task(**values)  # type: ignore[arg-type]


def test_score_v1_is_reduced_exact_and_rejects_bool_float_or_unreduced() -> None:
    assert ScoreV1(7, 20).as_json() == {"denominator": 20, "numerator": 7}
    assert ScoreV1(0, 1) < ScoreV1(7, 20) < ScoreV1(1, 1)
    for args in ((35, 100), (True, 2), (1, False), (1.0, 2), (-1, 2), (3, 2), (0, 2)):
        with pytest.raises((TypeError, ValueError)):
            ScoreV1(*args)  # type: ignore[arg-type]


def test_config_accepts_the_frozen_top_k_range_only() -> None:
    from rrc.contract import Config

    assert Config("owner", top_k=1).top_k == 1
    assert Config("owner", top_k=100).top_k == 100
    for value in (True, 0, 101):
        with pytest.raises(ValueError, match="top_k"):
            Config("owner", top_k=value)  # type: ignore[arg-type]


def test_final_task_surface_uses_immutable_test_and_slot_tuples() -> None:
    shape = StructuralShapeV1(("int",), 1, ("field",))
    task = Task(
        "task-1",
        "Implement it.",
        oracle_tests=(ORACLE,),
        family="family-1",
        public_tests=(PUBLIC,),
        searchable_public=True,
        verification_profile="rrcv2_synthetic_v1",
        primary="f",
        shape=shape,
        slot_values=(("function", "f"),),
    )
    assert task.artifact_path == "solution.py"
    assert task.oracle_tests == (ORACLE,)
    assert task.public_tests == (PUBLIC,)
    with pytest.raises(TypeError, match="oracle_tests"):
        Task("bad", "Bad.", oracle_tests=ORACLE)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sorted unique"):
        Task("bad", "Bad.", slot_values=(("z", "one"), ("a", "two")))
    for alias in ("a//b.py", "a/./b.py", "a/../b.py", ".rrcv2/source.py"):
        with pytest.raises(ValueError, match="confined relative POSIX"):
            Task("bad", "Bad.", artifact_path=alias)


def test_public_and_oracle_artifacts_match_the_frozen_goldens() -> None:
    public = canonical_test_artifact_bytes((PUBLIC,))
    oracle = canonical_test_artifact_bytes((ORACLE,))
    assert public == b'{"tests":["def test_public():\\n    assert f(1) == 1"],"v":1}'
    assert oracle == b'{"tests":["def test_oracle():\\n    assert f(2) == 2"],"v":1}'
    assert _sha(public) == "b824c5f54d5c011724e7756a54a84c66c861a9c0028e839cfa6384426b22d5a0"
    assert _sha(oracle) == "a7f79b175f9d7af4655373eae8cdef925cab24afd048650e92096b7bd9b0b549"


def test_inline_and_referenced_sealing_share_the_frozen_projection(
    tmp_path: Path,
) -> None:
    inline_root = tmp_path / "inline"
    inline = seal_task_input(
        InlineTaskInputV1(
            task=_golden_task(),
            starter_source=SOURCE,
            target_preimage=TargetPreimageV1.none(),
        ),
        input_root=inline_root,
    )
    projection = task_envelope_projection_bytes(inline)
    expected = (
        b'{"oracle_ref":{"bytes":60,"path":".rrcv2/oracle-tests.v1.json",'
        b'"sha256":"a7f79b175f9d7af4655373eae8cdef925cab24afd048650e92096b7bd9b0b549"},'
        b'"public_test_ref":{"bytes":60,"path":".rrcv2/public-tests.v1.json",'
        b'"sha256":"b824c5f54d5c011724e7756a54a84c66c861a9c0028e839cfa6384426b22d5a0"},'
        b'"shape":null,"slot_values":null,"source_ref":{"bytes":34,"path":"solution.py",'
        b'"sha256":"711f53c43b68fe40fe05acc05e555d7d77f288187b25cfaf846f41369e5e560e"},'
        b'"task":{"artifact_path":"solution.py","family":null,"primary":null,'
        b'"searchable_public":false,"task_id":"golden-general","text":"Return x.",'
        b'"verification_profile":"rrcv2_general_v1"},"v":1}'
    )
    assert projection == expected
    assert _sha(projection) == "5ff1dfb0ed2b63255f79dff36c2757de8e4ed8f62df2e12da8db9fbd5c707174"
    assert inline.target_preimage == TargetPreimageV1.none()
    assert inline_root.stat().st_mode & 0o777 == 0o700

    sealed = tmp_path / "sealed"
    (sealed / ".rrcv2").mkdir(parents=True)
    (sealed / "solution.py").write_text(SOURCE, encoding="utf-8")
    public_path = sealed / ".rrcv2/public-tests.v1.json"
    oracle_path = sealed / ".rrcv2/oracle-tests.v1.json"
    public_path.write_bytes(canonical_test_artifact_bytes((PUBLIC,)))
    oracle_path.write_bytes(canonical_test_artifact_bytes((ORACLE,)))
    source_ref = ArtifactRefV1(_sha(SOURCE.encode()), len(SOURCE.encode()), "solution.py")
    public_ref = ArtifactRefV1(
        _sha(public_path.read_bytes()), public_path.stat().st_size, ".rrcv2/public-tests.v1.json"
    )
    oracle_ref = ArtifactRefV1(
        _sha(oracle_path.read_bytes()), oracle_path.stat().st_size, ".rrcv2/oracle-tests.v1.json"
    )
    referenced = seal_task_input(
        ReferencedTaskInputV1(
            task=_golden_task(oracle_tests=(), public_tests=()),
            sealed_root=sealed,
            source_ref=source_ref,
            public_test_ref=public_ref,
            oracle_ref=oracle_ref,
            target_preimage=TargetPreimageV1.regular(
                "solution.py", source_ref.sha256, source_ref.bytes, 0o644
            ),
        ),
        input_root=tmp_path / "referenced",
    )
    assert task_envelope_projection_bytes(referenced) == projection
    assert referenced.target_preimage.kind == "regular"


def test_sealing_rejects_aliases_symlinks_and_mismatches_before_output(
    tmp_path: Path,
) -> None:
    sealed = tmp_path / "sealed"
    (sealed / ".rrcv2").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text(SOURCE)
    os.symlink(outside, sealed / "solution.py")
    public = canonical_test_artifact_bytes((PUBLIC,))
    (sealed / ".rrcv2/public-tests.v1.json").write_bytes(public)
    authority = ReferencedTaskInputV1(
        task=_golden_task(oracle_tests=(), public_tests=()),
        sealed_root=sealed,
        source_ref=ArtifactRefV1(_sha(SOURCE.encode()), len(SOURCE.encode()), "solution.py"),
        public_test_ref=ArtifactRefV1(_sha(public), len(public), ".rrcv2/public-tests.v1.json"),
        oracle_ref=None,
        target_preimage=TargetPreimageV1.regular(
            "solution.py", _sha(SOURCE.encode()), len(SOURCE.encode()), 0o644
        ),
    )
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="regular confined file"):
        seal_task_input(authority, input_root=output)
    assert not output.exists()


def test_canonical_json_rejects_float_and_non_nfc() -> None:
    with pytest.raises(TypeError, match="float"):
        canonical_json_bytes({"v": 1.0})
    with pytest.raises(ValueError, match="NFC"):
        canonical_json_bytes({"value": "e\u0301"})


def test_synthetic_task_requires_exact_final_markers_and_matching_metadata(
    tmp_path: Path,
) -> None:
    shape = StructuralShapeV1(("int",), 1, ())
    slots = (("function", "bounded_value"), ("high", "9"), ("low", "1"))
    body = "Implement the bounded value function."
    text = (
        body
        + '\nRRC_SHAPE: {"arg_types":["int"],"arity":1,"fields":[]}'
        + '\nRRC_SLOT_VALUES: {"function":"bounded_value","high":"9","low":"1"}'
    )
    task = Task(
        "synthetic-1",
        text,
        family="bounded",
        public_tests=(PUBLIC,),
        verification_profile="rrcv2_synthetic_v1",
        primary="bounded_value",
        shape=shape,
        slot_values=slots,
    )
    sealed = seal_task_input(
        InlineTaskInputV1(task, SOURCE, TargetPreimageV1.none()),
        input_root=tmp_path / "valid",
    )
    assert sealed.shape == shape
    assert sealed.slot_values == slots

    mutations = (
        text + "\n",
        text.replace('"arity":1', '"arity":0'),
        text.replace('"high":"9","low":"1"', '"low":"1","high":"9"'),
        text.replace("Implement", "RRC_SHAPE: {}\nImplement", 1),
    )
    for index, mutated in enumerate(mutations):
        with pytest.raises(ValueError, match="synthetic"):
            seal_task_input(
                InlineTaskInputV1(
                    Task(
                        f"synthetic-bad-{index}",
                        mutated,
                        family="bounded",
                        public_tests=(PUBLIC,),
                        verification_profile="rrcv2_synthetic_v1",
                        primary="bounded_value",
                        shape=shape,
                        slot_values=slots,
                    ),
                    SOURCE,
                    TargetPreimageV1.none(),
                ),
                input_root=tmp_path / f"bad-{index}",
            )


def test_general_metadata_and_primary_rules_fail_before_output(tmp_path: Path) -> None:
    shape = StructuralShapeV1(("int",), 1, ())
    with pytest.raises(ValueError, match="keyword"):
        _golden_task(primary="class")
    invalid = (
        _golden_task(slot_values=(("function", "f"),)),
        _golden_task(shape=shape, slot_values=(("function", "f"),), primary="g"),
        _golden_task(text="Body\nRRC_SHAPE: {}"),
    )
    for index, task in enumerate(invalid):
        with pytest.raises(ValueError):
            seal_task_input(
                InlineTaskInputV1(task, SOURCE, TargetPreimageV1.none()),
                input_root=tmp_path / f"invalid-general-{index}",
            )


def test_referenced_greenfield_requires_existing_real_parents_and_absent_target(
    tmp_path: Path,
) -> None:
    sealed = tmp_path / "sealed"
    (sealed / ".rrcv2").mkdir(parents=True)
    (sealed / "pkg").mkdir()
    public = canonical_test_artifact_bytes((PUBLIC,))
    (sealed / ".rrcv2/public-tests.v1.json").write_bytes(public)
    task = _golden_task(artifact_path="pkg/new_solution.py", oracle_tests=(), public_tests=())
    authority = ReferencedTaskInputV1(
        task=task,
        sealed_root=sealed.resolve(),
        source_ref=None,
        public_test_ref=ArtifactRefV1(_sha(public), len(public), ".rrcv2/public-tests.v1.json"),
        oracle_ref=None,
        target_preimage=TargetPreimageV1.absent("pkg/new_solution.py"),
    )
    envelope = seal_task_input(authority, input_root=tmp_path / "greenfield")
    assert envelope.source_ref is None
    assert envelope.target_preimage == TargetPreimageV1.absent("pkg/new_solution.py")

    missing_parent = ReferencedTaskInputV1(
        task=Task(
            "missing-parent",
            "Create it.",
            artifact_path="missing/new_solution.py",
        ),
        sealed_root=sealed.resolve(),
        source_ref=None,
        public_test_ref=authority.public_test_ref,
        oracle_ref=None,
        target_preimage=TargetPreimageV1.absent("missing/new_solution.py"),
    )
    with pytest.raises(ValueError, match="parent"):
        seal_task_input(missing_parent, input_root=tmp_path / "missing-output")
    assert not (tmp_path / "missing-output").exists()


def test_task_envelope_parser_rejects_unknown_alias_and_mutated_authority(
    tmp_path: Path,
) -> None:
    envelope = seal_task_input(
        InlineTaskInputV1(_golden_task(), SOURCE, TargetPreimageV1.none()),
        input_root=tmp_path / "sealed",
    )
    raw = task_envelope_bytes(envelope)
    assert parse_task_envelope(raw) == envelope

    value = json.loads(raw)
    value["unknown"] = True
    with pytest.raises(ValueError, match="schema"):
        parse_task_envelope(canonical_json_bytes(value))

    value = json.loads(raw)
    value["source_ref"]["path"] = "other.py"
    with pytest.raises(ValueError, match="source"):
        parse_task_envelope(canonical_json_bytes(value))

    value = json.loads(raw)
    assert envelope.source_ref is not None
    value["target_preimage"] = {
        "kind": "regular",
        "v": 1,
        "path": "solution.py",
        "sha256": envelope.source_ref.sha256,
        "bytes": envelope.source_ref.bytes,
        "mode": 420,
    }
    parsed = parse_task_envelope(canonical_json_bytes(value))
    assert parsed.target_preimage.kind == "regular"


def test_referenced_ref_swap_and_mode_or_hash_drift_are_rejected(tmp_path: Path) -> None:
    sealed = tmp_path / "sealed"
    (sealed / ".rrcv2").mkdir(parents=True)
    (sealed / "solution.py").write_text(SOURCE, encoding="utf-8")
    os.chmod(sealed / "solution.py", 0o640)
    public = canonical_test_artifact_bytes((PUBLIC,))
    oracle = canonical_test_artifact_bytes((ORACLE,))
    (sealed / ".rrcv2/public-tests.v1.json").write_bytes(public)
    (sealed / ".rrcv2/oracle-tests.v1.json").write_bytes(oracle)
    source_ref = ArtifactRefV1(_sha(SOURCE.encode()), len(SOURCE.encode()), "solution.py")
    public_ref = ArtifactRefV1(_sha(public), len(public), ".rrcv2/public-tests.v1.json")
    oracle_ref = ArtifactRefV1(_sha(oracle), len(oracle), ".rrcv2/oracle-tests.v1.json")

    def authority(**changes: object) -> ReferencedTaskInputV1:
        values: dict[str, object] = {
            "task": _golden_task(oracle_tests=(), public_tests=()),
            "sealed_root": sealed.resolve(),
            "source_ref": source_ref,
            "public_test_ref": public_ref,
            "oracle_ref": oracle_ref,
            "target_preimage": TargetPreimageV1.regular(
                "solution.py", source_ref.sha256, source_ref.bytes, 0o640
            ),
        }
        values.update(changes)
        return ReferencedTaskInputV1(**values)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="public test path"):
        seal_task_input(
            authority(public_test_ref=oracle_ref, oracle_ref=public_ref),
            input_root=tmp_path / "swapped",
        )
    with pytest.raises(ValueError, match="mode"):
        seal_task_input(
            authority(
                target_preimage=TargetPreimageV1.regular(
                    "solution.py", source_ref.sha256, source_ref.bytes, 0o644
                )
            ),
            input_root=tmp_path / "mode",
        )
    with pytest.raises(ValueError, match="match"):
        seal_task_input(
            authority(
                target_preimage=TargetPreimageV1.regular(
                    "solution.py", "f" * 64, source_ref.bytes, 0o640
                )
            ),
            input_root=tmp_path / "hash",
        )


def test_sealed_envelope_reopens_only_its_exact_owned_materials(tmp_path: Path) -> None:
    root = tmp_path / "input"
    envelope = seal_task_input(
        InlineTaskInputV1(_golden_task(), SOURCE, TargetPreimageV1.none()),
        input_root=root,
    )
    assert envelope.input_root == root
    assert reopen_task_inputs(envelope) == SealedTaskMaterialsV1(
        source=SOURCE,
        public_tests=(PUBLIC,),
        oracle_tests=(ORACLE,),
    )

    parsed = parse_task_envelope(task_envelope_bytes(envelope))
    assert parsed.input_root is None
    assert reopen_task_inputs(parsed, input_root=root) == reopen_task_inputs(envelope)

    (root / "solution.py").write_text(SOURCE + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reference"):
        reopen_task_inputs(envelope)
