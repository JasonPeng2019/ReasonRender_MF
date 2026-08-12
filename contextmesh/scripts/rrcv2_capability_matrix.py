#!/usr/bin/env python3
"""One-shot eight-surface/nine-call installed-Codex capability matrix."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

from contextmesh.scripts.rrcv2_capability_preflight import validate_sealed as validate_preflight
from rrc.dispatch_permit import (
    CALLS,
    CLI_VERSION,
    AuthorityRef,
    authorize_capability,
    canonical_json,
    capability_manifest,
)

STDOUT_CAP = 64 * 1024 * 1024
STDERR_CAP = 1024 * 1024
LINE_CAP = 4 * 1024 * 1024
EVENT_CAP = 32_768
FINAL_CAP = 2 * 1024 * 1024
CALL_TIMEOUT = 300
ROOT_TIMEOUT = 600
PLAN_SEAL_NAME = "plan-capability-preimplementation.seal.json"
ROOT_SENTINEL = "RRCV2_ROOT_PRIVATE_SENTINEL_8d0cbf61"


class MatrixError(RuntimeError):
    """Capability evidence is unavailable, conflicting, or not exact."""


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MatrixError(f"invalid integer field: {field}")
    return value


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_exclusive(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), mode)
    except FileExistsError as exc:
        raise MatrixError(f"sealed path already exists: {path}") from exc
    try:
        os.fchmod(fd, mode)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise MatrixError(f"short write: {path}")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_regular(path: Path, cap: int, *, mode: int = 0o600) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise MatrixError(f"cannot stat {path}") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_size > cap
    ):
        raise MatrixError(f"invalid sealed file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise MatrixError(f"cannot safely open {path}") from exc
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or (before.st_dev, before.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise MatrixError(f"sealed file changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while total <= cap:
            chunk = os.read(fd, min(65_536, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if total > cap or (current.st_size, current.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise MatrixError(f"sealed file changed or exceeded cap: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _load_canonical(path: Path, cap: int = 4 * 1024 * 1024) -> dict[str, Any]:
    raw = _read_regular(path, cap)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MatrixError(f"invalid canonical JSON: {path}") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise MatrixError(f"noncanonical JSON: {path}")
    return value


def _schema(kind: str) -> dict[str, object]:
    if kind == "code":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "v": {"type": "integer", "const": 1},
                "attempt_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "artifact_path": {"type": "string", "const": "solution.py"},
                "source": {"type": "string"},
            },
            "required": ["v", "attempt_id", "artifact_path", "source"],
        }
    if kind == "spec":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "plan": {"type": "string"},
                "signature": {"type": "string"},
                "contract": {"type": "string"},
                "tests": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "slots": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "constants": {"type": "array", "items": {"type": "string"}},
                        "edge_values": {"type": "array", "items": {"type": "string"}},
                        "entity": {"type": ["string", "null"]},
                        "fields": {"type": "array", "items": {"type": "string"}},
                        "identifiers": {"type": "array", "items": {"type": "string"}},
                        "types": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "constants",
                        "edge_values",
                        "entity",
                        "fields",
                        "identifiers",
                        "types",
                    ],
                },
            },
            "required": ["plan", "signature", "contract", "tests", "slots"],
        }
    if kind == "tests":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "v": {"type": "integer", "const": 1},
                "tests": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["v", "tests"],
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "v": {"type": "integer", "const": 1},
            "primary": {"type": "string"},
            "shape": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "arity": {"type": "integer"},
                    "arg_types": {"type": "array", "items": {"type": "string"}},
                    "fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["arity", "arg_types", "fields"],
            },
            "slot_values": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "function": {"type": "string", "const": "answer"},
                    "constant": {"type": "integer", "const": 1},
                },
                "required": ["function", "constant"],
            },
        },
        "required": ["v", "primary", "shape", "slot_values"],
    }


def _answer(kind: str) -> dict[str, object]:
    if kind == "code":
        return {
            "v": 1,
            "attempt_id": "1" * 64,
            "artifact_path": "solution.py",
            "source": "def answer() -> int:\n    return 1\n",
        }
    if kind == "spec":
        return {
            "plan": "Return the declared constant.",
            "signature": "def answer(x: int) -> int",
            "contract": "Return one for every integer input.",
            "tests": ["def test_answer():\n    assert answer(4) == 1"],
            "slots": {
                "constants": ["1"],
                "edge_values": [],
                "entity": None,
                "fields": [],
                "identifiers": ["answer"],
                "types": ["int"],
            },
        }
    if kind == "tests":
        return {"v": 1, "tests": ["def test_answer():\n    assert answer(4) == 1"]}
    return {
        "v": 1,
        "primary": "answer",
        "shape": {"arity": 1, "arg_types": ["int"], "fields": []},
        "slot_values": {"function": "answer", "constant": 1},
    }


SURFACE_KIND = {
    "strong_code_medium": "code",
    "small_code_low": "code",
    "small_spec_low": "spec",
    "small_tests_low": "tests",
    "small_metadata_low": "metadata",
    "strong_spec_low": "spec",
}


def _prompt(surface_id: str) -> bytes:
    answer = _answer(SURFACE_KIND[surface_id])
    return (
        "This is a zero-tool RRCv2 capability framing probe. Do not call tools. Return only this "
        f"JSON object and no prose: {json.dumps(answer, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n"
    ).encode()


def _role(surface_id: str) -> tuple[str, str]:
    if surface_id == "strong_code_medium":
        return ("gpt-5.5", "medium")
    if surface_id == "strong_spec_low":
        return ("gpt-5.5", "low")
    return ("gpt-5.6-luna", "low")


def _direct_argv_template(surface_id: str) -> tuple[str, ...]:
    model, reasoning = _role(surface_id)
    return (
        "<CODEX>",
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
        "<CWD>",
        "-m",
        model,
        "-c",
        'cli_auth_credentials_store="keyring"',
        "-c",
        f'model_reasoning_effort="{reasoning}"',
        "-c",
        'service_tier="priority"',
        "--output-schema",
        "<SCHEMA>",
        "-",
    )


def _root_prompt() -> bytes:
    return f"""RRCv2 native hierarchy capability. Root private sentinel: {ROOT_SENTINEL}
Do not use shell, file, web, or MCP tools. Use the multi-agent tools only.
Spawn exactly one worker with all of these explicit arguments:
- agent_type: worker
- model: gpt-5.6-luna
- reasoning_effort: low
- service_tier: priority
- fork_context: false
- message: RRCV2_WORKER_CAPABILITY. Return exactly CAPABILITY_WORKER_OK. Do not call any tool.
Wait until that exact worker completes. Then return exactly CAPABILITY_ROOT_OK followed by the exact worker result.
""".encode()


def _root_config(hook: Path) -> bytes:
    del hook
    disabled = (
        "apps",
        "plugins",
        "recommended_plugins",
        "remote_plugin",
        "plugin_sharing",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "in_app_browser",
        "computer_use",
        "image_generation",
        "view_image",
        "in_app_updates",
        "skill_mcp_dependency_install",
        "tool_call_mcp_elicitation",
    )
    feature_lines = "\n".join(f"{name} = false" for name in disabled)
    return f"""model = "gpt-5.5"
