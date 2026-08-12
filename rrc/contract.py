"""Frozen public seam shared by the RRCv2 implementation lanes."""

from __future__ import annotations

import hashlib
import json
import keyword
import math
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, TypeAlias, cast

_TASK_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_SCOPE = _TASK_ID
_FAMILY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SLOT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PUBLIC_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PROFILES = frozenset({"rrcv2_general_v1", "rrcv2_synthetic_v1"})
_TEST_ARTIFACT_PATH = ".rrcv2/public-tests.v1.json"
_ORACLE_ARTIFACT_PATH = ".rrcv2/oracle-tests.v1.json"
_MAX_SOURCE_BYTES = 1_048_576
_MAX_TASK_BYTES = 256 * 1024
_MAX_TEST_BYTES = 16 * 1024
_MAX_TESTS = 64


def _nfc_text(value: object, *, name: str, nonempty: bool = True, cap: int | None = None) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if nonempty and not value:
        raise ValueError(f"{name} must be nonempty")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{name} must already be NFC")
    if "\r" in value or "\x00" in value:
        raise ValueError(f"{name} must use LF-only text without NUL")
    if cap is not None and len(value.encode("utf-8", errors="strict")) > cap:
        raise ValueError(f"{name} exceeds its UTF-8 byte cap")
    return value


def _artifact_path(value: object) -> str:
    path = _nfc_text(value, name="artifact_path", cap=64 * 1024)
    pure = PurePosixPath(path)
    parts = pure.parts
    literal_parts = path.split("/")
    if (
        pure.is_absolute()
        or "\\" in path
        or not parts
        or any(part in {"", ".", ".."} for part in literal_parts)
        or parts[0] == ".rrcv2"
    ):
        raise ValueError("artifact_path must be a confined relative POSIX path")
    return path


def _public_symbol(value: object, *, name: str) -> str:
    symbol = _nfc_text(value, name=name, cap=257)
    if _PUBLIC_SYMBOL.fullmatch(symbol) is None:
        raise ValueError(f"{name} must be a public top-level or direct-method ASCII symbol")
    parts = symbol.split(".")
    if any(keyword.iskeyword(part) for part in parts):
        raise ValueError(f"{name} may not contain a Python keyword")
    return symbol


def _canonical_value(value: object, *, path: str = "$") -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError(f"float is forbidden in canonical JSON at {path}")
    if isinstance(value, str):
        return _nfc_text(value, name=f"canonical string at {path}", nonempty=False)
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"canonical JSON object keys must be strings at {path}")
        result: dict[str, object] = {}
        for key, item in cast(dict[str, object], value).items():
            normalized = _nfc_text(key, name=f"canonical key at {path}", nonempty=False)
            if normalized in result:
                raise ValueError(f"duplicate canonical key at {path}")
            result[normalized] = _canonical_value(item, path=f"{path}.{normalized}")
        return result
    raise TypeError(f"unsupported canonical JSON value at {path}: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """Return the frozen compact, sorted, NFC JSON representation."""

    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")


@dataclass(frozen=True)
class ScoreV1:
    """Reduced exact score used for floors, ordering, and persisted evidence."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if isinstance(self.numerator, bool) or not isinstance(self.numerator, int):
            raise TypeError("score numerator must be a non-bool integer")
        if isinstance(self.denominator, bool) or not isinstance(self.denominator, int):
            raise TypeError("score denominator must be a non-bool integer")
        if not 0 <= self.numerator <= self.denominator <= 1_000_000_000:
            raise ValueError("score must satisfy 0 <= numerator <= denominator <= 1e9")
        if self.denominator <= 0 or math.gcd(self.numerator, self.denominator) != 1:
            raise ValueError("score must have a positive denominator and be reduced")

    def as_json(self) -> dict[str, int]:
        return {"denominator": self.denominator, "numerator": self.numerator}

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ScoreV1):
            return NotImplemented
        return self.numerator * other.denominator < other.numerator * self.denominator

    def __le__(self, other: object) -> bool:
        if not isinstance(other, ScoreV1):
            return NotImplemented
        return self.numerator * other.denominator <= other.numerator * self.denominator


@dataclass(frozen=True)
class StructuralShapeV1:
    """Exact structural signature used only for retrieval classification."""

    arg_types: tuple[str, ...]
    arity: int
    fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.arity, bool) or not isinstance(self.arity, int):
            raise TypeError("shape arity must be a non-bool integer")
        if not 0 <= self.arity <= 16 or len(self.arg_types) != self.arity:
            raise ValueError("shape arity must be 0..16 and match arg_types")
        if not isinstance(self.arg_types, tuple) or any(
            not isinstance(item, str) or not item for item in self.arg_types
        ):
            raise TypeError("shape arg_types must be a tuple of nonempty strings")
        if not isinstance(self.fields, tuple) or any(
            not isinstance(item, str) or _SLOT_NAME.fullmatch(item) is None for item in self.fields
        ):
            raise TypeError("shape fields must be a tuple of ASCII identifiers")
        if len(self.fields) > 32 or tuple(sorted(set(self.fields))) != self.fields:
            raise ValueError("shape fields must be sorted unique and contain at most 32 names")
        for item in self.arg_types:
            _nfc_text(item, name="shape arg type", cap=4096)

    def as_json(self) -> dict[str, object]:
        return {
            "arg_types": list(self.arg_types),
            "arity": self.arity,
            "fields": list(self.fields),
        }


