"""Single-completion Codex CLI adapter for the frozen Lane A model port."""

from __future__ import annotations

import hashlib
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
    edge_values = selected(
        lambda key: key == "edge" or key.startswith("edge_") or key.endswith("_edge")
    )
    entity = concrete.get("entity")
    categorized = {
        *identifiers,
        *types,
        *edge_values,
        *((entity,) if entity is not None else ()),
    }
    # Every remaining controller binding is a concrete literal constant.  This
    # includes domain-named slots such as ``high``, ``delimiter``,
    # ``replacement``, ``active_value``, and ``discount_percent`` in the frozen
    # workload; restricting constants to names ending in ``_constant`` made the
    # canonical Spec schema impossible for all five families.
    constants = [value for value in concrete.values() if value not in categorized]
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
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def parse_codex_jsonl(stdout: str) -> tuple[str, Usage]:
    """Validate one zero-tool Codex turn and return its sole final plus usage."""

    if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("codex exec JSONL exceeds its output cap")
    thread_ids: list[str] = []
    turn_started = 0
    messages: list[str] = []
    usages: list[dict[str, object]] = []
    for line in stdout.splitlines():
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("codex exec emitted malformed JSONL") from exc
        if not isinstance(event, dict):
            raise ValueError("codex exec JSONL row is not an object")
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise ValueError("codex exec thread identity is invalid")
            thread_ids.append(thread_id)
        elif event_type == "turn.started":
            turn_started += 1
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    messages.append(text)
                    continue
            raise ValueError("codex exec used a tool or emitted a non-final item")
        elif event_type == "turn.completed":
            candidate = event.get("usage")
            if isinstance(candidate, dict):
                usages.append(candidate)
                continue
            raise ValueError("codex exec completed without a usage object")
        else:
            raise ValueError("codex exec emitted an error, retry, or unknown event")

    if len(thread_ids) != 1 or len(set(thread_ids)) != 1 or turn_started != 1:
        raise ValueError("codex exec JSONL did not contain exactly one thread/turn start")
    if len(messages) != 1:
        raise ValueError("codex exec JSONL did not contain exactly one completed agent message")
    if len(usages) != 1:
        raise ValueError("codex exec JSONL did not contain exactly one completed turn usage")

    usage = usages[0]
    prompt_tokens = _token_count(usage.get("input_tokens"))
    completion_tokens = _token_count(usage.get("output_tokens"))
    cached_input_tokens = _token_count(usage.get("cached_input_tokens"))
    reasoning_output_tokens = _token_count(usage.get("reasoning_output_tokens"))
    total_tokens = _token_count(usage.get("total_tokens"))
    if (
        prompt_tokens is None
        or completion_tokens is None
        or cached_input_tokens is None
        or reasoning_output_tokens is None
    ):
        raise ValueError("codex exec JSONL usage has no exact token-component counts")
    computed_total = prompt_tokens + completion_tokens
    if total_tokens is not None and total_tokens != computed_total:
        raise ValueError("codex exec total usage does not reconcile")
    return messages[0], Usage(
        prompt_tokens,
        completion_tokens,
        computed_total,
        cached_input_tokens,
        reasoning_output_tokens,
    )


class CodexModel(ModelPort):
    """Run one noninteractive, read-only Codex completion per Lane A stage."""

    provider = "openai"

    def __init__(
        self,
        *,
        strong_model: str = "gpt-5.5",
        small_model: str = "gpt-5.6-luna",
        executable: str = "codex",
        artifact_log: str | Path | None = None,
    ) -> None:
        self._models = {
            ModelRole.STRONG: strong_model,
            ModelRole.SMALL: small_model,
        }
        self._executable = executable
        self._artifact_log = Path(artifact_log) if artifact_log is not None else None
        self.product_cell_id: str | None = None

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
                "cached_input_tokens": usage.cached_input_tokens,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "reasoning_output_tokens": usage.reasoning_output_tokens,
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
        reasoning = (
            "medium"
            if role is ModelRole.STRONG and stage in {"baseline", "cascade_strong"}
            else "low"
        )
        schema = _spec_output_schema(prompt) if stage in {"spec", "fallback_spec"} else None
        try:
            with tempfile.TemporaryDirectory(prefix="rrc-stage-") as directory:
                cwd = Path(directory)
                cwd.chmod(0o700)
                command = [
                    self._executable,
                    "-a",
                    "never",
                    "exec",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--disable",
                    "hooks",
                    "--disable",
                    "plugins",
                    "--disable",
                    "multi_agent",
                    "--ephemeral",
                    "--json",
                    "--skip-git-repo-check",
                    "-s",
                    "read-only",
                    "-C",
                    str(cwd),
                    "-m",
                    model,
                    "-c",
                    'cli_auth_credentials_store="keyring"',
                    "-c",
                    f'model_reasoning_effort="{reasoning}"',
                    "-c",
                    'service_tier="priority"',
                ]
                if schema is not None:
                    schema_path = cwd / "output.schema.json"
                    schema_path.write_text(
                        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    schema_path.chmod(0o600)
                    command.extend(["--output-schema", str(schema_path)])
                command.append("-")
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=300,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
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
        return Completion(
            text=text,
            usage=usage,
            model=model,
            transcript_sha256=hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
            identity_attestation="native_partial",
            effective_provider="openai",
            effective_reasoning=reasoning,
            effective_service_tier="unattested",
        )
