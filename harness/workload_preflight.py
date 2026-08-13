"""Local source-mass capacity gate for the measured four-worker workload."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from harness.four_worker_plan import WorkerPlan

# This is capacity only, not an imputed billed-token saving. V13's roughly
# 112KB duplicate topology produced only a 9.6% worker signal, so the floor
# prevents another paid run whose repeated bodies cannot plausibly overcome the
# fixed worker/tool overhead at the required 15% gate.
MIN_DUPLICATE_SOURCE_BYTES = 240_000


def source_mass(workspace: str | Path, plans: Iterable[WorkerPlan]) -> dict[str, Any]:
    """Measure direct raw-source repetition for the frozen plan read sets."""

    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"workspace must be an existing directory: {root}")
    contracts = tuple(plans)
    counts: Counter[str] = Counter(path for plan in contracts for path in plan.initial_read_paths)
    sizes: dict[str, int] = {}
    for path in counts:
        candidate = (root / path).resolve()
        if root != candidate and root not in candidate.parents:
            raise ValueError(f"declared source escapes workspace: {path}")
        if not candidate.is_file():
            raise ValueError(f"declared source is missing from workload: {path}")
        sizes[path] = candidate.stat().st_size
    raw_direct_bytes = sum(sizes[path] * count for path, count in counts.items())
    one_read_each_bytes = sum(sizes.values())
    duplicate_source_bytes = raw_direct_bytes - one_read_each_bytes
    return {
        "schema_version": 1,
        "raw_direct_source_bytes": raw_direct_bytes,
        "one_read_each_source_bytes": one_read_each_bytes,
        "duplicate_source_bytes": duplicate_source_bytes,
        "duplicate_source_fraction": round(duplicate_source_bytes / raw_direct_bytes, 6) if raw_direct_bytes else 0.0,
        "paths": [
            {"canonical_path": path, "raw_readers": counts[path], "bytes": sizes[path]}
            for path in sorted(counts)
        ],
    }


def require_capacity(stats: dict[str, Any], minimum_duplicate_bytes: int = MIN_DUPLICATE_SOURCE_BYTES) -> None:
    """Reject a workload with insufficient real repeated source to measure fairly."""

    duplicate = stats.get("duplicate_source_bytes")
    if not isinstance(duplicate, int) or duplicate < minimum_duplicate_bytes:
        raise ValueError(
            "ContextMesh workload has insufficient duplicate source capacity: "
            f"{duplicate!r} bytes; need at least {minimum_duplicate_bytes} bytes before a paid comparison"
        )


__all__ = ["MIN_DUPLICATE_SOURCE_BYTES", "require_capacity", "source_mass"]