@dataclass(frozen=True)
class Task:
    """One coding task in an evaluation run."""

    task_id: str
    text: str
    oracle_tests: tuple[str, ...] = ()
    family: str | None = None
    artifact_path: str = "solution.py"
    public_tests: tuple[str, ...] = ()
    searchable_public: bool = False
    verification_profile: str = "rrcv2_general_v1"
    primary: str | None = None
    shape: StructuralShapeV1 | None = None
    slot_values: tuple[tuple[str, str], ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.oracle_tests, tuple):
            raise TypeError("oracle_tests must be an immutable tuple")
        if not isinstance(self.public_tests, tuple):
            raise TypeError("public_tests must be an immutable tuple")
        if not isinstance(self.searchable_public, bool):
            raise TypeError("searchable_public must be a boolean")
        if self.verification_profile not in _PROFILES:
            raise ValueError("unsupported verification_profile")
        if self.family is not None and (
            not isinstance(self.family, str) or _FAMILY.fullmatch(self.family) is None
        ):
            raise ValueError("family must use the frozen lowercase identifier grammar")
        if self.primary is not None and (not isinstance(self.primary, str)):
            raise ValueError("primary must be a public top-level or direct-method symbol")
        if self.primary is not None:
            _public_symbol(self.primary, name="primary")
        if self.shape is not None and not isinstance(self.shape, StructuralShapeV1):
            raise TypeError("shape must be StructuralShapeV1 or None")
        if self.slot_values is not None:
            if not isinstance(self.slot_values, tuple) or any(
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not isinstance(pair[1], str)
                for pair in self.slot_values
            ):
                raise TypeError("slot_values must be an immutable tuple of string pairs")
            keys = tuple(pair[0] for pair in self.slot_values)
            if tuple(sorted(set(keys))) != keys:
                raise ValueError("slot_values keys must be sorted unique")
            if len(keys) > 256 or any(_SLOT_NAME.fullmatch(key) is None for key in keys):
                raise ValueError("slot_values keys exceed the frozen identifier/count bounds")
            if any(keyword.iskeyword(key) for key in keys):
                raise ValueError("slot_values keys may not be Python keywords")
            values = tuple(pair[1] for pair in self.slot_values)
            if len(set(values)) != len(values):
                raise ValueError("slot_values values must be distinct")
            for value in values:
                _nfc_text(value, name="slot value", cap=4096)
            if sum(len(value.encode("utf-8")) for value in values) > 64 * 1024:
                raise ValueError("slot_values exceed their aggregate byte cap")
        _nfc_text(self.task_id, name="task_id")
        if _TASK_ID.fullmatch(self.task_id) is None:
            raise ValueError("task_id does not use the frozen lowercase identifier grammar")
        _nfc_text(self.text, name="task text", cap=_MAX_TASK_BYTES)
        _artifact_path(self.artifact_path)
        for kind, tests in (("oracle", self.oracle_tests), ("public", self.public_tests)):
            if len(tests) > _MAX_TESTS or len(set(tests)) != len(tests):
                raise ValueError(f"{kind}_tests must be bounded and unique")
            for test in tests:
                _nfc_text(test, name=f"{kind} test", cap=_MAX_TEST_BYTES)


@dataclass(frozen=True)
class ResolvedTaskMetadataV1:
    """One measured small-model hint for a general WARM retrieval query."""

    primary: str
    family: str | None
    shape: StructuralShapeV1
    slot_values: tuple[tuple[str, str], ...]
    authority: Literal["small_model"] = "small_model"
    v: int = 1

    def __post_init__(self) -> None:
        _public_symbol(self.primary, name="resolved primary")
        if self.family is not None and _FAMILY.fullmatch(self.family) is None:
            raise ValueError("resolved family is invalid")
        if not isinstance(self.shape, StructuralShapeV1):
            raise TypeError("resolved shape must be StructuralShapeV1")
        # Reuse the complete Task-owned binding validator rather than a second grammar.
        Task(
            "resolved-metadata",
            "resolved metadata",
            family=self.family,
            primary=self.primary,
            shape=self.shape,
            slot_values=self.slot_values,
        )
        values = dict(self.slot_values)
        function = values.get("function")
        if function is not None and self.primary.rsplit(".", 1)[-1] != function:
            raise ValueError("resolved primary differs from slot_values.function")
        if self.authority != "small_model" or self.v != 1:
            raise ValueError("resolved metadata authority/version is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "authority": "small_model",
            "family": self.family,
            "primary": self.primary,
            "shape": self.shape.as_json(),
            "slot_values": dict(self.slot_values),
            "v": 1,
        }


def task_from_legacy(task_id: str, text: str, oracle_tests: str | None) -> Task:
    """The sole compatibility adapter from the historical one-module oracle form."""

    tests = () if oracle_tests is None or not oracle_tests else (oracle_tests,)
    return Task(task_id, text, oracle_tests=tests)


@dataclass(frozen=True)
class ArtifactRefV1:
    """Hash/size/path reference to one attempt-owned canonical artifact."""

    sha256: str
    bytes: int
    path: str

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or _HEX64.fullmatch(self.sha256) is None:
            raise ValueError("artifact ref sha256 must be lowercase hex")
        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int) or self.bytes < 0:
            raise TypeError("artifact ref bytes must be a nonnegative non-bool integer")
        _artifact_path(self.path) if not self.path.startswith(".rrcv2/") else _rrcv2_path(self.path)

    def as_json(self) -> dict[str, object]:
        return {"bytes": self.bytes, "path": self.path, "sha256": self.sha256}


def _rrcv2_path(path: object) -> str:
    value = _nfc_text(path, name="RRCv2 artifact path")
    if value not in {_TEST_ARTIFACT_PATH, _ORACLE_ARTIFACT_PATH}:
        raise ValueError("unknown RRCv2 artifact path")
    return value


TargetKind: TypeAlias = Literal["none", "absent", "regular"]


@dataclass(frozen=True)
class TargetPreimageV1:
    """Exact apply-CAS authority for no, absent, or regular delivery targets."""

    kind: TargetKind
    path: str | None = None
    sha256: str | None = None
    bytes: int | None = None
    mode: int | None = None
    v: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        if self.kind == "none":
            if any(value is not None for value in (self.path, self.sha256, self.bytes, self.mode)):
                raise ValueError("none target has no payload fields")
            return
        path = _artifact_path(self.path)
        del path
        if (
            isinstance(self.mode, bool)
            or not isinstance(self.mode, int)
            or not 0 <= self.mode <= 0o777
        ):
            raise ValueError("target mode must be a non-bool integer from 0 through 0777")
        if self.kind == "absent":
            if self.mode != 0o644 or self.sha256 is not None or self.bytes is not None:
                raise ValueError("absent target has fixed mode 0644 and no content authority")
            return
        if self.kind != "regular":
            raise ValueError("unknown target preimage kind")
        if not isinstance(self.sha256, str) or _HEX64.fullmatch(self.sha256) is None:
            raise ValueError("regular target sha256 must be lowercase hex")
        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int) or self.bytes < 0:
            raise ValueError("regular target bytes must be nonnegative")

    @classmethod
    def none(cls) -> TargetPreimageV1:
        return cls("none")

    @classmethod
    def absent(cls, path: str, mode: int = 0o644) -> TargetPreimageV1:
        return cls("absent", path=path, mode=mode)

    @classmethod
    def regular(cls, path: str, sha256: str, bytes: int, mode: int) -> TargetPreimageV1:
        return cls("regular", path=path, sha256=sha256, bytes=bytes, mode=mode)

    def as_json(self) -> dict[str, object]:
        if self.kind == "none":
            return {"kind": "none", "v": 1}
        if self.kind == "absent":
            return {"kind": "absent", "mode": self.mode, "path": self.path, "v": 1}
        return {
            "bytes": self.bytes,
            "kind": "regular",
            "mode": self.mode,
            "path": self.path,
            "sha256": self.sha256,
            "v": 1,
        }


