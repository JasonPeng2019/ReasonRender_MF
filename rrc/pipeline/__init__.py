"""Public Lane A pipeline seam."""

from typing import Any

from rrc.contract import Solver


def solve(*args: Any, **kwargs: Any) -> Any:
    """Lazily dispatch to the canonical engine without import cycles."""

    from rrc.pipeline.solve import solve as implementation

    return implementation(*args, **kwargs)


__all__ = ["Solver", "solve"]
