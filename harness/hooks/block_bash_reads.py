"""Block Bash read workarounds for the full arm's workers."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

READ_COMMANDS = frozenset({"cat", "get-content", "grep", "rg", "sed", "gc", "type"})
SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&"})
SHELL_WRAPPERS = frozenset({"bash", "cmd", "powershell", "pwsh", "sh"})
PYTHON_COMMANDS = frozenset({"py", "python", "python3"})
PYTHON_READ = re.compile(
    r"(?:\bopen\s*\(|\bread_(?:text|bytes)\s*\(|\.\s*read\s*\()", re.IGNORECASE
)


def _executable(token: str) -> str:
    name = token.strip().strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _dollar_substitution(command: str, start: int) -> tuple[str, int] | None:
    depth = 1
    index = start + 2
    quote: str | None = None
    while index < len(command):
        char = command[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char in "'\"":
            quote = char
        elif command.startswith("$(", index):
            depth += 1
            index += 2
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return command[start + 2 : index], index + 1
        index += 1
    return None


def _backtick_substitution(command: str, start: int) -> tuple[str, int] | None:
    index = start + 1
    while index < len(command):
        if command[index] == "\\":
            index += 2
            continue
        if command[index] == "`":
            return command[start + 1 : index], index + 1
        index += 1
    return None


def _nested_commands(command: str) -> list[str]:
    commands: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if quote == '"':
            if char == '"':
                quote = None
                index += 1
                continue
        elif char == "'":
            quote = char
            index += 1
            continue
        elif char == '"':
            quote = char
            index += 1
            continue

        nested = None
        if command.startswith("$(", index):
            nested = _dollar_substitution(command, index)
        elif char == "`":
            nested = _backtick_substitution(command, index)
        if nested is not None:
            nested_command, index = nested
            commands.append(nested_command)
            continue
        index += 1
    return commands


def _matching_paren(tokens: list[str], start: int) -> int | None:
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index] == "(":
            depth += 1
        elif tokens[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in SHELL_SEPARATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _python_reads(segment: list[str], index: int) -> bool:
    for option_index in range(index + 1, len(segment)):
        if segment[option_index].lower() == "-c":
            return bool(PYTHON_READ.search(" ".join(segment[option_index + 1 :])))
    return False


def _wrapped_reads(segment: list[str], index: int) -> bool:
    for option_index in range(index + 1, len(segment)):
        if segment[option_index].lower() in {"-c", "-command", "/c"}:
            return _read_like(" ".join(segment[option_index + 1 :]))
    return False


def _read_like(command: str) -> bool:
    try:
        tokens = _tokens(command)
    except ValueError:
        return False

    if any(_read_like(nested) for nested in _nested_commands(command)):
        return True
    for index, token in enumerate(tokens):
        if token != "(":
            continue
        end = _matching_paren(tokens, index)
        if end is not None and _read_like(" ".join(tokens[index + 1 : end])):
            return True

    segments = _segments(tokens)
    for segment in segments:
        index = 0
        while index < len(segment):
            name = _executable(segment[index])
            if (
                not name
                or name in {"sudo", "env", "command"}
                or re.fullmatch(r"\w+=.*", segment[index])
            ):
                index += 1
                continue
            break
        if index == len(segment):
            continue
        name = _executable(segment[index])
        if name in READ_COMMANDS:
            return True
        if name == "git" and index + 1 < len(segment) and segment[index + 1].lower() == "grep":
            return True
        if name in PYTHON_COMMANDS and _python_reads(segment, index):
            return True
        if name in SHELL_WRAPPERS and _wrapped_reads(segment, index):
            return True
    return False


def _decision(payload: object) -> dict[str, object] | None:
    if os.environ.get("REASONRENDER_ARM") != "full" or not isinstance(payload, dict):
        return None
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command or not _read_like(command):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Full-arm Bash read guard blocked a read-like command because Read is unavailable: "
            + command,
        }
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return 0
    decision = _decision(payload)
    if decision is not None:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the hook CLI.
    raise SystemExit(main())