model_reasoning_effort = "medium"
service_tier = "priority"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
cli_auth_credentials_store = "keyring"

[analytics]
enabled = false

[feedback]
enabled = false

[features]
hooks = true
multi_agent = true
multi_agent_v2 = false
{feature_lines}

[agents]
max_concurrent_threads_per_session = 2
default_subagent_model = "gpt-5.6-luna"
default_subagent_reasoning_effort = "low"

[agents.worker]
description = "Run the RRCv2 native worker capability assignment without tools."
""".encode()


def _hooks(hook: Path) -> bytes:
    command = f"{sys.executable} {hook}"
    entry = {"type": "command", "command": command, "timeout": 30}
    value = {
        "description": "RRCv2 capability lifecycle hooks",
        "hooks": {
            "PreToolUse": [{"hooks": [entry]}],
            "PostToolUse": [{"hooks": [entry]}],
            "SubagentStart": [{"hooks": [entry], "matcher": "worker"}],
            "SubagentStop": [{"hooks": [entry], "matcher": "worker"}],
            "Stop": [{"hooks": [entry]}],
        },
    }
    return canonical_json(value)


def _root_argv_template() -> tuple[str, ...]:
    return (
        "<CODEX>",
        "--dangerously-bypass-hook-trust",
        "-a",
        "never",
        "-m",
        "gpt-5.5",
        "-c",
        'model_reasoning_effort="medium"',
        "-c",
        'service_tier="priority"',
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "-C",
        "<CWD>",
        "-",
    )


def _surface_hashes(repo: Path) -> dict[str, dict[str, str]]:
    hook = repo / "contextmesh/scripts/rrcv2_capability_hook.py"
    result: dict[str, dict[str, str]] = {}
    for surface_id in (
        "root_strong_medium_native",
        "small_code_low",
        "small_metadata_low",
        "small_spec_low",
        "small_tests_low",
        "strong_code_medium",
        "strong_spec_low",
        "worker_small_low_native",
    ):
        if surface_id in SURFACE_KIND:
            schema = canonical_json(_schema(SURFACE_KIND[surface_id]))
            prompt = _prompt(surface_id)
            argv = canonical_json(list(_direct_argv_template(surface_id)))
            config = canonical_json(
                {
                    "model": _role(surface_id)[0],
                    "reasoning": _role(surface_id)[1],
                    "service_tier": "priority",
                    "hooks": False,
                }
            )
            tool_schema = canonical_json({"tools": []})
            definition = canonical_json(
                {
                    "v": 1,
                    "surface_id": surface_id,
                    "stdin": "prompt",
                    "stdout": "codex_exec_jsonl",
                    "output_schema": _schema(SURFACE_KIND[surface_id]),
                }
            )
        elif surface_id == "root_strong_medium_native":
            schema = canonical_json({"kind": "native_root_assistant_text_v1"})
            prompt = _root_prompt()
            argv = canonical_json(list(_root_argv_template()))
            config = _root_config(hook)
            tool_schema = _read_regular(
                repo / "tests/fixtures/codex_0_147_multiagent_v1.json", 64 * 1024, mode=0o644
            )
            definition = canonical_json(
                {
                    "v": 1,
                    "surface_id": surface_id,
                    "root_prompt": _sha(prompt),
                    "fork_context": False,
                }
            )
        else:
            schema = canonical_json({"kind": "native_worker_assistant_text_v1"})
            prompt = b"RRCV2_WORKER_CAPABILITY. Return exactly CAPABILITY_WORKER_OK. Do not call any tool.\n"
            argv = canonical_json(
                {
                    "tool": "spawn_agent",
                    "agent_type": "worker",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "low",
                    "service_tier": "priority",
                    "fork_context": False,
                }
            )
            config = _root_config(hook)
            tool_schema = canonical_json({"tools": []})
            definition = canonical_json(
                {"v": 1, "surface_id": surface_id, "native_subagent": True, "fork_context": False}
            )
        result[surface_id] = {
            "argv_sha256": _sha(argv),
            "config_sha256": _sha(config),
            "tool_schema_sha256": _sha(tool_schema),
            "output_schema_sha256": _sha(schema),
            "prompt_wrapper_sha256": _sha(prompt),
            "assembled_byte_definition_sha256": _sha(definition),
        }
    return result


def _codex_binary() -> Path:
    value = (
        os.environ.get("RRD_CODEX_BIN")
        or subprocess.run(
            ("/usr/bin/which", "codex"), capture_output=True, text=True, check=False
        ).stdout.strip()
    )
    if not value:
        raise MatrixError("Codex binary is unavailable")
    path = Path(value).resolve(strict=True)
    meta = path.stat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o022:
        raise MatrixError("Codex binary is not a protected regular file")
    return path


def _environment(call_id: str, *, call_tmp: Path) -> dict[str, str]:
    home = os.environ.get("HOME", "")
    codex_home = os.environ.get("CODEX_HOME", "")
    if not Path(home).is_absolute() or not Path(codex_home).is_absolute():
        raise MatrixError("capability environment requires absolute HOME/CODEX_HOME")
    return {
        "HOME": home,
        "CODEX_HOME": codex_home,
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        "TMPDIR": str(call_tmp),
        "RRC_CAPABILITY_CALL_ID": call_id,
    }


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.02)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_bounded(
    argv: tuple[str, ...], *, cwd: Path, env: Mapping[str, str], stdin: bytes, timeout: int
) -> tuple[int, bytes, bytes]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    process.stdin.write(stdin)
    process.stdin.close()
    selector = selectors.DefaultSelector()
    for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MatrixError("Codex capability call exceeded wall deadline")
            for key, _ in selector.select(min(remaining, 0.25)):
                stream = key.fileobj
                name = key.data
                try:
                    chunk = os.read(key.fd, 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                outputs[name].extend(chunk)
                cap = STDOUT_CAP if name == "stdout" else STDERR_CAP
                if len(outputs[name]) > cap:
                    raise MatrixError(f"Codex capability {name} exceeded cap")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MatrixError("Codex capability call exceeded wall deadline")
        returncode = process.wait(timeout=remaining)
    except BaseException:
        _kill_group(process)
        process.wait(timeout=5)
        raise
    finally:
        selector.close()
    return returncode, bytes(outputs["stdout"]), bytes(outputs["stderr"])


def _parse_exec(stdout: bytes) -> tuple[str, str, dict[str, int]]:
    if any(len(line) > LINE_CAP for line in stdout.splitlines()):
        raise MatrixError("Codex JSONL line exceeds cap")
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line:
            continue
        try:
            row = json.loads(line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MatrixError("Codex stdout is not exact JSONL") from exc
        if not isinstance(row, dict):
            raise MatrixError("Codex JSONL row is not an object")
        rows.append(row)
    if len(rows) > EVENT_CAP:
        raise MatrixError("Codex JSONL event count exceeds cap")
    thread_ids = [row.get("thread_id") for row in rows if row.get("type") == "thread.started"]
    finals = [
        item.get("text")
        for row in rows
        if row.get("type") == "item.completed"
        and isinstance((item := row.get("item")), dict)
        and item.get("type") == "agent_message"
        and isinstance(item.get("text"), str)
    ]
    usages = [row.get("usage") for row in rows if row.get("type") == "turn.completed"]
    if (
        len(thread_ids) != 1
        or not isinstance(thread_ids[0], str)
        or len(finals) < 1
        or len(usages) != 1
    ):
        raise MatrixError("Codex transcript lacks one thread/final/usage authority")
    thread_id = thread_ids[0]
    final = finals[-1]
    if not isinstance(thread_id, str) or not isinstance(final, str):
        raise MatrixError("Codex thread/final types are invalid")
    if len(final.encode("utf-8")) > FINAL_CAP:
        raise MatrixError("Codex final assistant message exceeds cap")
    usage = usages[0]
    if not isinstance(usage, dict):
        raise MatrixError("Codex final usage is malformed")
    parsed: dict[str, int] = {}
    for field in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MatrixError(f"invalid Codex usage field: {field}")
        parsed[field] = value
    if (
        parsed["cached_input_tokens"] > parsed["input_tokens"]
        or parsed["reasoning_output_tokens"] > parsed["output_tokens"]
    ):
        raise MatrixError("Codex usage components do not reconcile")
    parsed["provider_total_tokens"] = parsed["input_tokens"] + parsed["output_tokens"]
    return thread_id, final, parsed


def _result_row(
    *,
    ordinal: int,
    call_id: str,
    surface_id: str,
    environment_sha256: str,
    prompt_sha256: str,
    output_schema_sha256: str,
    config_sha256: str,
    argv_sha256: str,
    stdout: bytes,
    stderr: bytes,
    transcript: bytes,
    effective_provider: str,
    effective_model: str,
    effective_reasoning: str,
    identity_attestation: str,
    usage: Mapping[str, int],
) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "call_id": call_id,
        "surface_id": surface_id,
        "environment_sha256": environment_sha256,
        "prompt_sha256": prompt_sha256,
        "output_schema_sha256": output_schema_sha256,
        "config_sha256": config_sha256,
        "argv_sha256": argv_sha256,
        "stdout_sha256": _sha(stdout),
        "stderr_sha256": _sha(stderr),
        "transcript_sha256": _sha(transcript),
        "exit_status": 0,
        "identity_attestation": identity_attestation,
        "effective_provider": effective_provider,
        "effective_model": effective_model,
        "effective_reasoning": effective_reasoning,
        "effective_service_tier": "unattested",
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_output_tokens": usage["reasoning_output_tokens"],
        "provider_total_tokens": usage["provider_total_tokens"],
    }


def _paths(repo: Path) -> dict[str, Path]:
    root = repo / ".generated/state/rrcv2-convergence/capability"
    return {
        "root": root,
        "calls": root / "calls",
        "manifest": root / "capability-manifest.v1.json",
        "summary": root / "capability-summary.v1.json",
        "ledger": root / "launch-ledger.jsonl",
        "inventory": repo
        / ".generated/state/rrcv2-convergence/verify/capability-evidence-inventory.v1.json",
        "plan_seal": repo / ".generated/state/rrcv2-convergence/reviews" / PLAN_SEAL_NAME,
    }


def _expected_call_files(ordinal: int) -> tuple[str, ...]:
    if ordinal <= 7:
        return (
            "argv.json",
            "config.json",
            "environment.json",
            "prompt.txt",
            "result.json",
            "schema.json",
            "stderr.txt",
            "stdout.jsonl",
        )
    if ordinal == 8:
        return (
            "argv.json",
            "config.toml",
            "environment.json",
            "hook-events.jsonl",
            "hooks.json",
            "prompt.txt",
            "result.json",
            "rollout.jsonl",
            "stderr.txt",
            "stdout.jsonl",
        )
    return ("config.toml", "environment.json", "prompt.txt", "result.json", "rollout.jsonl")


def _evidence_inventory(repo: Path) -> dict[str, object]:
    paths = _paths(repo)
    calls: list[dict[str, object]] = []
    for ordinal, call_id, surface_id in CALLS:
        call_dir = paths["calls"] / call_id
        actual = sorted(
            path.name for path in call_dir.iterdir() if path.is_file() and not path.is_symlink()
        )
        expected = list(_expected_call_files(ordinal))
        if actual != sorted(expected):
            raise MatrixError(f"capability call file set drift: {call_id}")
        for child in call_dir.iterdir():
            if child.is_symlink() or (child.is_dir() and any(child.iterdir())):
                raise MatrixError(f"capability call contains special/nonempty path: {call_id}")
        files: list[dict[str, object]] = []
        for name in expected:
            path = call_dir / name
            raw = _read_regular(path, 128 * 1024 * 1024, mode=0o600)
            files.append(
                {
                    "path": name,
                    "sha256": _sha(raw),
                    "bytes": len(raw),
                    "mode": 0o600,
                }
            )
        calls.append(
            {
                "ordinal": ordinal,
                "call_id": call_id,
                "surface_id": surface_id,
                "files": files,
            }
        )
    ledger_raw = _read_regular(paths["ledger"], 1024 * 1024, mode=0o600)
    return {
        "v": 1,
        "kind": "rrcv2_capability_evidence_inventory",
        "capability_manifest_sha256": _sha(
            _read_regular(paths["manifest"], 4 * 1024 * 1024, mode=0o600)
        ),
        "capability_summary_sha256": _sha(
            _read_regular(paths["summary"], 4 * 1024 * 1024, mode=0o600)
        ),
        "launch_ledger_sha256": _sha(ledger_raw),
        "calls": calls,
    }


def seal_evidence_inventory(repo: Path) -> dict[str, object]:
    """Seal the complete already-paid capability evidence without a provider replay."""

    repo = repo.resolve()
    value = _evidence_inventory(repo)
    path = _paths(repo)["inventory"]
    if path.exists() or path.is_symlink():
        raise MatrixError("capability evidence inventory already exists")
    _write_exclusive(path, canonical_json(value))
    return value


def _validate_evidence_inventory(repo: Path) -> dict[str, object]:
    expected = _evidence_inventory(repo)
    actual = _load_canonical(_paths(repo)["inventory"])
    if actual != expected:
        raise MatrixError("capability evidence inventory drift")
    return expected


def _append_ledger(path: Path, value: object) -> None:
    line = canonical_json(value)
    fd = os.open(
        path,
        os.O_APPEND
        | os.O_CREAT
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode):
            raise MatrixError("launch ledger is not regular")
        os.fchmod(fd, 0o600)
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _prepare_manifest(
    repo: Path, codex: Path
) -> tuple[dict[str, object], AuthorityRef, AuthorityRef]:
    paths = _paths(repo)
    plan_raw = _read_regular(paths["plan_seal"], 64 * 1024)
    plan_ref = AuthorityRef(paths["plan_seal"], _sha(plan_raw), len(plan_raw))
    codex_raw = _read_regular(codex, 512 * 1024 * 1024, mode=stat.S_IMODE(codex.stat().st_mode))
    value = capability_manifest(
        pre_capability_plan_review_seal_sha256=plan_ref.sha256,
        cli_binary_sha256=_sha(codex_raw),
        surface_hashes=_surface_hashes(repo),
    )
    raw = canonical_json(value)
    if not paths["manifest"].exists():
        _write_exclusive(paths["manifest"], raw)
    elif _read_regular(paths["manifest"], 4 * 1024 * 1024) != raw:
        raise MatrixError("existing capability manifest differs from reviewed construction")
    manifest_ref = AuthorityRef(paths["manifest"], _sha(raw), len(raw))
    return value, manifest_ref, plan_ref


def _direct_call(
    *,
    repo: Path,
    codex: Path,
    manifest: dict[str, object],
    manifest_ref: AuthorityRef,
    plan_ref: AuthorityRef,
    ordinal: int,
    call_id: str,
    surface_id: str,
) -> dict[str, object]:
    authorize_capability(
        call_id=call_id,
        surface_id=surface_id,
        manifest_ref=manifest_ref,
        plan_review_seal_ref=plan_ref,
        expected_manifest=manifest,
    )
    call_dir = _paths(repo)["calls"] / call_id
    try:
        call_dir.mkdir(parents=True, mode=0o700)
    except FileExistsError as exc:
        raise MatrixError(f"capability call path already exists: {call_id}") from exc
    call_dir.chmod(0o700)
    cwd = call_dir / "cwd"
    tmp = call_dir / "tmp"
    cwd.mkdir(mode=0o700)
    tmp.mkdir(mode=0o700)
    kind = SURFACE_KIND[surface_id]
    prompt = _prompt(surface_id)
    schema = canonical_json(_schema(kind))
    config = canonical_json(
        {
            "model": _role(surface_id)[0],
            "reasoning": _role(surface_id)[1],
            "service_tier": "priority",
            "hooks": False,
        }
    )
    schema_path = call_dir / "schema.json"
    _write_exclusive(schema_path, schema)
    _write_exclusive(call_dir / "prompt.txt", prompt)
    _write_exclusive(call_dir / "config.json", config)
    template = _direct_argv_template(surface_id)
    argv = tuple(
        str(codex)
        if item == "<CODEX>"
        else str(cwd)
        if item == "<CWD>"
        else str(schema_path)
        if item == "<SCHEMA>"
        else item
        for item in template
    )
    _write_exclusive(call_dir / "argv.json", canonical_json(list(argv)))
    env = _environment(call_id, call_tmp=tmp)
    env_raw = canonical_json(env)
    _write_exclusive(call_dir / "environment.json", env_raw)
    _append_ledger(
        _paths(repo)["ledger"],
        {
            "v": 1,
            "event": "call_started",
            "ordinal": ordinal,
            "call_id": call_id,
            "surface_id": surface_id,
        },
    )
    returncode, stdout, stderr = _run_bounded(
        argv, cwd=cwd, env=env, stdin=prompt, timeout=CALL_TIMEOUT
    )
    _write_exclusive(call_dir / "stdout.jsonl", stdout)
    _write_exclusive(call_dir / "stderr.txt", stderr)
    if returncode != 0:
        raise MatrixError(f"capability call failed: {call_id}: {stderr[:4096]!r}")
    _thread, final, usage = _parse_exec(stdout)
    try:
        parsed_final = json.loads(final)
    except json.JSONDecodeError as exc:
        raise MatrixError(f"capability final is not JSON: {call_id}") from exc
    if parsed_final != _answer(kind):
        raise MatrixError(f"capability final differs from required schema instance: {call_id}")
    row = _result_row(
        ordinal=ordinal,
        call_id=call_id,
        surface_id=surface_id,
        environment_sha256=_sha(env_raw),
        prompt_sha256=_sha(prompt),
        output_schema_sha256=_sha(schema),
        config_sha256=_sha(config),
        argv_sha256=_surface_hashes(repo)[surface_id]["argv_sha256"],
        stdout=stdout,
        stderr=stderr,
        transcript=stdout,
        effective_provider="unattested",
        effective_model="unattested",
        effective_reasoning="unattested",
        identity_attestation="usage_only",
        usage=usage,
    )
    _write_exclusive(call_dir / "result.json", canonical_json(row))
    _append_ledger(
        _paths(repo)["ledger"],
        {
            "v": 1,
            "event": "call_completed",
            "ordinal": ordinal,
            "call_id": call_id,
            "surface_id": surface_id,
            "result_sha256": _sha(canonical_json(row)),
        },
    )
    return row


def _snapshot_sessions(codex_home: Path) -> set[Path]:
    root = codex_home / "sessions"
    if not root.exists():
        return set()
    return {
        path for path in root.rglob("rollout-*.jsonl") if path.is_file() and not path.is_symlink()
    }


def _rollout(path: Path) -> tuple[dict[str, Any], str, dict[str, int]]:
    raw = _read_regular(path, 128 * 1024 * 1024, mode=stat.S_IMODE(path.stat().st_mode))
    meta: dict[str, Any] | None = None
    final = ""
    usage: dict[str, int] | None = None
    for line in raw.splitlines():
        try:
            row = json.loads(line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MatrixError("native rollout is malformed") from exc
        if row.get("type") == "session_meta" and isinstance(row.get("payload"), dict):
            meta = row["payload"]
        payload = row.get("payload")
        if row.get("type") == "turn_context" and isinstance(payload, dict) and meta is not None:
            meta["observed_model"] = payload.get("model")
            meta["observed_effort"] = payload.get("effort")
        if row.get("type") == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "agent_message" and isinstance(payload.get("message"), str):
                final = payload["message"]
            info = payload.get("info")
            if payload.get("type") == "token_count" and isinstance(info, dict):
                total = info.get("total_token_usage")
                if isinstance(total, dict):
                    fields = {}
                    for name in (
                        "input_tokens",
                        "cached_input_tokens",
                        "output_tokens",
                        "reasoning_output_tokens",
                    ):
                        value = total.get(name)
                        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                            raise MatrixError("native rollout usage is invalid")
                        fields[name] = value
                    fields["provider_total_tokens"] = (
                        fields["input_tokens"] + fields["output_tokens"]
                    )
                    usage = fields
    if meta is None or not final or usage is None:
        raise MatrixError("native rollout lacks meta/final/usage")
    return meta, final, usage


def _save_restore(path: Path) -> tuple[bool, bytes, int]:
    if not path.exists() and not path.is_symlink():
        return (False, b"", 0)
    meta = path.lstat()
    return (
        True,
        _read_regular(path, 4 * 1024 * 1024, mode=stat.S_IMODE(meta.st_mode)),
        stat.S_IMODE(meta.st_mode),
    )


def _atomic_replace(path: Path, raw: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _root_worker_calls(
    *,
    repo: Path,
    codex: Path,
    manifest: dict[str, object],
    manifest_ref: AuthorityRef,
    plan_ref: AuthorityRef,
) -> tuple[dict[str, object], dict[str, object]]:
    for ordinal, call_id, surface_id in CALLS[7:]:
        authorize_capability(
            call_id=call_id,
            surface_id=surface_id,
            manifest_ref=manifest_ref,
            plan_review_seal_ref=plan_ref,
            expected_manifest=manifest,
        )
        call_dir = _paths(repo)["calls"] / call_id
        try:
            call_dir.mkdir(parents=True, mode=0o700)
        except FileExistsError as exc:
            raise MatrixError(f"capability call path already exists: {call_id}") from exc
        call_dir.chmod(0o700)
    root_dir = _paths(repo)["calls"] / CALLS[7][1]
    worker_dir = _paths(repo)["calls"] / CALLS[8][1]
    cwd = root_dir / "cwd"
    tmp = root_dir / "tmp"
    cwd.mkdir(mode=0o700)
    tmp.mkdir(mode=0o700)
    hook = repo / "contextmesh/scripts/rrcv2_capability_hook.py"
    config = _root_config(hook)
    hooks = _hooks(hook)
    codex_home = Path(os.environ["CODEX_HOME"])
    saved = {name: _save_restore(codex_home / name) for name in ("config.toml", "hooks.json")}
    before_sessions = _snapshot_sessions(codex_home)
    prompt = _root_prompt()
    template = _root_argv_template()
    argv = tuple(
        str(codex) if item == "<CODEX>" else str(cwd) if item == "<CWD>" else item
        for item in template
    )
    env = _environment(CALLS[7][1], call_tmp=tmp)
    env_raw = canonical_json(env)
    _write_exclusive(root_dir / "prompt.txt", prompt)
    _write_exclusive(root_dir / "config.toml", config)
    _write_exclusive(root_dir / "hooks.json", hooks)
    _write_exclusive(root_dir / "argv.json", canonical_json(list(argv)))
    _write_exclusive(root_dir / "environment.json", env_raw)
    worker_prompt = (
        b"RRCV2_WORKER_CAPABILITY. Return exactly CAPABILITY_WORKER_OK. Do not call any tool.\n"
    )
    _write_exclusive(worker_dir / "prompt.txt", worker_prompt)
    _write_exclusive(worker_dir / "config.toml", config)
    _write_exclusive(worker_dir / "environment.json", env_raw)
    _append_ledger(
        _paths(repo)["ledger"],
        {
            "v": 1,
            "event": "call_started",
            "ordinal": 8,
            "call_id": CALLS[7][1],
            "surface_id": CALLS[7][2],
        },
    )
    _append_ledger(
        _paths(repo)["ledger"],
        {
            "v": 1,
            "event": "call_started",
            "ordinal": 9,
            "call_id": CALLS[8][1],
            "surface_id": CALLS[8][2],
        },
    )
    new_sessions: set[Path] = set()
    try:
        _atomic_replace(codex_home / "config.toml", config)
        _atomic_replace(codex_home / "hooks.json", hooks)
        returncode, stdout, stderr = _run_bounded(
            argv, cwd=cwd, env=env, stdin=prompt, timeout=ROOT_TIMEOUT
        )
        after_sessions = _snapshot_sessions(codex_home)
        new_sessions = after_sessions - before_sessions
    finally:
        for name, (existed, raw, mode) in saved.items():
            path = codex_home / name
            if existed:
                _atomic_replace(path, raw, mode)
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    _write_exclusive(root_dir / "stdout.jsonl", stdout)
    _write_exclusive(root_dir / "stderr.txt", stderr)
    if returncode != 0:
        raise MatrixError(f"native root capability failed: {stderr[:4096]!r}")
    root_id, root_final, root_usage = _parse_exec(stdout)
    receiver_ids: set[str] = set()
    for line in stdout.splitlines():
        row = json.loads(line)
        item = row.get("item") if isinstance(row, dict) else None
        if (
            isinstance(item, dict)
            and item.get("type") == "collab_tool_call"
            and item.get("tool") == "spawn_agent"
        ):
            values = item.get("receiver_thread_ids")
            if isinstance(values, list):
                receiver_ids.update(value for value in values if isinstance(value, str))
    if len(receiver_ids) != 1:
        raise MatrixError("native root did not expose exactly one worker agent ID")
    worker_id = next(iter(receiver_ids))
    by_id = {path.stem.rsplit("-", 5)[-1]: path for path in new_sessions}
    root_rollout = next((path for path in new_sessions if root_id in path.name), None)
    worker_rollout = next((path for path in new_sessions if worker_id in path.name), None)
    del by_id
    if root_rollout is None or worker_rollout is None or root_rollout == worker_rollout:
        raise MatrixError("native root/worker rollout correlation is incomplete")
    root_raw = _read_regular(
        root_rollout, 128 * 1024 * 1024, mode=stat.S_IMODE(root_rollout.stat().st_mode)
    )
    worker_raw = _read_regular(
        worker_rollout, 128 * 1024 * 1024, mode=stat.S_IMODE(worker_rollout.stat().st_mode)
    )
    worker_meta, worker_final, worker_usage = _rollout(worker_rollout)
    root_meta, _root_rollout_final, _root_rollout_usage = _rollout(root_rollout)
    if (
        ROOT_SENTINEL.encode() in worker_raw
        or "CAPABILITY_PRETOOL_REWRITE" not in worker_raw.decode("utf-8", errors="replace")
    ):
        raise MatrixError("fork_context=false/source-noninheritance attestation failed")
    if (
        worker_final.strip() != "CAPABILITY_WORKER_OK"
        or "CAPABILITY_ROOT_OK" not in root_final
        or "CAPABILITY_WORKER_OK" not in root_final
    ):
        raise MatrixError("native root/worker final messages differ from the capability contract")
    for path, raw, target in (
        (root_rollout, root_raw, root_dir / "rollout.jsonl"),
        (worker_rollout, worker_raw, worker_dir / "rollout.jsonl"),
    ):
        _write_exclusive(target, raw)
        path.unlink()
    hook_raw = _read_regular(root_dir / "hook-events.jsonl", 4 * 1024 * 1024)
    hook_events = [json.loads(line) for line in hook_raw.splitlines()]
    required_hooks = {"PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop", "Stop"}
    if not required_hooks.issubset(
        {row.get("hook_event_name") for row in hook_events if isinstance(row, dict)}
    ):
        raise MatrixError("native lifecycle hook evidence is incomplete")
    root_schema = canonical_json({"kind": "native_root_assistant_text_v1"})
    worker_schema = canonical_json({"kind": "native_worker_assistant_text_v1"})
    surfaces = _surface_hashes(repo)
    root_row = _result_row(
        ordinal=8,
        call_id=CALLS[7][1],
        surface_id=CALLS[7][2],
        environment_sha256=_sha(env_raw),
        prompt_sha256=_sha(prompt),
        output_schema_sha256=_sha(root_schema),
        config_sha256=_sha(config),
        argv_sha256=surfaces[CALLS[7][2]]["argv_sha256"],
        stdout=stdout,
        stderr=stderr,
        transcript=root_raw,
        effective_provider=str(root_meta.get("model_provider", "unattested")),
        effective_model=str(root_meta.get("observed_model", "unattested")),
        effective_reasoning=str(root_meta.get("observed_effort", "unattested")),
        identity_attestation="native_partial",
        usage=root_usage,
    )
    worker_row = _result_row(
        ordinal=9,
        call_id=CALLS[8][1],
        surface_id=CALLS[8][2],
        environment_sha256=_sha(env_raw),
        prompt_sha256=_sha(worker_prompt),
        output_schema_sha256=_sha(worker_schema),
        config_sha256=_sha(config),
        argv_sha256=surfaces[CALLS[8][2]]["argv_sha256"],
        stdout=worker_final.encode(),
        stderr=b"",
        transcript=worker_raw,
        effective_provider=str(worker_meta.get("model_provider", "unattested")),
        effective_model=str(worker_meta.get("observed_model", "unattested")),
        effective_reasoning=str(worker_meta.get("observed_effort", "unattested")),
        identity_attestation="native_partial",
        usage=worker_usage,
    )
    for call_dir, row in ((root_dir, root_row), (worker_dir, worker_row)):
        _write_exclusive(call_dir / "result.json", canonical_json(row))
        _append_ledger(
            _paths(repo)["ledger"],
            {
                "v": 1,
                "event": "call_completed",
                "ordinal": row["ordinal"],
                "call_id": row["call_id"],
                "surface_id": row["surface_id"],
                "result_sha256": _sha(canonical_json(row)),
            },
        )
    return root_row, worker_row


def _recover_root_worker_calls(repo: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Finish sealing an already completed native root/worker call without provider replay."""

    root_dir = _paths(repo)["calls"] / CALLS[7][1]
    worker_dir = _paths(repo)["calls"] / CALLS[8][1]
    for directory in (root_dir, worker_dir):
        if not directory.is_dir() or directory.is_symlink():
            raise MatrixError("native recovery directory is absent or special")
        if (directory / "result.json").exists():
            raise MatrixError("native recovery refuses an already sealed result")
    stdout = _read_regular(root_dir / "stdout.jsonl", STDOUT_CAP)
    stderr = _read_regular(root_dir / "stderr.txt", STDERR_CAP)
    prompt = _read_regular(root_dir / "prompt.txt", FINAL_CAP)
    config = _read_regular(root_dir / "config.toml", 1024 * 1024)
    env_raw = _read_regular(root_dir / "environment.json", 1024 * 1024)
    worker_prompt = _read_regular(worker_dir / "prompt.txt", FINAL_CAP)
    root_id, root_final, root_usage = _parse_exec(stdout)
    receiver_ids: set[str] = set()
    for line in stdout.splitlines():
        row = json.loads(line)
        item = row.get("item") if isinstance(row, dict) else None
        if (
            isinstance(item, dict)
            and item.get("type") == "collab_tool_call"
            and item.get("tool") == "spawn_agent"
        ):
            values = item.get("receiver_thread_ids")
            if isinstance(values, list):
                receiver_ids.update(value for value in values if isinstance(value, str))
    if len(receiver_ids) != 1:
        raise MatrixError("native recovery cannot identify one worker")
    worker_id = next(iter(receiver_ids))
    sessions = Path(os.environ["CODEX_HOME"]) / "sessions"
    root_candidates = list(sessions.rglob(f"*{root_id}*.jsonl"))
    worker_candidates = list(sessions.rglob(f"*{worker_id}*.jsonl"))
    if len(root_candidates) != 1 or len(worker_candidates) != 1:
        raise MatrixError("native recovery rollout cardinality mismatch")
    root_rollout = root_candidates[0]
    worker_rollout = worker_candidates[0]
    root_raw = _read_regular(
        root_rollout, 128 * 1024 * 1024, mode=stat.S_IMODE(root_rollout.stat().st_mode)
    )
    worker_raw = _read_regular(
        worker_rollout, 128 * 1024 * 1024, mode=stat.S_IMODE(worker_rollout.stat().st_mode)
    )
    root_meta, _root_rollout_final, _root_rollout_usage = _rollout(root_rollout)
    worker_meta, worker_final, worker_usage = _rollout(worker_rollout)
    if (
        ROOT_SENTINEL.encode() in worker_raw
        or "CAPABILITY_PRETOOL_REWRITE" not in worker_raw.decode("utf-8", errors="replace")
    ):
        raise MatrixError("native recovery source-noninheritance attestation failed")
    if (
        worker_final.strip() != "CAPABILITY_WORKER_OK"
        or "CAPABILITY_ROOT_OK" not in root_final
        or "CAPABILITY_WORKER_OK" not in root_final
    ):
        raise MatrixError("native recovery final-message attestation failed")
    hook_raw = _read_regular(root_dir / "hook-events.jsonl", 4 * 1024 * 1024)
    hook_events = [json.loads(line) for line in hook_raw.splitlines()]
    required_hooks = {"PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop", "Stop"}
    if not required_hooks.issubset(
        {row.get("hook_event_name") for row in hook_events if isinstance(row, dict)}
    ):
        raise MatrixError("native recovery lifecycle evidence is incomplete")
    for source, raw, target in (
        (root_rollout, root_raw, root_dir / "rollout.jsonl"),
        (worker_rollout, worker_raw, worker_dir / "rollout.jsonl"),
    ):
        _write_exclusive(target, raw)
        source.unlink()
    surfaces = _surface_hashes(repo)
    root_row = _result_row(
        ordinal=8,
        call_id=CALLS[7][1],
        surface_id=CALLS[7][2],
        environment_sha256=_sha(env_raw),
        prompt_sha256=_sha(prompt),
        output_schema_sha256=_sha(canonical_json({"kind": "native_root_assistant_text_v1"})),
        config_sha256=_sha(config),
        argv_sha256=surfaces[CALLS[7][2]]["argv_sha256"],
        stdout=stdout,
        stderr=stderr,
        transcript=root_raw,
        effective_provider=str(root_meta.get("model_provider", "unattested")),
        effective_model=str(root_meta.get("observed_model", "unattested")),
        effective_reasoning=str(root_meta.get("observed_effort", "unattested")),
        identity_attestation="native_partial",
        usage=root_usage,
    )
    worker_row = _result_row(
        ordinal=9,
        call_id=CALLS[8][1],
        surface_id=CALLS[8][2],
        environment_sha256=_sha(env_raw),
        prompt_sha256=_sha(worker_prompt),
        output_schema_sha256=_sha(canonical_json({"kind": "native_worker_assistant_text_v1"})),
        config_sha256=_sha(config),
        argv_sha256=surfaces[CALLS[8][2]]["argv_sha256"],
        stdout=worker_final.encode(),
        stderr=b"",
        transcript=worker_raw,
        effective_provider=str(worker_meta.get("model_provider", "unattested")),
        effective_model=str(worker_meta.get("observed_model", "unattested")),
        effective_reasoning=str(worker_meta.get("observed_effort", "unattested")),
        identity_attestation="native_partial",
        usage=worker_usage,
    )
    for call_dir, row in ((root_dir, root_row), (worker_dir, worker_row)):
        raw = canonical_json(row)
        _write_exclusive(call_dir / "result.json", raw)
        _append_ledger(
            _paths(repo)["ledger"],
            {
                "v": 1,
                "event": "call_completed",
                "ordinal": row["ordinal"],
                "call_id": row["call_id"],
                "surface_id": row["surface_id"],
                "result_sha256": _sha(raw),
                "recovered_without_provider_replay": True,
            },
        )
    return root_row, worker_row


