from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://ollama.com", "https://ollama.com"),
        ("https://ollama.com/v1", "https://ollama.com"),
        ("https://example.test:9443/v1/", "https://example.test:9443"),
        ("https://[::1]:9443/v1", "https://[::1]:9443"),
    ],
)
def test_normalize_ollama_api_base_to_true_origin(value: str, expected: str) -> None:
    from contextmesh.scripts.rrd_origin import normalize_origin

    assert normalize_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://ollama.test/v1",
        "https://user:secret@ollama.test/v1",
        "https://ollama.test/v1/responses",
        "https://ollama.test//",
        "https://ollama.test/v1//",
        "https://ollama.test/v2",
        "https://ollama.test/v1?debug=1",
        "https://ollama.test:invalid/v1",
    ],
)
def test_normalize_ollama_api_base_rejects_unsafe_or_ambiguous_values(value: str) -> None:
    from contextmesh.scripts.rrd_origin import normalize_origin

    with pytest.raises(ValueError):
        normalize_origin(value)
