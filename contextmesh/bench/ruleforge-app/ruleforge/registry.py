"""The registry is the only source of truth for policy definitions."""
from __future__ import annotations

from .domain import RuleDefinition, stable_rule_key
from .errors import DuplicateRuleError, UnknownRuleError


class RuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, tuple[RuleDefinition, object]] = {}

    def register(self, definition: RuleDefinition, expected: object) -> None:
        key = stable_rule_key(definition)
        if key in self._rules:
            raise DuplicateRuleError(key)
        self._rules[key] = (definition, expected)

    def get(self, key: str) -> tuple[RuleDefinition, object]:
        try:
            return self._rules[key]
        except KeyError as error:
            raise UnknownRuleError(key) from error

    def all(self) -> tuple[tuple[RuleDefinition, object], ...]:
        return tuple(self._rules.values())
