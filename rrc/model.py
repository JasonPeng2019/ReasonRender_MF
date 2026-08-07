"""Single-shot Codex CLI completion adapter."""

from __future__ import annotations

import json
import subprocess


def parse_codex_jsonl(stdout: str) -> tuple[str, int]:
    """Extract the final agent message and token usage from Codex JSONL."""

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

    total_tokens = usage.get("total_tokens")
    if isinstance(total_tokens, (int, float)) and not isinstance(total_tokens, bool):
        return message, int(total_tokens)

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if (
        isinstance(input_tokens, (int, float))
        and not isinstance(input_tokens, bool)
        and isinstance(output_tokens, (int, float))
        and not isinstance(output_tokens, bool)
    ):
        return message, int(input_tokens + output_tokens)

    raise ValueError(
        "codex exec JSONL usage has no numeric total_tokens or input_tokens/output_tokens"
    )


class CodexModel:
    """Run one noninteractive, read-only Codex CLI completion."""

    def complete(self, prompt: str, model: str) -> tuple[str, int]:
        """Return the completion text and Codex's reported token count."""

        command = [
            "codex",
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
            detail = result.stderr.strip()
            if detail:
                detail = f": {detail}"
            raise RuntimeError(
                f"codex exec failed with exit code {result.returncode}{detail}"
            )

        return parse_codex_jsonl(result.stdout)

    def __call__(self, prompt: str, model: str) -> tuple[str, int]:
        """Make the model directly compatible with the Complete seam."""

        return self.complete(prompt, model)