def _summary(
    repo: Path, manifest_ref: AuthorityRef, rows: list[dict[str, object]]
) -> dict[str, Any]:
    rows.sort(key=lambda row: _strict_int(row["ordinal"], "ordinal"))
    if len(rows) != 9:
        raise MatrixError("capability result cardinality differs from nine")
    maxima: dict[str, int] = {}
    for row in rows:
        surface_id = str(row["surface_id"])
        maxima[surface_id] = max(
            maxima.get(surface_id, 0), _strict_int(row["input_tokens"], "input_tokens")
        )
    if set(maxima) != {row[2] for row in CALLS} or any(value > 65_536 for value in maxima.values()):
        raise MatrixError("capability fixed-framing bound failed")
    return {
        "v": 1,
        "kind": "rrcv2_capability_summary",
        "capability_manifest_sha256": manifest_ref.sha256,
        "results": rows,
        "surface_input_tokens_max": dict(sorted(maxima.items())),
        "apfs_evidence_sha256": _evidence_hash(repo, "apfs-evidence.v1.json"),
        "docker_evidence_sha256": _evidence_hash(repo, "docker-evidence.v1.json"),
        "sandbox_evidence_sha256": _evidence_hash(repo, "sandbox-evidence.v1.json"),
        "exact_call_count": 9,
    }


