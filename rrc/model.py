"""Single-completion Codex CLI adapter for the frozen Lane A model port."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rrc.contract import Completion, ModelPort, ModelRole, RunContext, Usage

_SHAPE_PREFIX = "RRC_SHAPE:"
_VALUES_PREFIX = "RRC_SLOT_VALUES:"


def _marker_object(prompt: str, prefix: str) -> dict[str, Any] | None:
    matches = [
        line[len(prefix) :].strip() for line in prompt.splitlines() if line.startswith(prefix)
    ]
    if len(matches) != 1:
        return None
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _array_schema(allowed: list[str], *, exact_length: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array"}
    if not allowed:
        schema["maxItems"] = 0
        return schema
    schema["items"] = {"type": "string", "enum": allowed}
    length = len(allowed) if exact_length is None else exact_length
    schema["minItems"] = length
    schema["maxItems"] = length
    return schema


def _spec_output_schema(prompt: str) -> dict[str, Any] | None:
    """Build a task-specific structured-output fence for the strict SPEC artifact."""

    shape = _marker_object(prompt, _SHAPE_PREFIX)
    values = _marker_object(prompt, _VALUES_PREFIX)
    if (
        shape is None
        or values is None
        or any(not isinstance(value, str) for value in values.values())
    ):
        return None
    concrete = {str(key): str(value) for key, value in values.items()}
    fields_value = shape.get("fields")
    fields = (
        list(fields_value)
        if isinstance(fields_value, list) and all(isinstance(value, str) for value in fields_value)
        else []
    )

    def selected(predicate: Callable[[str], bool]) -> list[str]:
        return [value for key, value in concrete.items() if predicate(key.lower())]

    identifiers = selected(
        lambda key: (
            key in {"function", "identifier"}
            or key.endswith("_function")
            or key.endswith("_identifier")
        )
    )
    types = selected(lambda key: key == "type" or key.endswith("_type"))
    constants = selected(
        lambda key: (
            key in {"constant", "number"} or key.endswith("_constant") or key.endswith("_number")
        )
    )
    edge_values = selected(
        lambda key: key == "edge" or key.startswith("edge_") or key.endswith("_edge")
    )
    entity = concrete.get("entity")
    function = concrete.get("function")
    number = concrete.get("number")
    signature_schema: dict[str, Any] = {"type": "string"}
    if function is not None:
        signature_match = re.search(
            rf"\bImplement\s+{re.escape(function)}\(([^()\n]*)\)\s*->\s*([^\s.,]+)",
            prompt,
        )
        if signature_match is not None:
            parameters, return_type = signature_match.groups()
            signature_schema["enum"] = [
                f"def {function}({parameters.strip()}) -> {return_type.strip()}"
            ]
    tests_schema: dict[str, Any] = {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": 3,
    }
    if function is not None and number is not None:
        tests_schema = {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [f"def test_behavior():\n    assert {function}(99) == {number}"],
            },
            "minItems": 1,
            "maxItems": 1,
        }
    category_properties = {
        "entity": {"type": "null"} if entity is None else {"type": "string", "enum": [entity]},
        "identifiers": _array_schema(identifiers),
        "types": _array_schema(types),
        "fields": _array_schema(fields, exact_length=len(fields)),
        "constants": _array_schema(constants),
        "edge_values": _array_schema(edge_values),
        "values": {
            "type": "object",
            "properties": {
                key: {"type": "string", "enum": [value]} for key, value in concrete.items()
            },
            "required": list(concrete),
            "additionalProperties": False,
        },
    }
    return {
        "type": "object",
        "properties": {
            "plan": {"type": "string"},
            "signature": signature_schema,
            "contract": {"type": "string"},
            "tests": tests_schema,
            "slots": {
                "type": "object",
                "properties": category_properties,
                "required": list(category_properties),
                "additionalProperties": False,
            },
        },
        "required": ["plan", "signature", "contract", "tests", "slots"],
        "additionalProperties": False,
    }


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
        artifact_log: str | Path | None = None,
    ) -> None:
        self._models = {
            ModelRole.STRONG: strong_model,
            ModelRole.SMALL: small_model or strong_model,
        }
        self._executable = executable
        self._artifact_log = Path(artifact_log) if artifact_log is not None else None

    def _record(
        self,
        *,
        role: ModelRole,
        model: str,
        prompt: str,
        ctx: RunContext,
        stage: str,
        text: str,
        usage: Usage,
    ) -> None:
        if self._artifact_log is None:
            return
        self._artifact_log.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "arm": ctx.arm,
            "task_id": ctx.task_id,
            "stage": stage,
            "role": role.value,
            "model": model,
            "prompt": prompt,
            "response": text,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
        }
        with self._artifact_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def complete(
        self,
        role: ModelRole,
        prompt: str,
        ctx: RunContext,
        stage: str,
    ) -> Completion:
        """Return exactly one typed completion with provider-reported usage."""

        model = self._models[role]
        command = [
            self._executable,
            "exec",
            "--json",
            # Artifact calls must not inherit repository AGENTS instructions:
            # those add thousands of irrelevant tokens and can conflict with
            # the strict JSON/code-only stage contract.
            "--ignore-rules",
            # RRC persists its own typed evidence; do not create a full Codex
            # session rollout for every single-stage completion.
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            model,
        ]
        schema = _spec_output_schema(prompt) if stage in {"spec", "fallback_spec"} else None
        try:
            if schema is None:
                result = subprocess.run(
                    [*command, prompt], capture_output=True, text=True, check=False
                )
            else:
                with tempfile.TemporaryDirectory(prefix="rrc-schema-") as directory:
                    schema_path = Path(directory) / "spec-output.schema.json"
                    schema_path.write_text(
                        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    result = subprocess.run(
                        [*command, "--output-schema", str(schema_path), prompt],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
        except OSError as exc:
            raise RuntimeError(f"could not start codex exec: {exc}") from exc
        if result.returncode != 0:
            detail = f": {result.stderr.strip()}" if result.stderr.strip() else ""
            raise RuntimeError(f"codex exec failed with exit code {result.returncode}{detail}")

        text, usage = parse_codex_jsonl(result.stdout)
        self._record(
            role=role,
            model=model,
            prompt=prompt,
            ctx=ctx,
            stage=stage,
            text=text,
            usage=usage,
        )
        return Completion(text=text, usage=usage, model=model)
