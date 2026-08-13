"""Service facade used by callers after transport normalization."""
from __future__ import annotations

from collections.abc import Mapping

from .evaluator import evaluate_all
from .normalizer import normalize_payload
from .registry import RuleRegistry


class PolicyService:
    def __init__(self, registry: RuleRegistry) -> None:
        self.registry = registry

    def evaluate(self, payload: Mapping[str, object], subject_id: str, request_id: str):
        data = normalize_payload(payload, subject_id, request_id)
        return evaluate_all(self.registry.all(), data)