def _evidence_hash(repo: Path, name: str) -> str:
    return _sha(
        _read_regular(
            repo / ".generated/state/rrcv2-convergence/capability" / name, 4 * 1024 * 1024
        )
    )


def produce(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    if os.environ.get("RRC_CAPABILITY_MODE") != "produce":
        raise MatrixError("capability producer requires RRC_CAPABILITY_MODE=produce")
    validate_preflight(repo=repo, environ=os.environ)
    paths = _paths(repo)
    if paths["summary"].exists() or paths["calls"].exists() or paths["ledger"].exists():
        raise MatrixError("capability producer is one-shot and its outputs already exist")
    codex = _codex_binary()
    version = subprocess.run(
        (str(codex), "--version"),
        capture_output=True,
        text=True,
        check=False,
        env=_environment("capability-version", call_tmp=Path(os.environ.get("TMPDIR", "/tmp"))),
    )
    if version.returncode != 0 or version.stdout.strip() != CLI_VERSION:
        raise MatrixError("installed Codex version differs from the capability contract")
    manifest, manifest_ref, plan_ref = _prepare_manifest(repo, codex)
    paths["calls"].mkdir(parents=True, mode=0o700)
    paths["calls"].chmod(0o700)
    rows: list[dict[str, object]] = []
    try:
        for ordinal, call_id, surface_id in CALLS[:5]:
            rows.append(
                _direct_call(
                    repo=repo,
                    codex=codex,
                    manifest=manifest,
                    manifest_ref=manifest_ref,
                    plan_ref=plan_ref,
                    ordinal=ordinal,
                    call_id=call_id,
                    surface_id=surface_id,
                )
            )
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="rrcv2-capability") as pool:
            futures = [
                pool.submit(
                    _direct_call,
                    repo=repo,
                    codex=codex,
                    manifest=manifest,
                    manifest_ref=manifest_ref,
                    plan_ref=plan_ref,
                    ordinal=ordinal,
                    call_id=call_id,
                    surface_id=surface_id,
                )
                for ordinal, call_id, surface_id in CALLS[5:7]
            ]
            rows.extend(future.result() for future in futures)
        rows.extend(
            _root_worker_calls(
                repo=repo,
                codex=codex,
                manifest=manifest,
                manifest_ref=manifest_ref,
                plan_ref=plan_ref,
            )
        )
        summary = _summary(repo, manifest_ref, rows)
        _write_exclusive(paths["summary"], canonical_json(summary))
        return summary
    except BaseException:
        raise


