"""EverOS-free owner-scoped hybrid retrieval for canonical RRCv2 bundles."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from functools import cmp_to_key
from typing import Literal, Protocol, cast

from rrc.contract import (
    Candidate,
    Config,
    ScoreV1,
    StructuralShapeV1,
    Task,
    Template,
    canonical_json_bytes,
)
from rrc.pipeline.template import TemplateError, parse_template_bundle

_WORD = re.compile(r"[a-z0-9_]+")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PROJECTION_BYTES = 48 * 1024


class ProjectionError(ValueError):
    """A task cannot be safely projected without retaining a concrete slot value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_PROJECTION_CODES = frozenset(
    {
        "normalized_value_collision",
        "overlap",
        "reserved_token",
        "value_leak",
        "oversize",
    }
)


@dataclass(frozen=True)
class ProjectionUnavailableV1:
    attempt_id: str
    phase: Literal["query", "store"]
    code: str
    input_sha256: str
    evidence_sha256: str
    v: int = 1

    def __post_init__(self) -> None:
        if _HEX64.fullmatch(self.attempt_id) is None:
            raise ValueError("projection attempt_id must be lowercase SHA-256")
        if self.phase not in {"query", "store"}:
            raise ValueError("projection phase is invalid")
        if self.code not in _PROJECTION_CODES:
            raise ValueError("projection code is invalid")
        if (
            _HEX64.fullmatch(self.input_sha256) is None
            or _HEX64.fullmatch(self.evidence_sha256) is None
        ):
            raise ValueError("projection hashes must be lowercase SHA-256")
        if self.v != 1:
            raise ValueError("unknown projection-unavailable version")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "attempt_id": self.attempt_id,
                "code": self.code,
                "evidence_sha256": self.evidence_sha256,
                "input_sha256": self.input_sha256,
                "phase": self.phase,
                "v": 1,
            }
        )


def projection_unavailable(
    *, attempt_id: str, phase: Literal["query", "store"], code: str, input_bytes: bytes
) -> tuple[ProjectionUnavailableV1, bytes]:
    if not isinstance(input_bytes, bytes) or len(input_bytes) > 256 * 1024:
        raise ValueError("projection input evidence must be bounded bytes")
    evidence = (code + "\n").encode("ascii", errors="strict")
    return (
        ProjectionUnavailableV1(
            attempt_id,
            phase,
            code,
            hashlib.sha256(input_bytes).hexdigest(),
            hashlib.sha256(evidence).hexdigest(),
        ),
        evidence,
    )


def parse_projection_unavailable(raw: bytes) -> ProjectionUnavailableV1:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("projection-unavailable record is not JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "attempt_id",
            "code",
            "evidence_sha256",
            "input_sha256",
            "phase",
            "v",
        }
        or value.get("v") != 1
    ):
        raise ValueError("projection-unavailable schema is invalid")
    record = ProjectionUnavailableV1(
        cast(str, value.get("attempt_id")),
        cast(Literal["query", "store"], value.get("phase")),
        cast(str, value.get("code")),
        cast(str, value.get("input_sha256")),
        cast(str, value.get("evidence_sha256")),
    )
    if record.canonical_bytes() != raw:
        raise ValueError("projection-unavailable record is not canonical")
    return record


def search_norm(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("search text must be a string")
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())


def _task_body(task: Task) -> str:
    text = task.text
    if task.verification_profile == "rrcv2_synthetic_v1":
        lines = text.splitlines()
        if (
            len(lines) < 3
            or not lines[-2].startswith("RRC_SHAPE: ")
            or not lines[-1].startswith("RRC_SLOT_VALUES: ")
        ):
            raise ProjectionError("value_leak")
        text = "\n".join(lines[:-2])
    return search_norm(text).strip(" \t\n")


def _normalized_bindings(task: Task) -> tuple[tuple[str, str], ...]:
    values = task.slot_values or ()
    normalized = tuple((name, search_norm(value)) for name, value in values)
    concrete = [value for _, value in normalized]
    if any(not value for value in concrete) or len(set(concrete)) != len(concrete):
        raise ProjectionError("normalized_value_collision")
    return tuple(sorted(normalized, key=lambda item: (-len(item[1].encode("utf-8")), item[0])))


