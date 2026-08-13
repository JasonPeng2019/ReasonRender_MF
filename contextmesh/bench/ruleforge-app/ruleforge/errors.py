"""Typed errors reserved for configuration and registry boundaries."""


class RuleForgeError(Exception):
    pass


class DuplicateRuleError(RuleForgeError):
    pass


class UnknownRuleError(RuleForgeError):
    pass


class InvalidRuleConfiguration(RuleForgeError):
    pass
