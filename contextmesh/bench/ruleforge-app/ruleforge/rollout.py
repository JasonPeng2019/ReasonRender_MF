"""Versioned rollout contract consumed by staged policy work."""
from __future__ import annotations


STAGE_REVISION = "stage-00"


def rollout_revision() -> str:
    return STAGE_REVISION