def _project_string(value: str, bindings: tuple[tuple[str, str], ...]) -> str:
    normalized = search_norm(value)
    for name, _ in bindings:
        if f"slot_{name}" in normalized:
            raise ProjectionError("reserved_token")
    matches: list[tuple[int, int, str]] = []
    for name, concrete in bindings:
        start = 0
        while True:
            index = normalized.find(concrete, start)
            if index < 0:
                break
            matches.append((index, index + len(concrete), name))
            start = index + 1
    matches.sort(key=lambda row: (row[0], -(row[1] - row[0]), row[2]))
    for previous, current in zip(matches, matches[1:]):
        if current[0] < previous[1]:
            raise ProjectionError("overlap")
    pieces: list[str] = []
    unmatched: list[str] = []
    cursor = 0
    for start, end, name in matches:
        plain = normalized[cursor:start]
        pieces.append(plain)
        unmatched.append(plain)
        pieces.append(f"slot_{name}")
        cursor = end
    tail = normalized[cursor:]
    pieces.append(tail)
    unmatched.append(tail)
    projected = "".join(pieces)
    remaining = "".join(unmatched)
    for _, concrete in bindings:
        if concrete in remaining:
            raise ProjectionError("value_leak")
    return projected


def _project_shape(
    shape: StructuralShapeV1 | None,
    bindings: tuple[tuple[str, str], ...],
) -> dict[str, object] | None:
    if shape is None:
        return None

    def leaf(value: object) -> object:
        if isinstance(value, str):
            normalized = search_norm(value)
            exact = [(name, concrete) for name, concrete in bindings if concrete == normalized]
            if len(exact) > 1:
                raise ProjectionError("normalized_value_collision")
            if exact:
                return "slot_" + exact[0][0]
            for _, concrete in bindings:
                if concrete in normalized:
                    raise ProjectionError("value_leak")
            return normalized
        if isinstance(value, list):
            return [leaf(item) for item in value]
        return value

    return cast(dict[str, object], leaf(shape.as_json()))


def query_searchable_text(task: Task) -> str:
    bindings = _normalized_bindings(task)
    projection = {
        "body": _project_string(_task_body(task), bindings),
        "family": task.family,
        "shape": _project_shape(task.shape, bindings),
        "slot_names": sorted(name for name, _ in bindings),
        "v": 1,
    }
    raw = canonical_json_bytes(projection)
    if len(raw) > _MAX_PROJECTION_BYTES:
        raise ProjectionError("oversize")
    return raw.decode("utf-8")


def index_searchable_text(task: Task, bundle: Template) -> str:
    names = tuple(sorted(name for name, _ in (task.slot_values or ())))
    if names != bundle.slot_names:
        raise ProjectionError("value_leak")
    return query_searchable_text(task)


def hashed_features(text: str) -> dict[int, int]:
    tokens = _WORD.findall(text)
    features: dict[int, int] = {}
    items: list[tuple[bytes, bytes]] = [(b"u", token.encode()) for token in tokens]
    items.extend(
        (b"b", left.encode() + b"\x00" + right.encode()) for left, right in zip(tokens, tokens[1:])
    )
    for kind, payload in items:
        digest = hashlib.sha256(b"rrcv2-hash-v1\x00" + kind + b"\x00" + payload).digest()
        bucket = int.from_bytes(digest[:2], "big") % 512
        sign = 1 if digest[2] & 1 == 0 else -1
        features[bucket] = features.get(bucket, 0) + sign
        if features[bucket] == 0:
            del features[bucket]
    return features


@dataclass(frozen=True)
class _CosineV1:
    dot: int
    norm_product: int


def _cosine(left: dict[int, int], right: dict[int, int]) -> _CosineV1 | None:
    left_norm = sum(value * value for value in left.values())
    right_norm = sum(value * value for value in right.values())
    if left_norm == 0 or right_norm == 0:
        return None
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    return _CosineV1(dot, left_norm * right_norm)


