"""Single-completion Codex CLI adapter for the frozen Lane A model port."""

from __future__ import annotations

import json
import subprocess

from rrc.contract import Completion, ModelPort, ModelRole, RunContext, Usage


def _token_count(value: object) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return None


def parse_codex_jsonl(stdout: str) -> tuple[str, Usage]:
    """Extract the final agent artifact and reported usage from Codex JSONL."""

    message: str | None = None
    usage: dict[str, object] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    message = text
        elif event.get("type") == "turn.completed":
            candidate = event.get("usage")
            if isinstance(candidate, dict):
                usage = candidate

    if message is None:
        raise ValueError("codex exec JSONL contained no completed agent message")
    if usage is None:
        raise ValueError("codex exec JSONL contained no completed turn usage")

    prompt_tokens = _token_count(usage.get("input_tokens"))
    completion_tokens = _token_count(usage.get("output_tokens"))
    total_tokens = _token_count(usage.get("total_tokens"))
    if prompt_tokens is None or completion_tokens is None:
        if total_tokens is None:
            raise ValueError("codex exec JSONL usage has no numeric token counts")
        # Some Codex versions expose only a total. Preserve that cost without
        # inventing completion usage.
        prompt_tokens, completion_tokens = total_tokens, 0
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    return message, Usage(prompt_tokens, completion_tokens, total_tokens)


class CodexModel(ModelPort):
    """Run one noninteractive, read-only Codex completion per Lane A stage."""

    provider = "codex"

    def __init__(
        self,
        *,
        strong_model: str = "gpt-5",
        small_model: str | None = None,
        executable: str = "codex",
    ) -> None:
        self._models = {
            ModelRole.STRONG: strong_model,
            ModelRole.SMALL: small_model or strong_model,
        }
        self._executable = executable

    def complete(
        self,
        role: ModelRole,
        prompt: str,
        ctx: RunContext,
        stage: str,
    ) -> Completion:
        """Return exactly one typed completion with provider-reported usage."""

        del ctx, stage
        model = self._models[role]
        command = [
            self._executable,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            model,
            prompt,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise RuntimeError(f"could not start codex exec: {exc}") from exc
        if result.returncode != 0:
            detail = f": {result.stderr.strip()}" if result.stderr.strip() else ""
            raise RuntimeError(f"codex exec failed with exit code {result.returncode}{detail}")

        text, usage = parse_codex_jsonl(result.stdout)
        return Completion(text=text, usage=usage, model=model)
