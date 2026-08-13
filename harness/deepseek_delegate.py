"""External-Qwen DeepSeek Flash worker command contract.

Measured workers use direct, persistent Qwen Code processes against the
Ollama-hosted DeepSeek model. Their JSONL stream is the durable per-worker
session pointer used by the staged runner after an interruption.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROFILE_RELATIVE = Path(".codex/delegates/deepseek.toml")
MODEL = "deepseek-v4-flash:0731-cloud"
CONTEXT_WINDOW = 1_048_576
AUTO_COMPACT_TOKEN_LIMIT = 230_000


@dataclass(frozen=True, slots=True)
class DeepSeekProfile:
    model: str
    model_reasoning_effort: str
    model_context_window: int
    model_auto_compact_token_limit: int
    model_auto_compact_token_limit_scope: str
    local_provider: str
    model_catalog: Path
    developer_instructions: str


def load_profile(repo_root: str | Path) -> DeepSeekProfile:
    """Load the checked-in external-delegate profile and validate its catalog."""

    root = Path(repo_root).resolve()
    path = root / PROFILE_RELATIVE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"invalid DeepSeek delegate profile: {path}") from error
    required = (
        "model",
        "model_reasoning_effort",
        "model_context_window",
        "model_auto_compact_token_limit",
        "model_auto_compact_token_limit_scope",
        "local_provider",
        "model_catalog_json",
        "developer_instructions",
    )
    if any(key not in data for key in required):
        raise ValueError("DeepSeek delegate profile is missing a required setting")
    catalog = path.parent / str(data["model_catalog_json"])
    try:
        catalog_data = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid DeepSeek model catalog: {catalog}") from error
    models = catalog_data.get("models") if isinstance(catalog_data, dict) else None
    if not isinstance(models, list) or not any(
        isinstance(item, dict)
        and item.get("slug") == data["model"]
        and item.get("context_window") == data["model_context_window"]
        for item in models
    ):
        raise ValueError("DeepSeek model catalog does not bind the selected profile model and context window")
    profile = DeepSeekProfile(
        model=str(data["model"]),
        model_reasoning_effort=str(data["model_reasoning_effort"]),
        model_context_window=int(data["model_context_window"]),
        model_auto_compact_token_limit=int(data["model_auto_compact_token_limit"]),
        model_auto_compact_token_limit_scope=str(data["model_auto_compact_token_limit_scope"]),
        local_provider=str(data["local_provider"]),
        model_catalog=catalog,
        developer_instructions=str(data["developer_instructions"]),
    )
    if profile.model != MODEL or profile.model_context_window != CONTEXT_WINDOW:
        raise ValueError("DeepSeek delegate profile drifted from the measured worker model contract")
    if not 0 < profile.model_auto_compact_token_limit < profile.model_context_window:
        raise ValueError("DeepSeek auto-compaction threshold must be below its context window")
    if profile.local_provider != "ollama" or profile.model_auto_compact_token_limit != AUTO_COMPACT_TOKEN_LIMIT:
        raise ValueError("DeepSeek delegate provider or auto-compaction contract drifted")
    return profile


def command(
    repo_root: str | Path,
    final: Path,
    *,
    resume_thread_id: str | None = None,
    extra_config: Sequence[str] = (),
    enabled_features: Sequence[str] = (),
    codex_bin: str | None = None,
) -> tuple[str, ...]:
    """Build a full-access initial or resume Qwen command for one worker.

    ``codex_bin`` remains as a compatibility keyword for existing callers and
    tests; it now selects the Qwen executable passed to the isolated wrapper.
    """

    root = Path(repo_root).resolve()
    load_profile(root)
    binary = codex_bin or os.environ.get("STAGED_QWEN_BIN", "qwen")
    result: list[str] = [
        sys.executable,
        "-m",
        "harness.qwen_delegate",
        "--repo",
        str(root),
        "--final",
        str(final),
        "--qwen-bin",
        binary,
    ]
    if resume_thread_id is not None:
        result.extend(("--resume-session-id", resume_thread_id))
    if extra_config:
        if not all(item.startswith("mcp_servers.contextmesh.") for item in extra_config):
            raise ValueError("Qwen worker received unsupported external configuration")
        result.append("--contextmesh")
    # Qwen has no Codex feature toggles. Retain the parameter so historical
    # probe callers do not need a second API shape during this runner swap.
    del enabled_features
    return tuple(result)


__all__ = [
    "AUTO_COMPACT_TOKEN_LIMIT",
    "CONTEXT_WINDOW",
    "DeepSeekProfile",
    "MODEL",
    "command",
    "load_profile",
]