def validate(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    if os.environ.get("RRC_CAPABILITY_MODE") != "validate-sealed":
        raise MatrixError("sealed validation requires RRC_CAPABILITY_MODE=validate-sealed")
    validate_preflight(repo=repo, environ=os.environ)
    paths = _paths(repo)
    summary = _load_canonical(paths["summary"])
    manifest = _load_canonical(paths["manifest"])
    if (
        summary.get("v") != 1
        or summary.get("kind") != "rrcv2_capability_summary"
        or summary.get("exact_call_count") != 9
    ):
        raise MatrixError("invalid capability summary identity")
    if summary.get("capability_manifest_sha256") != _sha(
        _read_regular(paths["manifest"], 4 * 1024 * 1024)
    ):
        raise MatrixError("capability summary manifest binding failed")
    results = summary.get("results")
    if not isinstance(results, list) or len(results) != 9:
        raise MatrixError("capability summary result cardinality differs from nine")
    for expected, row in zip(CALLS, results, strict=True):
        if (
            not isinstance(row, dict)
            or (row.get("ordinal"), row.get("call_id"), row.get("surface_id")) != expected
        ):
            raise MatrixError("capability result ordering/identity mismatch")
        result_path = paths["calls"] / expected[1] / "result.json"
        if _load_canonical(result_path) != row:
            raise MatrixError("capability call result drift")
        if row.get("exit_status") != 0 or int(row.get("input_tokens", 65_537)) > 65_536:
            raise MatrixError("capability call result is not successful/bounded")
        call_dir = paths["calls"] / expected[1]
        if expected[0] <= 7:
            stdout = _read_regular(call_dir / "stdout.jsonl", STDOUT_CAP, mode=0o600)
            stderr = _read_regular(call_dir / "stderr.txt", STDERR_CAP, mode=0o600)
            _thread, _final, usage = _parse_exec(stdout)
            hashes = {
                "environment_sha256": _sha(
                    _read_regular(call_dir / "environment.json", 1024 * 1024, mode=0o600)
                ),
                "prompt_sha256": _sha(
                    _read_regular(call_dir / "prompt.txt", FINAL_CAP, mode=0o600)
                ),
                "output_schema_sha256": _sha(
                    _read_regular(call_dir / "schema.json", 1024 * 1024, mode=0o600)
                ),
                "config_sha256": _sha(
                    _read_regular(call_dir / "config.json", 1024 * 1024, mode=0o600)
                ),
                "stdout_sha256": _sha(stdout),
                "stderr_sha256": _sha(stderr),
                "transcript_sha256": _sha(stdout),
            }
            if (
                any(row.get(field) != digest for field, digest in hashes.items())
                or any(row.get(field) != usage[field] for field in usage)
                or row.get("argv_sha256") != _surface_hashes(repo)[expected[2]]["argv_sha256"]
            ):
                raise MatrixError("direct capability raw evidence does not reconcile")
    if summary.get("surface_input_tokens_max") != {
        surface: max(int(row["input_tokens"]) for row in results if row["surface_id"] == surface)
        for surface in sorted({call[2] for call in CALLS})
    }:
        raise MatrixError("capability surface maxima do not reconcile")
    root_dir = paths["calls"] / CALLS[7][1]
    worker_dir = paths["calls"] / CALLS[8][1]
    root_stdout = _read_regular(root_dir / "stdout.jsonl", STDOUT_CAP, mode=0o600)
    root_stderr = _read_regular(root_dir / "stderr.txt", STDERR_CAP, mode=0o600)
    _root_thread, root_final, root_usage = _parse_exec(root_stdout)
    root_meta, rollout_root_final, rollout_root_usage = _rollout(root_dir / "rollout.jsonl")
    worker_meta, worker_final, worker_usage = _rollout(worker_dir / "rollout.jsonl")
    if root_usage != rollout_root_usage or root_final != rollout_root_final:
        raise MatrixError("native root stdout and rollout disagree")
    root_row = results[7]
    worker_row = results[8]
    root_hashes = {
        "environment_sha256": _sha(
            _read_regular(root_dir / "environment.json", 1024 * 1024, mode=0o600)
        ),
        "prompt_sha256": _sha(_read_regular(root_dir / "prompt.txt", FINAL_CAP, mode=0o600)),
        "config_sha256": _sha(_read_regular(root_dir / "config.toml", 1024 * 1024, mode=0o600)),
        "stdout_sha256": _sha(root_stdout),
        "stderr_sha256": _sha(root_stderr),
        "transcript_sha256": _sha(
            _read_regular(root_dir / "rollout.jsonl", 128 * 1024 * 1024, mode=0o600)
        ),
    }
    worker_hashes = {
        "environment_sha256": _sha(
            _read_regular(worker_dir / "environment.json", 1024 * 1024, mode=0o600)
        ),
        "prompt_sha256": _sha(_read_regular(worker_dir / "prompt.txt", FINAL_CAP, mode=0o600)),
        "config_sha256": _sha(_read_regular(worker_dir / "config.toml", 1024 * 1024, mode=0o600)),
        "stdout_sha256": _sha(worker_final.encode()),
        "stderr_sha256": _sha(b""),
        "transcript_sha256": _sha(
            _read_regular(worker_dir / "rollout.jsonl", 128 * 1024 * 1024, mode=0o600)
        ),
    }
    if (
        any(root_row.get(field) != digest for field, digest in root_hashes.items())
        or any(worker_row.get(field) != digest for field, digest in worker_hashes.items())
        or any(root_row.get(field) != root_usage[field] for field in root_usage)
        or any(worker_row.get(field) != worker_usage[field] for field in worker_usage)
        or root_row.get("effective_model") != root_meta.get("observed_model")
        or root_row.get("effective_reasoning") != root_meta.get("observed_effort")
        or worker_row.get("effective_model") != worker_meta.get("observed_model")
        or worker_row.get("effective_reasoning") != worker_meta.get("observed_effort")
    ):
        raise MatrixError("native root/worker raw evidence does not reconcile")
    hook_rows = [
        json.loads(line)
        for line in _read_regular(
            root_dir / "hook-events.jsonl", 4 * 1024 * 1024, mode=0o600
        ).splitlines()
    ]
    if {"PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop", "Stop"} - {
        row.get("hook_event_name") for row in hook_rows if isinstance(row, dict)
    }:
        raise MatrixError("native hook evidence is incomplete")
    worker_rollout = _read_regular(worker_dir / "rollout.jsonl", 128 * 1024 * 1024, mode=0o600)
    if (
        ROOT_SENTINEL.encode() in worker_rollout
        or b"CAPABILITY_PRETOOL_REWRITE" not in worker_rollout
    ):
        raise MatrixError("native worker source noninheritance evidence failed")

    lines = _read_regular(paths["ledger"], 1024 * 1024, mode=0o600).splitlines()
    ledger_rows = [json.loads(line) for line in lines]
    if len(ledger_rows) != 18 or any(not isinstance(row, dict) for row in ledger_rows):
        raise MatrixError("capability launch ledger must contain exactly eighteen events")
    starts = [row for row in ledger_rows if row.get("event") == "call_started"]
    completions = [row for row in ledger_rows if row.get("event") == "call_completed"]
    start_identities = sorted(
        ((row.get("ordinal"), row.get("call_id"), row.get("surface_id")) for row in starts),
        key=lambda row: int(row[0]),
    )
    if start_identities != list(CALLS):
        raise MatrixError("capability launch ledger differs from exactly nine calls")
    completion_identities = sorted(
        (
            (
                row.get("ordinal"),
                row.get("call_id"),
                row.get("surface_id"),
                row.get("result_sha256"),
            )
            for row in completions
        ),
        key=lambda row: int(row[0]),
    )
    expected_completions = [
        (*identity, _sha(canonical_json(result)))
        for identity, result in zip(CALLS, results, strict=True)
    ]
    if completion_identities != expected_completions:
        raise MatrixError("capability completion ledger does not bind all nine results")
    _validate_evidence_inventory(repo)
    del manifest
    return summary


def ensure(repo: Path) -> dict[str, Any]:
    mode = os.environ.get("RRC_CAPABILITY_MODE")
    if mode == "produce":
        if _paths(repo.resolve())["summary"].exists():
            return validate_under_produce(repo)
        paths = _paths(repo.resolve())
        if paths["calls"].exists() and paths["ledger"].exists():
            codex = _codex_binary()
            manifest, manifest_ref, _plan_ref = _prepare_manifest(repo.resolve(), codex)
            del manifest
            rows = [
                _load_canonical(paths["calls"] / call_id / "result.json")
                for _ordinal, call_id, _surface_id in CALLS[:7]
            ]
            rows.extend(_recover_root_worker_calls(repo.resolve()))
            summary = _summary(repo.resolve(), manifest_ref, rows)
            _write_exclusive(paths["summary"], canonical_json(summary))
            return summary
        return produce(repo)
    if mode == "validate-sealed":
        return validate(repo)
    raise MatrixError("RRC_CAPABILITY_MODE must be produce or validate-sealed")


def validate_under_produce(repo: Path) -> dict[str, Any]:
    previous = os.environ.get("RRC_CAPABILITY_MODE")
    os.environ["RRC_CAPABILITY_MODE"] = "validate-sealed"
    try:
        return validate(repo)
    finally:
        if previous is None:
            os.environ.pop("RRC_CAPABILITY_MODE", None)
        else:
            os.environ["RRC_CAPABILITY_MODE"] = previous