def _cosine_order(
    left: tuple[_CosineV1, str, str],
    right: tuple[_CosineV1, str, str],
) -> int:
    """Order descending cosine without a float or square-root approximation."""

    left_cosine, left_ref, left_document = left
    right_cosine, right_ref, right_document = right
    left_sign = (left_cosine.dot > 0) - (left_cosine.dot < 0)
    right_sign = (right_cosine.dot > 0) - (right_cosine.dot < 0)
    if left_sign != right_sign:
        return -1 if left_sign > right_sign else 1
    left_squared = left_cosine.dot * left_cosine.dot * right_cosine.norm_product
    right_squared = right_cosine.dot * right_cosine.dot * left_cosine.norm_product
    if left_squared != right_squared:
        left_is_greater = (
            left_squared > right_squared if left_sign >= 0 else left_squared < right_squared
        )
        return -1 if left_is_greater else 1
    if left_ref != right_ref:
        return -1 if left_ref < right_ref else 1
    if left_document != right_document:
        return -1 if left_document < right_document else 1
    return 0


def rrf_score(lexical_rank: int | None, hash_rank: int | None) -> ScoreV1:
    def bounded(rank: int | None) -> int | None:
        if rank is None:
            return None
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ValueError("retrieval ranks must be positive non-bool integers")
        return rank if rank <= 22_000 else None

    lexical_rank = bounded(lexical_rank)
    hash_rank = bounded(hash_rank)
    if lexical_rank is None and hash_rank is None:
        return ScoreV1(0, 1)
    score = Fraction(0, 1)
    if lexical_rank is not None:
        score += Fraction(1, 60 + lexical_rank)
    if hash_rank is not None:
        score += Fraction(1, 60 + hash_rank)
    score /= Fraction(2, 61)
    return ScoreV1(score.numerator, score.denominator)


@dataclass(frozen=True)
class CaseDocumentV1:
    external_ref: str
    owner_scope: str
    family: str | None
    shape: StructuralShapeV1 | None
    slot_schema: tuple[tuple[str, tuple[str, ...]], ...]
    searchable_text: str
    v: int = 1

    def __post_init__(self) -> None:
        if _HEX64.fullmatch(self.external_ref) is None:
            raise ValueError("case external_ref must be lowercase SHA-256")
        Config(self.owner_scope)
        if (
            self.family is not None
            and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.family) is None
        ):
            raise ValueError("case family is invalid")
        if tuple(name for name, _ in self.slot_schema) != tuple(
            sorted(name for name, _ in self.slot_schema)
        ):
            raise ValueError("case slot schema must be sorted")
        if (
            not isinstance(self.searchable_text, str)
            or len(self.searchable_text.encode()) > 48 * 1024
        ):
            raise ValueError("case searchable text is invalid")
        if self.v != 1:
            raise ValueError("unknown case-document version")
        if len(self.canonical_bytes()) > 64 * 1024:
            raise ValueError("case document exceeds its byte cap")

    def as_json(self) -> dict[str, object]:
        return {
            "external_ref": self.external_ref,
            "family": self.family,
            "owner_scope": self.owner_scope,
            "searchable_text": self.searchable_text,
            "shape": None if self.shape is None else self.shape.as_json(),
            "slot_schema": {name: list(contexts) for name, contexts in self.slot_schema},
            "v": 1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())

    @property
    def document_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def case_document(task: Task, cfg: Config, bundle: Template) -> CaseDocumentV1:
    return CaseDocumentV1(
        external_ref=bundle.external_ref,
        owner_scope=cfg.owner_scope,
        family=task.family,
        shape=task.shape,
        slot_schema=bundle.slot_contexts,
        searchable_text=index_searchable_text(task, bundle),
    )


def parse_case_document(raw: bytes) -> CaseDocumentV1:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("case document is not JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "external_ref",
            "family",
            "owner_scope",
            "searchable_text",
            "shape",
            "slot_schema",
            "v",
        }
        or value.get("v") != 1
    ):
        raise ValueError("case document schema is invalid")
    shape_value = value.get("shape")
    shape = None
    if shape_value is not None:
        if not isinstance(shape_value, dict) or set(shape_value) != {
            "arg_types",
            "arity",
            "fields",
        }:
            raise ValueError("case shape schema is invalid")
        shape = StructuralShapeV1(
            tuple(cast(list[str], shape_value["arg_types"])),
            cast(int, shape_value["arity"]),
            tuple(cast(list[str], shape_value["fields"])),
        )
    slot_value = value.get("slot_schema")
    if not isinstance(slot_value, dict):
        raise ValueError("case slot schema is invalid")
    slot_schema = tuple(
        (name, tuple(cast(list[str], contexts))) for name, contexts in sorted(slot_value.items())
    )
    document = CaseDocumentV1(
        cast(str, value["external_ref"]),
        cast(str, value["owner_scope"]),
        cast(str | None, value["family"]),
        shape,
        slot_schema,
        cast(str, value["searchable_text"]),
    )
    if document.canonical_bytes() != raw:
        raise ValueError("case document is not canonical")
    return document


