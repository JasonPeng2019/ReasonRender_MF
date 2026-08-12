from __future__ import annotations

import hashlib
from functools import cmp_to_key

import pytest
from rrc import retrieval as retrieval_module
from rrc.contract import Config, ScoreV1, Slots, Spec, StructuralShapeV1, Task
from rrc.pipeline.template import template_bundle_bytes, templatize
from rrc.retrieval import (
    ProjectionError,
    ProjectionUnavailableV1,
    RetrievalObservationV1,
    SQLiteHybridRetrieval,
    hashed_features,
    parse_projection_unavailable,
    projection_unavailable,
    query_searchable_text,
    rrf_score,
)


def _task(text: str, values: tuple[tuple[str, str], ...]) -> Task:
    return Task(
        "task-1",
        text,
        family="lookup",
        primary="get_order",
        shape=StructuralShapeV1(("int",), 1, ()),
        slot_values=values,
    )


def test_projection_and_hashed_feature_goldens_match_frozen_design() -> None:
    projection = query_searchable_text(
        _task(
            "Lookup one integer and return constant 1.",
            (("constant", "1"), ("function", "get_order")),
        )
    )
    assert projection == (
        '{"body":"lookup one integer and return constant slot_constant.",'
        '"family":"lookup","shape":{"arg_types":["int"],"arity":1,"fields":[]},'
        '"slot_names":["constant","function"],"v":1}'
    )
    assert hashlib.sha256(projection.encode()).hexdigest() == (
        "856360d631036cedf13df9e60ecfd38a6b2921c6805b186fdd17f540c6bc308f"
    )
    assert hashed_features("lookup order id") == {
        74: -1,
        59: -1,
        256: 1,
        193: 1,
        317: -1,
    }
    assert hashed_features("lookup user id") == {
        74: -1,
        207: -1,
        256: 1,
        391: 1,
        152: -1,
    }
    assert rrf_score(1, 2) == ScoreV1(123, 124)
    assert rrf_score(2, 1) == ScoreV1(123, 124)
    assert rrf_score(22_001, 22_001) == ScoreV1(0, 1)
    assert rrf_score(22_001, 22_000) == ScoreV1(61, 44_120)
    assert rrf_score(22_000, 22_000).denominator < 1_000_000_000
    for invalid in (True, 0, -1):
        with pytest.raises(ValueError, match="retrieval ranks"):
            rrf_score(invalid, None)  # type: ignore[arg-type]


def test_hash_cosine_order_is_exact_for_positive_negative_and_ties() -> None:
    rows = [
        (retrieval_module._CosineV1(1, 2), "b" * 64, "1" * 64),  # noqa: SLF001
        (retrieval_module._CosineV1(2, 5), "c" * 64, "2" * 64),  # noqa: SLF001
        (retrieval_module._CosineV1(-1, 2), "a" * 64, "3" * 64),  # noqa: SLF001
        (retrieval_module._CosineV1(-1, 8), "d" * 64, "4" * 64),  # noqa: SLF001
        (retrieval_module._CosineV1(2, 8), "a" * 64, "5" * 64),  # noqa: SLF001
    ]
    ordered = sorted(rows, key=cmp_to_key(retrieval_module._cosine_order))  # noqa: SLF001
    assert [(row[0].dot, row[0].norm_product, row[1]) for row in ordered] == [
        (2, 5, "c" * 64),
        (2, 8, "a" * 64),
        (1, 2, "b" * 64),
        (-1, 8, "d" * 64),
        (-1, 2, "a" * 64),
    ]


def test_projection_unavailable_store_golden_is_exact() -> None:
    record, evidence = projection_unavailable(
        attempt_id="1" * 64,
        phase="store",
        code="normalized_value_collision",
        input_bytes=b"A a",
    )
    raw = record.canonical_bytes()
    assert evidence == b"normalized_value_collision\n"
    assert len(raw) == 306
    assert hashlib.sha256(raw).hexdigest() == (
        "fe895a08f587cd88a30f0270a466ff322ec19e7bc39913c260afdd7d581699fd"
    )
    assert parse_projection_unavailable(raw) == record
    assert isinstance(record, ProjectionUnavailableV1)