@dataclass(frozen=True)
class InlineTaskInputV1:
    task: Task
    starter_source: str | None
    target_preimage: TargetPreimageV1
    kind: Literal["inline"] = field(default="inline", init=False)
    v: int = field(default=1, init=False)


@dataclass(frozen=True)
class ReferencedTaskInputV1:
    task: Task
    sealed_root: Path
    source_ref: ArtifactRefV1 | None
    public_test_ref: ArtifactRefV1
    oracle_ref: ArtifactRefV1 | None
    target_preimage: TargetPreimageV1
    kind: Literal["references"] = field(default="references", init=False)
    v: int = field(default=1, init=False)


TaskInputAuthorityV1: TypeAlias = InlineTaskInputV1 | ReferencedTaskInputV1


@dataclass(frozen=True)
class TaskEnvelopeV1:
    task: Task
    source_ref: ArtifactRefV1 | None
    public_test_ref: ArtifactRefV1
    oracle_ref: ArtifactRefV1 | None
    target_preimage: TargetPreimageV1
    shape: StructuralShapeV1 | None
    slot_values: tuple[tuple[str, str], ...] | None
    v: int = field(default=1, init=False)
    _input_root: Path | None = field(default=None, init=False, compare=False, repr=False)

    @property
    def input_root(self) -> Path | None:
        """Controller-owned runtime locator; deliberately absent from canonical JSON."""

        return self._input_root


@dataclass(frozen=True)
class SealedTaskMaterialsV1:
    """Hash-reopened source and test bytes consumed by the canonical engine."""

    source: str | None
    public_tests: tuple[str, ...]
    oracle_tests: tuple[str, ...]


def canonical_test_artifact_bytes(tests: tuple[str, ...]) -> bytes:
    if not isinstance(tests, tuple) or len(tests) > _MAX_TESTS or len(set(tests)) != len(tests):
        raise ValueError("test artifact tests must be a bounded unique tuple")
    for test in tests:
        _nfc_text(test, name="test artifact source", cap=_MAX_TEST_BYTES)
    raw = canonical_json_bytes({"tests": list(tests), "v": 1})
    if len(raw) > _MAX_SOURCE_BYTES:
        raise ValueError("test artifact exceeds its aggregate byte cap")
    return raw


def _task_json(task: Task) -> dict[str, object]:
    return {
        "artifact_path": task.artifact_path,
        "family": task.family,
        "primary": task.primary,
        "searchable_public": task.searchable_public,
        "task_id": task.task_id,
        "text": task.text,
        "verification_profile": task.verification_profile,
    }


def _slots_json(values: tuple[tuple[str, str], ...] | None) -> dict[str, str] | None:
    return None if values is None else dict(values)


def task_envelope_projection_bytes(envelope: TaskEnvelopeV1) -> bytes:
    return canonical_json_bytes(
        {
            "oracle_ref": None if envelope.oracle_ref is None else envelope.oracle_ref.as_json(),
            "public_test_ref": envelope.public_test_ref.as_json(),
            "shape": None if envelope.shape is None else envelope.shape.as_json(),
            "slot_values": _slots_json(envelope.slot_values),
            "source_ref": None if envelope.source_ref is None else envelope.source_ref.as_json(),
            "task": _task_json(envelope.task),
            "v": 1,
        }
    )


def task_envelope_bytes(envelope: TaskEnvelopeV1) -> bytes:
    value = cast(dict[str, object], json.loads(task_envelope_projection_bytes(envelope)))
    value["target_preimage"] = envelope.target_preimage.as_json()
    return canonical_json_bytes(value)


def _source_bytes(source: str) -> bytes:
    _nfc_text(source, name="starter_source", cap=_MAX_SOURCE_BYTES)
    return source.encode("utf-8", errors="strict")


def _ref(raw: bytes, path: str) -> ArtifactRefV1:
    return ArtifactRefV1(hashlib.sha256(raw).hexdigest(), len(raw), path)