@dataclass(frozen=True)
class RetrievalObservationV1:
    external_ref: str
    document_sha256: str
    family: str | None
    shape: StructuralShapeV1 | None
    slot_schema: tuple[tuple[str, tuple[str, ...]], ...]
    searchable_text: str


class _Repository(Protocol):
    authority_id: str
    database_uuid: str

    def get_bundle(self, external_ref: str) -> bytes | None: ...

    def retrieval_observations(self, owner_scope: str) -> tuple[RetrievalObservationV1, ...]: ...

    def lexical_ranks(self, owner_scope: str, query: str) -> dict[tuple[str, str], int]: ...


class SQLiteHybridRetrieval:
    """Read-only deterministic hybrid retrieval over the acceptance repository."""

    def __init__(self, repository: _Repository) -> None:
        self._repository = repository
        self.authority_id = repository.authority_id
        self.database_uuid = repository.database_uuid
        self._templates: dict[str, Template] = {}
        self._classifications: dict[str, Literal["exact", "near"]] = {}

    def get_template(self, external_ref: str) -> Template | None:
        cached = self._templates.get(external_ref)
        if cached is not None:
            return cached
        raw = self._repository.get_bundle(external_ref)
        if raw is None:
            return None
        try:
            template = parse_template_bundle(raw)
        except (TemplateError, TypeError, ValueError):
            return None
        if template.external_ref != external_ref:
            return None
        self._templates[external_ref] = template
        return template

    def projection_failure(self, task: Task) -> str | None:
        """Return the deterministic query-projection code, if projection is unavailable."""

        try:
            query_searchable_text(task)
        except ProjectionError as exc:
            return exc.code
        return None

    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
        self._classifications = {}
        try:
            query = query_searchable_text(task)
        except ProjectionError:
            return []
        observations = self._repository.retrieval_observations(cfg.owner_scope)
        lexical = self._repository.lexical_ranks(cfg.owner_scope, query)
        query_features = hashed_features(query)
        hash_rows: list[tuple[_CosineV1, str, str]] = []
        for row in observations:
            cosine = _cosine(query_features, hashed_features(row.searchable_text))
            if cosine is not None:
                hash_rows.append((cosine, row.external_ref, row.document_sha256))
        hash_rows.sort(key=cmp_to_key(_cosine_order))
        hash_ranks = {
            (external_ref, document_sha): rank
            for rank, (_, external_ref, document_sha) in enumerate(hash_rows[:22000], 1)
        }
        ranked: list[tuple[ScoreV1, RetrievalObservationV1]] = []
        for row in observations:
            key = (row.external_ref, row.document_sha256)
            score = rrf_score(lexical.get(key), hash_ranks.get(key))
            if score >= cfg.tau_floor:
                ranked.append((score, row))
        ranked.sort(
            key=lambda item: (
                -Fraction(item[0].numerator, item[0].denominator),
                item[1].external_ref,
                item[1].document_sha256,
            )
        )
        ranked = ranked[: cfg.top_k]
        exact: dict[str, tuple[ScoreV1, RetrievalObservationV1]] = {}
        near: dict[str, tuple[ScoreV1, RetrievalObservationV1]] = {}
        for score, row in ranked:
            template = self.get_template(row.external_ref)
            if template is None or template.slot_contexts != row.slot_schema:
                continue
            target = None
            if task.shape is not None and row.shape is not None and task.shape == row.shape:
                target = exact
            elif task.family is not None and row.family == task.family:
                target = near
            if target is not None and row.external_ref not in target:
                target[row.external_ref] = (score, row)
        for external_ref in exact:
            near.pop(external_ref, None)
        selected = list(exact.items()) if exact else list(near.items())[:2]
        self._classifications = (
            {external_ref: "exact" for external_ref in exact}
            if exact
            else {external_ref: "near" for external_ref in near}
        )
        return [Candidate(external_ref, record[0]) for external_ref, record in selected]

    def classify(self, task: Task, external_ref: str) -> Literal["exact", "near", "miss"]:
        del task
        return self._classifications.get(external_ref, "miss")