@pytest.mark.parametrize(
    ("text", "values", "code"),
    [
        ("A a", (("first", "A"), ("second", "a")), "normalized_value_collision"),
        ("slot_secret SecretX", (("secret", "SecretX"),), "reserved_token"),
        ("abc", (("long", "abc"), ("short", "bc")), "overlap"),
        ("value ss", (("a", "ß"), ("b", "ss")), "normalized_value_collision"),
    ],
)
def test_projection_fails_closed_on_ambiguous_or_reserved_values(
    text: str,
    values: tuple[tuple[str, str], ...],
    code: str,
) -> None:
    with pytest.raises(ProjectionError, match=code):
        query_searchable_text(_task(text, values))


def test_inserted_placeholder_span_does_not_false_positive_as_a_value_leak() -> None:
    projection = query_searchable_text(_task("Return secret.", (("secret", "secret"),)))
    assert '"body":"return slot_secret."' in projection


def test_same_ref_exact_observation_dominates_near_observation() -> None:
    template = templatize(
        Spec(
            "Return the input.",
            "def get_order(x: int) -> int",
            "Return x.",
            ("def test_value():\n    assert get_order(1) == 1",),
            Slots(),
        )
    )

    class Repository:
        authority_id = "authority"
        database_uuid = "a" * 64

        def get_bundle(self, external_ref: str) -> bytes | None:
            return (
                template_bundle_bytes(template) if external_ref == template.external_ref else None
            )

        def retrieval_observations(self, owner_scope: str):
            assert owner_scope == "owner"
            return (
                RetrievalObservationV1(
                    template.external_ref,
                    "1" * 64,
                    "lookup",
                    StructuralShapeV1(("str",), 1, ()),
                    template.slot_contexts,
                    "lookup near",
                ),
                RetrievalObservationV1(
                    template.external_ref,
                    "2" * 64,
                    "other",
                    StructuralShapeV1(("int",), 1, ()),
                    template.slot_contexts,
                    "lookup exact",
                ),
            )

        def lexical_ranks(self, owner_scope: str, query: str):
            del owner_scope, query
            return {(template.external_ref, "1" * 64): 1, (template.external_ref, "2" * 64): 2}

    retrieval = SQLiteHybridRetrieval(Repository())
    query = _task("Lookup one integer.", ())
    hits = retrieval.retrieve(query, Config("owner"))
    assert [hit.external_ref for hit in hits] == [template.external_ref]
    assert retrieval.classify(query, template.external_ref) == "exact"


def test_structural_exact_survives_cache_render_rejection_for_fresh_fallback() -> None:
    miss_spec = Spec(
        "Implement the User label method.",
        "class User:\n    def label(self) -> str: ...",
        "User.label returns the string User.",
        ('def test_label():\n    assert User().label() == "User"',),
        Slots(entity="User"),
    )
    template = templatize(
        miss_spec,
        ('def test_user_label():\n    assert User().label() == "User"',),
        slot_values=(("entity", "User"),),
        primary="User.label",
    )
    assert template.slot_contexts == (("entity", ("identifier", "string_content", "text")),)

    near = Task(
        task_id="rrcv2-cli-near-001",
        text=(
            'Implement label_entity() so it returns the entity label "order item". '
            "Preserve the zero-argument public function API."
        ),
        family="entity_label",
        artifact_path="rrcv2_demo/entity.py",
        primary="label_entity",
        shape=StructuralShapeV1((), 0, ()),
        slot_values=(("entity", "order item"),),
    )

    class Repository:
        authority_id = "authority"
        database_uuid = "a" * 64

        def get_bundle(self, external_ref: str) -> bytes | None:
            return (
                template_bundle_bytes(template) if external_ref == template.external_ref else None
            )

        def retrieval_observations(self, owner_scope: str):
            assert owner_scope == "owner"
            return (
                RetrievalObservationV1(
                    template.external_ref,
                    "3" * 64,
                    "entity_label",
                    StructuralShapeV1((), 0, ()),
                    template.slot_contexts,
                    query_searchable_text(near),
                ),
            )

        def lexical_ranks(self, owner_scope: str, query: str):
            assert owner_scope == "owner"
            assert query == query_searchable_text(near)
            return {(template.external_ref, "3" * 64): 1}

    retrieval = SQLiteHybridRetrieval(Repository())
    hits = retrieval.retrieve(near, Config("owner"))
    assert [hit.external_ref for hit in hits] == [template.external_ref]
    assert retrieval.classify(near, template.external_ref) == "exact"