def _read_confined(root: Path, ref: ArtifactRefV1, *, cap: int) -> tuple[bytes, int]:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ValueError("sealed root is unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink() or not root.is_absolute():
        raise ValueError("sealed root must be an absolute real directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        parts = PurePosixPath(ref.path).parts
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise ValueError("reference parent is not a directory")
        file_flags = flags | getattr(os, "O_NONBLOCK", 0)
        file_fd = os.open(parts[-1], file_flags, dir_fd=current)
        descriptors.append(file_fd)
        observed = os.fstat(file_fd)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_size != ref.bytes
            or observed.st_size > cap
        ):
            raise ValueError("reference is not the expected bounded regular confined file")
        chunks: list[bytes] = []
        remaining = cap + 1
        while remaining:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != ref.bytes or hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise ValueError("reference bytes changed or do not match their authority")
        return raw, stat.S_IMODE(observed.st_mode)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("reference is not the expected bounded regular confined file") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate_test_artifact(raw: bytes, expected: tuple[str, ...]) -> None:
    if raw != canonical_test_artifact_bytes(expected):
        raise ValueError("test artifact is not canonical or does not match the task authority")


def _write_owned(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_task_for_sealing(task: Task) -> None:
    if _TASK_ID.fullmatch(task.task_id) is None:
        raise ValueError("task_id does not use the frozen grammar")
    if task.verification_profile == "rrcv2_synthetic_v1":
        if task.family is None or task.shape is None or not task.slot_values:
            raise ValueError("synthetic tasks require family, shape, and nonempty slot_values")
        values = dict(task.slot_values)
        function = values.get("function")
        if function is None:
            raise ValueError("synthetic slot_values require function")
        _public_symbol(function, name="synthetic function")
        if "." in function:
            raise ValueError("synthetic function must be a top-level identifier")
        if task.primary is not None and task.primary != function:
            raise ValueError("synthetic primary must equal slot_values.function")
        _validate_synthetic_markers(task)
    if task.slot_values is not None and task.shape is None:
        raise ValueError("nonnull slot_values require nonnull shape")
    if task.slot_values is not None and "function" in dict(task.slot_values):
        function = dict(task.slot_values)["function"]
        _public_symbol(function, name="slot_values.function")
        if "." in function:
            raise ValueError("slot_values.function must be a top-level identifier")
        if task.primary is not None and task.primary.rsplit(".", 1)[-1] != function:
            raise ValueError("primary terminal component must equal slot_values.function")
    markers = ("RRC_SHAPE:", "RRC_SLOT_VALUES:")
    if task.verification_profile == "rrcv2_general_v1" and any(
        marker in task.text for marker in markers
    ):
        raise ValueError("general task text may not contain synthetic markers")


def _validate_synthetic_markers(task: Task) -> None:
    shape_prefix = "RRC_SHAPE: "
    values_prefix = "RRC_SLOT_VALUES: "
    if task.text.endswith("\n"):
        raise ValueError("synthetic task text must not have a trailing newline")
    lines = task.text.split("\n")
    if (
        len(lines) < 3
        or not lines[-2].startswith(shape_prefix)
        or not lines[-1].startswith(values_prefix)
    ):
        raise ValueError("synthetic task text requires exact final markers")
    body = "\n".join(lines[:-2])
    if not body or "RRC_SHAPE:" in body or "RRC_SLOT_VALUES:" in body:
        raise ValueError("synthetic task body or marker cardinality is invalid")
    shape_raw = lines[-2][len(shape_prefix) :].encode("utf-8", errors="strict")
    values_raw = lines[-1][len(values_prefix) :].encode("utf-8", errors="strict")
    try:
        shape_value = json.loads(shape_raw, object_pairs_hook=_unique_object)
        values_value = json.loads(values_raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("synthetic marker JSON is invalid") from exc
    if (
        canonical_json_bytes(shape_value) != shape_raw
        or canonical_json_bytes(values_value) != values_raw
    ):
        raise ValueError("synthetic marker JSON must be canonical")
    expected_shape = None if task.shape is None else task.shape.as_json()
    expected_values = None if task.slot_values is None else dict(task.slot_values)
    if shape_value != expected_shape or values_value != expected_values:
        raise ValueError("synthetic markers do not match top-level metadata")


def seal_task_input(authority: TaskInputAuthorityV1, *, input_root: Path) -> TaskEnvelopeV1:
    """Validate one exclusive input authority and copy it once into an owned sealed layout."""

    if (
        not isinstance(input_root, Path)
        or not input_root.is_absolute()
        or input_root.exists()
        or input_root.is_symlink()
    ):
        raise ValueError("input_root must be a fresh absolute path")
    if not isinstance(authority, (InlineTaskInputV1, ReferencedTaskInputV1)):
        raise TypeError("unknown task input authority")
    task = authority.task
    _validate_task_for_sealing(task)
    source_raw: bytes | None
    source_mode: int | None = None
    public_raw: bytes
    oracle_raw: bytes | None
    if isinstance(authority, InlineTaskInputV1):
        if authority.target_preimage.kind != "none":
            raise ValueError("inline input requires the none target preimage")
        source_raw = (
            None if authority.starter_source is None else _source_bytes(authority.starter_source)
        )
        if task.verification_profile == "rrcv2_synthetic_v1" and source_raw is None:
            raise ValueError("synthetic task starter_source is required")
        public_raw = canonical_test_artifact_bytes(task.public_tests)
        oracle_raw = (
            None if not task.oracle_tests else canonical_test_artifact_bytes(task.oracle_tests)
        )
    else:
        if not isinstance(authority.sealed_root, Path):
            raise TypeError("referenced sealed_root must be a Path")
        if task.public_tests or task.oracle_tests:
            raise ValueError("referenced task tuples must be empty")
        if authority.public_test_ref.path != _TEST_ARTIFACT_PATH:
            raise ValueError("referenced public test path is not canonical")
        if authority.oracle_ref is not None and authority.oracle_ref.path != _ORACLE_ARTIFACT_PATH:
            raise ValueError("referenced oracle path is not canonical")
        if authority.source_ref is not None and authority.source_ref.path != task.artifact_path:
            raise ValueError("source reference path does not match task artifact_path")
        if authority.source_ref is None:
            source_raw = None
        else:
            source_raw, source_mode = _read_confined(
                authority.sealed_root, authority.source_ref, cap=_MAX_SOURCE_BYTES
            )
        if source_raw is not None:
            try:
                source_text = source_raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ValueError("starter source is not strict UTF-8") from exc
            _source_bytes(source_text)
        public_raw, _ = _read_confined(
            authority.sealed_root, authority.public_test_ref, cap=_MAX_TESTS * _MAX_TEST_BYTES
        )
        oracle_raw = None
        if authority.oracle_ref is not None:
            oracle_raw, _ = _read_confined(
                authority.sealed_root, authority.oracle_ref, cap=_MAX_TESTS * _MAX_TEST_BYTES
            )
        _validate_test_artifact(public_raw, _tests_from_artifact(public_raw, allow_empty=True))
        if oracle_raw is not None:
            _validate_test_artifact(oracle_raw, _tests_from_artifact(oracle_raw, allow_empty=False))
        target = authority.target_preimage
        if source_raw is None:
            if target.kind != "absent" or target.path != task.artifact_path:
                raise ValueError("greenfield referenced input requires matching absent target")
            _prove_absent(authority.sealed_root, task.artifact_path)
        else:
            if authority.source_ref is None or (
                target.kind != "regular"
                or target.path != authority.source_ref.path
                or target.sha256 != authority.source_ref.sha256
                or target.bytes != authority.source_ref.bytes
            ):
                raise ValueError("regular target preimage does not match source authority")
            if target.mode != source_mode:
                raise ValueError("regular target mode does not match source authority")

    source_ref = None if source_raw is None else _ref(source_raw, task.artifact_path)
    public_ref = _ref(public_raw, _TEST_ARTIFACT_PATH)
    oracle_ref = None if oracle_raw is None else _ref(oracle_raw, _ORACLE_ARTIFACT_PATH)
    envelope = TaskEnvelopeV1(
        task=replace(task, oracle_tests=(), public_tests=()),
        source_ref=source_ref,
        public_test_ref=public_ref,
        oracle_ref=oracle_ref,
        target_preimage=authority.target_preimage,
        shape=task.shape,
        slot_values=task.slot_values,
    )
    input_root.mkdir(mode=0o700)
    os.chmod(input_root, 0o700)
    try:
        if source_raw is not None:
            _write_owned(input_root / task.artifact_path, source_raw)
        _write_owned(input_root / _TEST_ARTIFACT_PATH, public_raw)
        if oracle_raw is not None:
            _write_owned(input_root / _ORACLE_ARTIFACT_PATH, oracle_raw)
        for reference in (source_ref, public_ref, oracle_ref):
            if reference is not None:
                copied, mode = _read_confined(input_root, reference, cap=_MAX_SOURCE_BYTES)
                if mode != 0o600 or _ref(copied, reference.path) != reference:
                    raise ValueError("sealed input copy did not reopen with exact bytes and mode")
        directory = os.open(
            input_root,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        object.__setattr__(envelope, "_input_root", input_root)
    except BaseException:
        # The root is operation-owned and contains no provider output at this pre-journal boundary.
        import shutil

        shutil.rmtree(input_root, ignore_errors=True)
        raise
    return envelope


def reopen_task_inputs(
    envelope: TaskEnvelopeV1, *, input_root: Path | None = None
) -> SealedTaskMaterialsV1:
    """Boundedly reopen all TaskEnvelope refs from the controller-owned sealed root."""

    if not isinstance(envelope, TaskEnvelopeV1):
        raise TypeError("envelope must be TaskEnvelopeV1")
    root = envelope.input_root if input_root is None else input_root
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("task envelope has no absolute input-root authority")
    try:
        observed = root.lstat()
    except OSError as exc:
        raise ValueError("task input root is unavailable") from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) != 0o700
        or root.is_symlink()
    ):
        raise ValueError("task input root must be a real mode-0700 directory")

    source: str | None = None
    if envelope.source_ref is not None:
        source_raw, source_mode = _read_confined(root, envelope.source_ref, cap=_MAX_SOURCE_BYTES)
        if source_mode != 0o600:
            raise ValueError("sealed source must have exact mode 0600")
        try:
            source = source_raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("sealed source is not strict UTF-8") from exc
        _source_bytes(source)
    public_raw, public_mode = _read_confined(root, envelope.public_test_ref, cap=_MAX_SOURCE_BYTES)
    if public_mode != 0o600:
        raise ValueError("sealed public tests must have exact mode 0600")
    public = _tests_from_artifact(public_raw, allow_empty=True)
    _validate_test_artifact(public_raw, public)
    oracle: tuple[str, ...] = ()
    if envelope.oracle_ref is not None:
        oracle_raw, oracle_mode = _read_confined(root, envelope.oracle_ref, cap=_MAX_SOURCE_BYTES)
        if oracle_mode != 0o600:
            raise ValueError("sealed oracle tests must have exact mode 0600")
        oracle = _tests_from_artifact(oracle_raw, allow_empty=False)
        _validate_test_artifact(oracle_raw, oracle)
    return SealedTaskMaterialsV1(source=source, public_tests=public, oracle_tests=oracle)


def _exact_object(value: object, fields: set[str], *, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} schema is invalid")
    return cast(dict[str, object], value)


def _parse_artifact_ref(value: object, *, name: str) -> ArtifactRefV1:
    row = _exact_object(value, {"bytes", "path", "sha256"}, name=name)
    try:
        return ArtifactRefV1(
            sha256=cast(str, row["sha256"]),
            bytes=cast(int, row["bytes"]),
            path=cast(str, row["path"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} schema is invalid") from exc


def _parse_shape(value: object) -> StructuralShapeV1 | None:
    if value is None:
        return None
    row = _exact_object(value, {"arg_types", "arity", "fields"}, name="shape")
    arg_types = row["arg_types"]
    fields = row["fields"]
    if not isinstance(arg_types, list) or not isinstance(fields, list):
        raise ValueError("shape schema is invalid")
    try:
        return StructuralShapeV1(
            tuple(cast(list[str], arg_types)),
            cast(int, row["arity"]),
            tuple(cast(list[str], fields)),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("shape schema is invalid") from exc


def _parse_slot_values(value: object) -> tuple[tuple[str, str], ...] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("slot_values schema is invalid")
    return tuple(sorted(cast(dict[str, str], value).items()))


def _parse_target_preimage(value: object) -> TargetPreimageV1:
    if not isinstance(value, dict):
        raise ValueError("target_preimage schema is invalid")
    row = cast(dict[str, object], value)
    kind = row.get("kind")
    if kind == "none":
        _exact_object(row, {"kind", "v"}, name="target_preimage")
        if row["v"] != 1:
            raise ValueError("target_preimage version is invalid")
        return TargetPreimageV1.none()
    if kind == "absent":
        _exact_object(row, {"kind", "mode", "path", "v"}, name="target_preimage")
        if row["v"] != 1:
            raise ValueError("target_preimage version is invalid")
        return TargetPreimageV1.absent(cast(str, row["path"]), cast(int, row["mode"]))
    if kind == "regular":
        _exact_object(
            row,
            {"bytes", "kind", "mode", "path", "sha256", "v"},
            name="target_preimage",
        )
        if row["v"] != 1:
            raise ValueError("target_preimage version is invalid")
        return TargetPreimageV1.regular(
            cast(str, row["path"]),
            cast(str, row["sha256"]),
            cast(int, row["bytes"]),
            cast(int, row["mode"]),
        )
    raise ValueError("target_preimage schema is invalid")


def parse_task_envelope(raw: bytes) -> TaskEnvelopeV1:
    """Strictly reopen one canonical TaskEnvelopeV1 value without file access."""

    if not isinstance(raw, bytes) or len(raw) > 2 * _MAX_SOURCE_BYTES:
        raise ValueError("task envelope exceeds its byte cap")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("task envelope is not strict JSON") from exc
    if canonical_json_bytes(value) != raw:
        raise ValueError("task envelope is not canonical JSON")
    row = _exact_object(
        value,
        {
            "oracle_ref",
            "public_test_ref",
            "shape",
            "slot_values",
            "source_ref",
            "target_preimage",
            "task",
            "v",
        },
        name="task envelope",
    )
    if row["v"] != 1:
        raise ValueError("task envelope version is invalid")
    task_row = _exact_object(
        row["task"],
        {
            "artifact_path",
            "family",
            "primary",
            "searchable_public",
            "task_id",
            "text",
            "verification_profile",
        },
        name="task",
    )
    shape = _parse_shape(row["shape"])
    slot_values = _parse_slot_values(row["slot_values"])
    try:
        task = Task(
            task_id=cast(str, task_row["task_id"]),
            text=cast(str, task_row["text"]),
            family=cast(str | None, task_row["family"]),
            artifact_path=cast(str, task_row["artifact_path"]),
            searchable_public=cast(bool, task_row["searchable_public"]),
            verification_profile=cast(str, task_row["verification_profile"]),
            primary=cast(str | None, task_row["primary"]),
            shape=shape,
            slot_values=slot_values,
        )
        _validate_task_for_sealing(task)
        source_ref = (
            None
            if row["source_ref"] is None
            else _parse_artifact_ref(row["source_ref"], name="source_ref")
        )
        public_ref = _parse_artifact_ref(row["public_test_ref"], name="public_test_ref")
        oracle_ref = (
            None
            if row["oracle_ref"] is None
            else _parse_artifact_ref(row["oracle_ref"], name="oracle_ref")
        )
        target = _parse_target_preimage(row["target_preimage"])
    except (TypeError, ValueError) as exc:
        raise ValueError("task envelope schema is invalid") from exc
    if public_ref.path != _TEST_ARTIFACT_PATH:
        raise ValueError("task envelope public source path is invalid")
    if oracle_ref is not None and oracle_ref.path != _ORACLE_ARTIFACT_PATH:
        raise ValueError("task envelope oracle source path is invalid")
    if source_ref is not None and source_ref.path != task.artifact_path:
        raise ValueError("task envelope source path is invalid")
    if task.verification_profile == "rrcv2_synthetic_v1" and source_ref is None:
        raise ValueError("task envelope synthetic source is required")
    if target.kind == "regular":
        if (
            source_ref is None
            or target.path != source_ref.path
            or (
                target.sha256,
                target.bytes,
            )
            != (source_ref.sha256, source_ref.bytes)
        ):
            raise ValueError("task envelope regular target does not match source")
    elif target.kind == "absent":
        if source_ref is not None or target.path != task.artifact_path:
            raise ValueError("task envelope absent target does not match greenfield source")
    envelope = TaskEnvelopeV1(
        task=task,
        source_ref=source_ref,
        public_test_ref=public_ref,
        oracle_ref=oracle_ref,
        target_preimage=target,
        shape=shape,
        slot_values=slot_values,
    )
    if task_envelope_bytes(envelope) != raw:
        raise ValueError("task envelope does not round-trip canonically")
    return envelope


def _tests_from_artifact(raw: bytes, *, allow_empty: bool) -> tuple[str, ...]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("test artifact is not strict canonical JSON") from exc
    if not isinstance(value, dict) or set(value) != {"tests", "v"} or value.get("v") != 1:
        raise ValueError("test artifact schema is invalid")
    tests = value.get("tests")
    if not isinstance(tests, list) or any(not isinstance(item, str) for item in tests):
        raise ValueError("test artifact tests are invalid")
    result = tuple(cast(list[str], tests))
    if not allow_empty and not result:
        raise ValueError("oracle test artifact must be nonempty")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _prove_absent(root: Path, path: str) -> None:
    parts = PurePosixPath(path).parts
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open(root, flags)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise ValueError("absent target parent must be a real directory")
        try:
            observed = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            return
        if observed:
            raise ValueError("absent target already exists")
    except OSError as exc:
        raise ValueError("absent target parent is unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


@dataclass(frozen=True)
class Slots:
    """Instance values labelled by the SPEC stage for deterministic reuse."""

    entity: str | None = None
    identifiers: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    constants: tuple[str, ...] = ()
    edge_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.entity is not None:
            _nfc_text(self.entity, name="slots.entity", cap=4096)
        categories = (
            ("identifiers", self.identifiers),
            ("types", self.types),
            ("fields", self.fields),
            ("constants", self.constants),
            ("edge_values", self.edge_values),
        )
        occurrences = int(self.entity is not None)
        distinct: set[str] = set()
        if self.entity is not None:
            distinct.add(self.entity)
        for name, values in categories:
            if not isinstance(values, tuple):
                raise TypeError(f"slots.{name} must be an immutable tuple")
            if len(values) > 256:
                raise ValueError(f"slots.{name} exceeds its item cap")
            occurrences += len(values)
            for value in values:
                distinct.add(_nfc_text(value, name=f"slots.{name} item", cap=4096))
        if occurrences > 1024:
            raise ValueError("slots exceed the aggregate occurrence cap")
        if len(distinct) > 256:
            raise ValueError("slots exceed the distinct-value cap")
        if sum(len(item.encode("utf-8")) for item in distinct) > 64 * 1024:
            raise ValueError("slots exceed the distinct-value byte cap")
        if len(canonical_json_bytes(self.as_json())) > 256 * 1024:
            raise ValueError("slots exceed the canonical byte cap")

    def as_json(self) -> dict[str, object]:
        return {
            "constants": list(self.constants),
            "edge_values": list(self.edge_values),
            "entity": self.entity,
            "fields": list(self.fields),
            "identifiers": list(self.identifiers),
            "types": list(self.types),
        }


@dataclass(frozen=True)
class Spec:
    """Model-authored implementation specification and acceptance tests."""

    plan: str
    signature: str
    contract: str
    tests: tuple[str, ...]
    slots: Slots

    def __post_init__(self) -> None:
        _nfc_text(self.plan, name="spec.plan", cap=64 * 1024)
        _nfc_text(self.signature, name="spec.signature", cap=64 * 1024)
        _nfc_text(self.contract, name="spec.contract", cap=128 * 1024)
        if not isinstance(self.tests, tuple) or not self.tests or len(self.tests) > _MAX_TESTS:
            raise ValueError("spec.tests must be a nonempty bounded immutable tuple")
        if len(set(self.tests)) != len(self.tests):
            raise ValueError("spec.tests must be unique")
        for test in self.tests:
            _nfc_text(test, name="spec test", cap=_MAX_TEST_BYTES)
        if not isinstance(self.slots, Slots):
            raise TypeError("spec.slots must be Slots")

    def as_json(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "plan": self.plan,
            "signature": self.signature,
            "slots": self.slots.as_json(),
            "tests": list(self.tests),
        }


@dataclass(frozen=True)
class TemplateBundle:
    """Content-addressed reusable Spec plus implementation-blind tests."""

    spec_template: Spec
    independent_tests: tuple[str, ...]
    slot_names: tuple[str, ...]
    slot_contexts: tuple[tuple[str, tuple[str, ...]], ...]
    v: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.spec_template, Spec):
            raise TypeError("spec_template must be Spec")
        if not isinstance(self.independent_tests, tuple):
            raise TypeError("independent_tests must be an immutable tuple")
        for test in self.independent_tests:
            _nfc_text(test, name="generic independent test", cap=_MAX_TEST_BYTES)
        if len(self.independent_tests) > _MAX_TESTS or len(set(self.independent_tests)) != len(
            self.independent_tests
        ):
            raise ValueError("independent tests must be bounded and unique")
        if self.slot_names != tuple(sorted(set(self.slot_names))) or len(self.slot_names) > 256:
            raise ValueError("slot_names must be sorted, unique, and bounded")
        for name in self.slot_names:
            if _SLOT_NAME.fullmatch(name) is None or keyword.iskeyword(name):
                raise ValueError("slot name does not use the frozen identifier grammar")
        if tuple(name for name, _ in self.slot_contexts) != self.slot_names:
            raise ValueError("slot_contexts must have exactly the sorted slot-name keys")
        allowed = {
            "text",
            "identifier",
            "type",
            "python_literal",
            "string_content",
            "exception_symbol",
        }
        for _, contexts in self.slot_contexts:
            if (
                contexts != tuple(sorted(set(contexts)))
                or not contexts
                or not set(contexts) <= allowed
            ):
                raise ValueError("slot contexts must be nonempty sorted unique known contexts")

    def as_json(self) -> dict[str, object]:
        return {
            "independent_tests": {"tests": list(self.independent_tests), "v": 1},
            "slot_contexts": {name: list(contexts) for name, contexts in self.slot_contexts},
            "slot_names": list(self.slot_names),
            "spec_template": self.spec_template.as_json(),
            "v": 1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())

    @property
    def external_ref(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def spec_skeleton(self) -> Spec:
        """Compatibility spelling for callers while M2 migrates to ``spec_template``."""

        return self.spec_template


Template = TemplateBundle


class BranchDecision(str, Enum):
    """Result of retrieval and deterministic structural matching."""

    DIRECT = "direct"
    REUSE = "reuse"
    PRIME = "prime"
    MISS = "miss"


class ArmMode(str, Enum):
    """Evaluation arm selected for a solve."""

    BASELINE = "baseline"
    CHEAP_ALONE = "cheap_alone"
    CASCADE = "cascade"
    COLD = "cold"
    WARM = "warm"


class ModelRole(str, Enum):
    """Provider-independent model capability requested by Lane A."""

    STRONG = "strong"
    SMALL = "small"


@dataclass(frozen=True)
class Candidate:
    """Similarity-index hit resolved through the RRCv2 template store."""

    external_ref: str
    score: ScoreV1

    def __post_init__(self) -> None:
        if _HEX64.fullmatch(self.external_ref) is None:
            raise ValueError("candidate external_ref must be lowercase SHA-256")
        if not isinstance(self.score, ScoreV1):
            raise TypeError("candidate score must be ScoreV1")


@dataclass(frozen=True)
class Usage:
    """Token usage reported by a model provider."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens must equal prompt_tokens plus completion_tokens")
        if self.cached_input_tokens > self.prompt_tokens:
            raise ValueError("cached_input_tokens cannot exceed prompt_tokens")
        if self.reasoning_output_tokens > self.completion_tokens:
            raise ValueError("reasoning_output_tokens cannot exceed completion_tokens")


@dataclass(frozen=True)
class Completion:
    """One provider completion and its metering information."""

    text: str
    usage: Usage
    model: str
    transcript_sha256: str | None = None
    identity_attestation: str = "usage_only"
    effective_provider: str = "unattested"
    effective_reasoning: str = "unattested"
    effective_service_tier: str = "unattested"

    def __post_init__(self) -> None:
        _nfc_text(self.text, name="completion text", cap=2 * 1024 * 1024)
        _nfc_text(self.model, name="completion model", cap=256)
        if self.transcript_sha256 is not None and _HEX64.fullmatch(self.transcript_sha256) is None:
            raise ValueError("completion transcript_sha256 must be lowercase SHA-256")
        if self.identity_attestation not in {"usage_only", "native_partial", "native_complete"}:
            raise ValueError("completion identity_attestation is invalid")
        for value, name in (
            (self.effective_provider, "effective_provider"),
            (self.effective_reasoning, "effective_reasoning"),
            (self.effective_service_tier, "effective_service_tier"),
        ):
            _nfc_text(value, name=name, cap=256)


@dataclass(frozen=True)
class CostEvent:
    """Provider-scoped cost event emitted for one model call."""

    arm: str
    task_id: str
    stage: str
    model: str
    usage: Usage
    provider: str


@dataclass(frozen=True)
class CostEventV1:
    """Exact provider-visible accounting row for one committed paid call."""

    cost_event_id: str
    cell_id: str
    attempt_id: str | None
    task_id: str
    arm: str
    stage: str
    stage_ordinal: int
    prompt_sha256: str
    final_message_sha256: str
    transcript_sha256: str
    requested_provider: str
    requested_model: str
    requested_reasoning: str
    requested_service_tier: str
    identity_attestation: str
    effective_provider: str
    effective_model: str
    effective_reasoning: str
    effective_service_tier: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    provider_total_tokens: int
    v: int = 1

    def __post_init__(self) -> None:
        for value, name in (
            (self.cost_event_id, "cost_event_id"),
            (self.cell_id, "cell_id"),
            (self.task_id, "task_id"),
            (self.arm, "arm"),
            (self.stage, "stage"),
        ):
            if _SCOPE.fullmatch(value) is None:
                raise ValueError(f"{name} does not use the frozen identifier grammar")
        if self.attempt_id is not None and _HEX64.fullmatch(self.attempt_id) is None:
            raise ValueError("attempt_id must be null or lowercase SHA-256")
        for value, name in (
            (self.prompt_sha256, "prompt_sha256"),
            (self.final_message_sha256, "final_message_sha256"),
            (self.transcript_sha256, "transcript_sha256"),
        ):
            if _HEX64.fullmatch(value) is None:
                raise ValueError(f"{name} must be lowercase SHA-256")
        if (
            isinstance(self.stage_ordinal, bool)
            or not isinstance(self.stage_ordinal, int)
            or not 1 <= self.stage_ordinal <= 16
        ):
            raise ValueError("stage_ordinal must be an integer from 1 through 16")
        for value, name in (
            (self.requested_provider, "requested_provider"),
            (self.requested_model, "requested_model"),
            (self.requested_reasoning, "requested_reasoning"),
            (self.requested_service_tier, "requested_service_tier"),
            (self.effective_provider, "effective_provider"),
            (self.effective_model, "effective_model"),
            (self.effective_reasoning, "effective_reasoning"),
            (self.effective_service_tier, "effective_service_tier"),
        ):
            _nfc_text(value, name=name, cap=256)
        if self.requested_provider != "openai" or self.requested_service_tier != "priority":
            raise ValueError("requested provider/tier differs from the frozen policy")
        if self.requested_reasoning not in {"low", "medium"}:
            raise ValueError("requested_reasoning is invalid")
        if self.identity_attestation not in {"usage_only", "native_partial", "native_complete"}:
            raise ValueError("identity_attestation is invalid")
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "provider_total_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input cannot exceed input tokens")
        if self.reasoning_output_tokens > self.output_tokens:
            raise ValueError("reasoning output cannot exceed output tokens")
        if self.provider_total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("provider total must equal input plus output tokens")
        if self.v != 1:
            raise ValueError("unknown CostEvent version")

    @property
    def model(self) -> str:
        return self.effective_model

    @property
    def provider(self) -> str:
        return self.effective_provider

    @property
    def usage(self) -> Usage:
        return Usage(
            self.input_tokens,
            self.output_tokens,
            self.provider_total_tokens,
            self.cached_input_tokens,
            self.reasoning_output_tokens,
        )

    def as_json(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "attempt_id": self.attempt_id,
            "cached_input_tokens": self.cached_input_tokens,
            "cell_id": self.cell_id,
            "cost_event_id": self.cost_event_id,
            "effective_model": self.effective_model,
            "effective_provider": self.effective_provider,
            "effective_reasoning": self.effective_reasoning,
            "effective_service_tier": self.effective_service_tier,
            "final_message_sha256": self.final_message_sha256,
            "identity_attestation": self.identity_attestation,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "prompt_sha256": self.prompt_sha256,
            "provider_total_tokens": self.provider_total_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "requested_model": self.requested_model,
            "requested_provider": self.requested_provider,
            "requested_reasoning": self.requested_reasoning,
            "requested_service_tier": self.requested_service_tier,
            "stage": self.stage,
            "stage_ordinal": self.stage_ordinal,
            "task_id": self.task_id,
            "transcript_sha256": self.transcript_sha256,
            "v": 1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())


@dataclass(frozen=True)
class SolveOutcome:
    """Complete result returned by the Lane A solve pipeline."""

    task_id: str
    arm: str
    code: str
    passed: bool
    pass_at_1: bool | None
    branch: BranchDecision
    repairs: int
    escalated: bool
    template: Template | None
    cost_events: tuple[CostEventV1, ...]
    oracle_status: Literal["not_present", "passed", "failed", "infrastructure_failure"] = (
        "not_present"
    )

    def __post_init__(self) -> None:
        if self.oracle_status not in {
            "not_present",
            "passed",
            "failed",
            "infrastructure_failure",
        }:
            raise ValueError("unknown oracle status")
        if self.oracle_status == "passed" and self.pass_at_1 is not True:
            raise ValueError("passed oracle status requires pass_at_1=true")
        if self.oracle_status == "failed" and self.pass_at_1 is not False:
            raise ValueError("failed oracle status requires pass_at_1=false")
        if (
            self.oracle_status in {"not_present", "infrastructure_failure"}
            and self.pass_at_1 is not None
        ):
            raise ValueError("unscored oracle status requires pass_at_1=null")


class StoreFailure(RuntimeError):
    """A post-success persistence failure retaining the completed outcome."""

    def __init__(self, outcome: SolveOutcome) -> None:
        super().__init__(f"failed to store successful outcome for task {outcome.task_id}")
        self.outcome = outcome


@dataclass
class RunContext:
    """Stable arm/task identity used for provider query attribution."""

    arm: str
    task_id: str
    owner_scope: str
    cell_id: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.arm, "arm"),
            (self.task_id, "task_id"),
            (self.owner_scope, "owner_scope"),
        ):
            if _SCOPE.fullmatch(value) is None:
                raise ValueError(f"{name} does not use the frozen identifier grammar")
        if self.cell_id is not None and _SCOPE.fullmatch(self.cell_id) is None:
            raise ValueError("cell_id does not use the frozen identifier grammar")

    def tag(self, stage: str) -> str:
        """Return the frozen Snowflake query-tag format."""

        return f"rrc:arm={self.arm};task={self.task_id};stage={stage}"


@dataclass(frozen=True)
class Config:
    """Configuration shared across the two implementation lanes."""

    owner_scope: str
    repair_cap_N: int = 2
    pyright_mode: str = "basic"
    model_provider: str = "codex"
    tau_floor: ScoreV1 = field(default_factory=lambda: ScoreV1(7, 20))
    top_k: int = 3
    prefer_prime_on_shape_diff: bool = True
    memory_backend: Literal["sqlite", "everos"] = "sqlite"
    everos_target: bytes | None = None

    def __post_init__(self) -> None:
        if _SCOPE.fullmatch(self.owner_scope) is None:
            raise ValueError("owner_scope does not use the frozen lowercase identifier grammar")
        if (
            isinstance(self.repair_cap_N, bool)
            or not isinstance(self.repair_cap_N, int)
            or not 0 <= self.repair_cap_N <= 2
        ):
            raise ValueError("repair_cap_N must be a non-bool integer from 0 through 2")
        if self.pyright_mode != "basic":
            raise ValueError("pyright_mode must be the frozen basic profile")
        if self.model_provider != "codex":
            raise ValueError("model_provider must be codex")
        if not isinstance(self.tau_floor, ScoreV1):
            raise TypeError("tau_floor must be ScoreV1")
        if (
            isinstance(self.top_k, bool)
            or not isinstance(self.top_k, int)
            or not 1 <= self.top_k <= 100
        ):
            raise ValueError("top_k must be a non-bool integer from 1 through 100")
        if not isinstance(self.prefer_prime_on_shape_diff, bool):
            raise TypeError("prefer_prime_on_shape_diff must be a boolean")
        if self.memory_backend not in {"sqlite", "everos"}:
            raise ValueError("memory_backend must be sqlite or everos")
        if self.memory_backend == "sqlite" and self.everos_target is not None:
            raise ValueError("SQLite configuration cannot contain an EverOS target")
        if self.memory_backend == "everos":
            if self.everos_target is None:
                raise ValueError("EverOS configuration requires a sealed target")
            from rrc.everos import parse_target

            target = parse_target(self.everos_target)
            if target.isolation.owner_scope != self.owner_scope:
                raise ValueError("EverOS target owner differs from Config.owner_scope")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "everos_target": (
                    None if self.everos_target is None else json.loads(self.everos_target)
                ),
                "memory_backend": self.memory_backend,
                "model_provider": self.model_provider,
                "owner_scope": self.owner_scope,
                "prefer_prime_on_shape_diff": self.prefer_prime_on_shape_diff,
                "pyright_mode": self.pyright_mode,
                "repair_cap_N": self.repair_cap_N,
                "tau_floor": self.tau_floor.as_json(),
                "top_k": self.top_k,
                "v": 1,
            }
        )


class ModelPort(Protocol):
    """Single-completion provider consumed by the Lane A control loop."""

    provider: str

    def complete(
        self,
        role: ModelRole,
        prompt: str,
        ctx: RunContext,
        stage: str,
    ) -> Completion:
        """Return exactly one completion for a pipeline stage."""

        ...


class RetrievalPort(Protocol):
    """Similarity-index and exact-template operations consumed by Lane A."""

    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
        """Return candidates whose score meets the configured floor."""

        ...

    def get_template(self, external_ref: str) -> Template | None:
        """Fetch an exact template from RRCv2's store of record."""

        ...

    authority_id: str
    database_uuid: str


class Solver(Protocol):
    """Typed callable public entry point implemented by :func:`rrc.pipeline.solve`."""

    def __call__(
        self,
        input: TaskEnvelopeV1,
        *,
        mode: ArmMode,
        model: ModelPort,
        retrieval: RetrievalPort,
        cfg: Config,
        journal: Any,
        acceptance: Any,
        operation_key: str,
    ) -> SolveOutcome:
        """Solve one task through the selected Lane A arm."""

        ...


class NullRetrieval:
    """No-op retrieval implementation for cold arms and offline development."""

    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
        return []

    def get_template(self, external_ref: str) -> Template | None:
        return None

    authority_id = "null-retrieval"
    database_uuid = "0" * 64
