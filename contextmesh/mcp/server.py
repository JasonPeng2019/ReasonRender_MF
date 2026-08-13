#!/usr/bin/env python3
"""ContextMesh's persistent stdio MCP server and digest gate.

The server intentionally has no package dependency: Claude launches it once per
arm, and all sibling workers share the one process and therefore this gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib.request import Request, urlopen

DIGEST_SYSTEM = "\n".join(
    (
        "You are a code-digest generator. Produce a compact structural digest of the file so another engineer can work from it without rereading the raw source.",
        "Requirements:",
        "- One line of purpose, one line of imports.",
        "- Every exported/public symbol on ONE terse line: `L<start>-L<end>: signature — behavior` (abbreviate aggressively; no full sentences).",
        "- Note key data shapes, side effects, error paths, and anything surprising — telegraph style.",
        "- HARD LIMIT: the digest must be under 30% of the original character count. If needed, group trivial or similar symbols onto shared lines.",
        "- Plain text, no code fences, no commentary about this task.",
    )
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class DigestRecord:
    path: str
    lines: int
    raw_chars: int
    digest: str | None = None
    do_not_digest: bool = False
    v: int = 1

    def to_value(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "path": self.path,
            "lines": self.lines,
            "rawChars": self.raw_chars,
            "digest": self.digest,
            "doNotDigest": self.do_not_digest,
        }

    @classmethod
    def from_value(cls, value: Any) -> DigestRecord | None:
        if not isinstance(value, Mapping):
            return None
        path = value.get("path")
        lines = value.get("lines")
        raw_chars = value.get("rawChars")
        digest = value.get("digest")
        if not isinstance(path, str) or not isinstance(lines, int) or not isinstance(raw_chars, int):
            return None
        if digest is not None and not isinstance(digest, str):
            return None
        return cls(
            path=path,
            lines=lines,
            raw_chars=raw_chars,
            digest=digest,
            do_not_digest=value.get("doNotDigest") is True,
            v=value.get("v") if isinstance(value.get("v"), int) else 1,
        )


@dataclass
class GateEntry:
    future: asyncio.Future[DigestRecord | None]
    started_ms: int
    task: asyncio.Task[None] | None = None


Lookup = Callable[[str], Awaitable[DigestRecord | None]]
Store = Callable[[str, DigestRecord], Awaitable[bool]]
Summarize = Callable[[str, str, int], Awaitable[str | None]]
TaskResultLookup = Callable[[str], Awaitable[str | None]]


class ContextMesh:
    """The shared process state used by MCP `read` and `expand_result`."""

    def __init__(
        self,
        *,
        lookup: Lookup | None = None,
        store: Store | None = None,
        summarize: Summarize | None = None,
        task_result_lookup: TaskResultLookup | None = None,
        min_lines: int | None = None,
        max_digest_ratio: float | None = None,
        gate_timeout_ms: int | None = None,
        log_path: str | Path | None = None,
    ) -> None:
        self.everos_url = os.environ.get("CONTEXTMESH_EVEROS_URL", "http://127.0.0.1:8000")
        self.app_id = os.environ.get("CONTEXTMESH_APP_ID", "contextmesh")
        self.project_id = "digests"
        self.ev_timeout_ms = _env_int("CONTEXTMESH_EV_TIMEOUT_MS", 4000)
        self.sum_timeout_ms = _env_int("CONTEXTMESH_SUM_TIMEOUT_MS", 65000)
        # Explicit test/development adapter. Production leaves this empty and
        # uses the configured local CLI below.
        self.summarizer_url = os.environ.get("CONTEXTMESH_SUMMARIZER_URL", "").strip()
        self.summarizer_key = os.environ.get("CONTEXTMESH_SUMMARIZER_API_KEY", "")
        self.summarizer_command = os.environ.get(
            "CONTEXTMESH_SUMMARIZER_COMMAND", "claude"
        ).strip()
        self.summarizer_model = os.environ.get(
            "CONTEXTMESH_SUMMARIZER_MODEL", "sonnet"
        )
        self.summarizer_budget_usd = os.environ.get(
            "CONTEXTMESH_SUMMARIZER_MAX_BUDGET_USD", "0.50"
        )
        self.summarizer_concurrency = max(
            1, _env_int("CONTEXTMESH_SUMMARIZER_CONCURRENCY", 1)
        )
        self.min_lines = min_lines if min_lines is not None else _env_int("CONTEXTMESH_MIN_LINES", 60)
        self.max_digest_ratio = (
            max_digest_ratio
            if max_digest_ratio is not None
            else _env_float("CONTEXTMESH_MAX_DIGEST_RATIO", 0.35)
        )
        self.gate_timeout_ms = (
            gate_timeout_ms
            if gate_timeout_ms is not None
            else _env_int("CONTEXTMESH_DIGEST_GATE_TIMEOUT_MS", 65000)
        )
        self.log_path = Path(log_path or os.environ.get("CONTEXTMESH_LOG", ".contextmesh-metrics.jsonl"))
        self.l1: dict[str, DigestRecord] = {}
        self.gate: dict[str, GateEntry] = {}
        self.counters: dict[str, int] = {
            "read_raw": 0,
            "digest_hit": 0,
            "digest_stored": 0,
            "digest_rejected": 0,
            "digest_gate_wait": 0,
            "digest_gate_hit": 0,
            "digest_gate_timeout": 0,
            "errors": 0,
        }
        self._lookup = lookup or self._everos_lookup
        self._store = store or self._everos_store
        self._summarize = summarize or self._chat_complete
        self._trim_overlong_digests = summarize is None
        self._summarizer_semaphore = asyncio.Semaphore(self.summarizer_concurrency)
        self._task_result_lookup = task_result_lookup or self._everos_task_result_lookup

    def _event(self, event: str, **data: Any) -> None:
        payload = {"ts": int(time.time() * 1000), "pid": os.getpid(), "event": event, **data}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, sort_keys=True) + "\n")
        except OSError:
            pass

    def _count(self, name: str, **data: Any) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1
        self._event(name, **data)

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def _request_json(self, url: str, payload: Mapping[str, Any], timeout_ms: int) -> Any:
        def request_json() -> Any:
            request = Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=timeout_ms / 1000) as response:  # noqa: S310
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"HTTP {response.status}")
                return json.loads(response.read().decode("utf-8"))

        return await asyncio.to_thread(request_json)

    async def _everos_value(self, key: str) -> Any:
        payload = {
            "user_id": "contextmesh",
            "app_id": self.app_id,
            "project_id": self.project_id,
            "query": "digest",
            "method": "keyword",
            "filters": {"session_id": key},
        }
        body = await self._request_json(
            f"{self.everos_url}/api/v2/memory/search", payload, self.ev_timeout_ms
        )
        messages = body.get("data", {}).get("unprocessed_messages", []) if isinstance(body, Mapping) else []
        content = messages[0].get("content") if messages and isinstance(messages[0], Mapping) else None
        if not isinstance(content, str):
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content

    async def _everos_lookup(self, key: str) -> DigestRecord | None:
        return DigestRecord.from_value(await self._everos_value(key))

    async def _everos_task_result_lookup(self, key: str) -> str | None:
        value = await self._everos_value(key)
        if isinstance(value, Mapping) and isinstance(value.get("full"), str):
            return value["full"]
        return None

    async def _everos_store(self, key: str, record: DigestRecord) -> bool:
        payload = {
            "session_id": key,
            "app_id": self.app_id,
            "project_id": self.project_id,
            "messages": [
                {
                    "sender_id": "contextmesh",
                    "role": "assistant",
                    "timestamp": int(time.time() * 1000),
                    "content": json.dumps(record.to_value(), sort_keys=True),
                }
            ],
        }
        body = await self._request_json(
            f"{self.everos_url}/api/v2/memory/add", payload, self.ev_timeout_ms
        )
        status = body.get("data", {}).get("status") if isinstance(body, Mapping) else None
        if status != "accumulated":
            self._event("everos_unexpected_status", key=key, status=status)
            return False
        return True

    async def _chat_complete(self, system: str, content: str, max_tokens: int) -> str | None:
        """Run the configured authenticated local CLI as the digest provider.

        The outer validation arm and this process use Claude Code's local
        authentication; no API key or OpenAI-compatible proxy is involved.
        A failed child is deliberately a soft miss so the read path remains
        fail-open and stores a do-not-digest record rather than blocking work.
        """
        if self.summarizer_url:
            return await self._http_chat_complete(system, content, max_tokens)
        if self.summarizer_command.casefold() == "codex":
            return await self._codex_chat_complete(system, content, max_tokens)
        if self.summarizer_command.casefold() == "deterministic":
            return self._deterministic_digest(content)
        if not self.summarizer_command or shutil.which(self.summarizer_command) is None:
            self._event("summarizer_unavailable", command=self.summarizer_command)
            return None
        max_chars = max(1, int(len(content) * 0.30))
        prompt = (
            f"{system}\n\nSource to digest:\n{content}\n\n"
            f"HARD OUTPUT CAP: {max_chars} characters, including whitespace. "
            "Do not explain the cap; omit lower-priority detail until the response fits."
        )
        child_environment = os.environ.copy()
        # Claude Code refuses accidental nesting when this variable is inherited
        # from a Claude parent, but this bounded provider child is intentional.
        child_environment.pop("CLAUDECODE", None)
        async with self._summarizer_semaphore:
            process = await asyncio.create_subprocess_exec(
                self.summarizer_command,
                "-p",
                "--model",
                self.summarizer_model,
                "--max-turns",
                "1",
                "--max-budget-usd",
                self.summarizer_budget_usd,
                "--output-format",
                "json",
                "--no-session-persistence",
                "--dangerously-skip-permissions",
                prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_environment,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self.sum_timeout_ms / 1000
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                self._event("summarizer_timeout", timeout_ms=self.sum_timeout_ms)
                return None
        try:
            body = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError:
            self._event("summarizer_invalid_json")
            return None
        self._record_cli_usage(body)
        if process.returncode != 0 or body.get("is_error") is True:
            self._event(
                "summarizer_failed",
                exit_code=process.returncode,
                stderr=stderr.decode("utf-8", errors="replace")[-500:],
            )
            return None
        text = body.get("result") if isinstance(body, Mapping) else None
        return text.strip() if isinstance(text, str) and text.strip() else None

    async def _codex_chat_complete(self, system: str, content: str, max_tokens: int) -> str | None:
        """Use a fully captured, bypassed Codex child for a local digest.

        This route exists for the measured Codex comparison only.  Its exact
        token fields are emitted to the ContextMesh event log so callers can
        include digest overhead rather than treating it as free context.
        """
        executable = shutil.which("codex")
        if executable is None:
            self._event("summarizer_unavailable", command="codex")
            return None
        max_chars = max(1, int(len(content) * 0.30))
        prompt = (
            f"{system}\n\nSource to digest:\n{content}\n\n"
            f"HARD OUTPUT CAP: {max_chars} characters, including whitespace. "
            "Do not explain the cap; omit lower-priority detail until the response fits."
        )
        handle = tempfile.NamedTemporaryFile(prefix="contextmesh-codex-", suffix=".md", delete=False)
        handle.close()
        final_path = Path(handle.name)
        child_environment = os.environ.copy()
        child_environment.pop("CLAUDECODE", None)
        child_environment["CONTEXTMESH_DIGEST_CHILD"] = "1"
        command = (
            executable,
            "exec",
            "--ignore-user-config",
            "--enable",
            "fast_mode",
            "--model",
            self.summarizer_model,
            "--config",
            "model_reasoning_effort=high",
            "--config",
            "service_tier=priority",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "--output-last-message",
            str(final_path),
            "-",
        )
        try:
            async with self._summarizer_semaphore:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=child_environment,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(prompt.encode("utf-8")),
                        timeout=self.sum_timeout_ms / 1000,
                    )
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    self._event("summarizer_timeout", timeout_ms=self.sum_timeout_ms)
                    return None
            totals = self._codex_usage(stdout)
            self._event("summarizer_codex_usage", **totals)
            if process.returncode != 0:
                self._event(
                    "summarizer_failed",
                    exit_code=process.returncode,
                    stderr=stderr.decode("utf-8", errors="replace")[-500:],
                )
                return None
            text = final_path.read_text(encoding="utf-8") if final_path.is_file() else ""
            return text.strip() or None
        finally:
            try:
                final_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _codex_usage(stdout: bytes) -> dict[str, int]:
        """Sum terminal Codex usage records without accepting malformed values."""
        totals = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping) or event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, Mapping):
                continue
            for field in totals:
                value = usage.get(field, 0)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    totals[field] += value
        return totals

    def _deterministic_digest(self, content: str) -> str | None:
        """Return a bounded structural outline without launching another model.

        The headless comparison has exactly one Terra coordinator and four Luna
        workers per arm. This adapter keeps ContextMesh live without adding a
        sixth provider child or a deferred Claude invocation.
        """
        limit = max(1, int(len(content) * 0.25))
        candidates: list[str] = []
        for index, raw in enumerate(content.splitlines(), start=1):
            line = raw.strip()
            if index <= 2 or line.startswith(
                ("import ", "from ", "class ", "def ", "async def ")
            ):
                candidates.append(f"L{index}: {line[:160]}")
        if not candidates:
            candidates = [
                f"L{index}: {line[:160]}"
                for index, line in enumerate(content.splitlines(), start=1)
                if index % 12 == 1
            ]
        digest = "\n".join(candidates)[:limit].rstrip()
        self._event("summarizer_deterministic", raw_chars=len(content), digest_chars=len(digest))
        return digest or None

    def _record_cli_usage(self, body: Mapping[str, Any]) -> None:
        """Persist child usage so the arm report includes digest overhead."""
        model_usage = body.get("modelUsage")
        if not isinstance(model_usage, Mapping):
            return
        totals = {"in_new": 0, "cache_read": 0, "cache_write": 0, "out": 0}
        provider_cost_usd = 0.0
        for value in model_usage.values():
            if not isinstance(value, Mapping):
                continue
            for field, source in (
                ("in_new", "inputTokens"),
                ("cache_read", "cacheReadInputTokens"),
                ("cache_write", "cacheCreationInputTokens"),
                ("out", "outputTokens"),
            ):
                count = value.get(source, 0)
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    totals[field] += count
            cost = value.get("costUSD", 0.0)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
                provider_cost_usd += float(cost)
        self._event(
            "summarizer_usage",
            **totals,
            provider_cost_usd=provider_cost_usd,
        )

    async def _http_chat_complete(self, system: str, content: str, max_tokens: int) -> str | None:
        """Compatibility seam for deterministic tests; unset in production."""
        payload = {
            "model": self.summarizer_model,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        }

        def request_chat() -> Any:
            headers = {"Content-Type": "application/json"}
            if self.summarizer_key:
                headers["Authorization"] = f"Bearer {self.summarizer_key}"
            request = Request(
                self.summarizer_url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urlopen(request, timeout=self.sum_timeout_ms / 1000) as response:  # noqa: S310
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"summarizer HTTP {response.status}")
                return json.loads(response.read().decode("utf-8"))

        body = await asyncio.to_thread(request_chat)
        choices = body.get("choices", []) if isinstance(body, Mapping) else []
        text = choices[0].get("message", {}).get("content") if choices else None
        return text.strip() if isinstance(text, str) and text.strip() else None

    async def _fill_gate(
        self,
        entry: GateEntry,
        hash_value: str,
        key: str,
        path: str,
        lines: int,
        content: str,
    ) -> None:
        """Finish one owner lookup/summarize/store section and release waiters."""
        record: DigestRecord | None = None
        try:
            record = await self._lookup(key)
            if record is None:
                digest = await self._summarize(DIGEST_SYSTEM, f"File: {path}\n\n{content[:48000]}", 8000)
                maximum = max(1, int(len(content) * self.max_digest_ratio))
                if digest and len(digest) > maximum and self._trim_overlong_digests:
                    digest = self._fit_digest(digest, maximum)
                    self._event(
                        "digest_trimmed",
                        path=path,
                        hash=hash_value,
                        digest_chars=len(digest),
                        max_chars=maximum,
                    )
                if digest and len(digest) <= len(content) * self.max_digest_ratio:
                    record = DigestRecord(path=path, lines=lines, raw_chars=len(content), digest=digest)
                    self._count(
                        "digest_stored",
                        path=path,
                        hash=hash_value,
                        raw_chars=len(content),
                        digest_chars=len(digest),
                    )
                else:
                    record = DigestRecord(path=path, lines=lines, raw_chars=len(content), do_not_digest=True)
                    self._count(
                        "digest_rejected",
                        path=path,
                        hash=hash_value,
                        raw_chars=len(content),
                        digest_chars=len(digest) if digest else -1,
                    )
                if not await self._store(key, record):
                    raise RuntimeError("EverOS did not retain the digest record")
            self.l1[hash_value] = record
        except Exception as error:  # Fail open: the owner and all waiters receive raw source.
            record = None
            self._count("errors", op="resolve_digest", path=path, hash=hash_value, error=str(error))
        finally:
            self.gate.pop(hash_value, None)
            if not entry.future.done():
                entry.future.set_result(record)

    @staticmethod
    def _fit_digest(digest: str, maximum: int) -> str:
        """Keep a line boundary when a real provider ignores the hard size cap."""
        clipped = digest[:maximum]
        if "\n" in clipped:
            clipped = clipped.rsplit("\n", 1)[0].rstrip()
        return clipped or digest[:maximum]

    async def resolve_digest(
        self, hash_value: str, key: str, path: str, lines: int, content: str
    ) -> DigestRecord | None:
        """Return the shared digest decision for one content hash.

        The `gate` assignment below must remain before the first await: it is
        the same-process atomic handoff that prevents cold-cache fan-out.
        """
        local = self.l1.get(hash_value)
        if local is not None:
            return local

        waiting = self.gate.get(hash_value)
        if waiting is not None:
            try:
                record = await asyncio.wait_for(
                    asyncio.shield(waiting.future), timeout=self.gate_timeout_ms / 1000
                )
            except TimeoutError:
                self._count(
                    "digest_gate_timeout",
                    path=path,
                    hash=hash_value,
                    waited_ms=int(time.time() * 1000) - waiting.started_ms,
                )
                return None
            self._count(
                "digest_gate_wait",
                path=path,
                hash=hash_value,
                waited_ms=int(time.time() * 1000) - waiting.started_ms,
            )
            if record is not None and record.digest:
                self._count("digest_gate_hit", path=path, hash=hash_value)
            return record or self.l1.get(hash_value)

        entry = GateEntry(asyncio.get_running_loop().create_future(), int(time.time() * 1000))
        self.gate[hash_value] = entry  # Must execute before any await or remote call (F3.1).
        self._count("read_raw", path=path, hash=hash_value)
        entry.task = asyncio.create_task(
            self._fill_gate(entry, hash_value, key, path, lines, content)
        )
        return None  # The owner gets raw immediately; only waiters receive the completed digest.

    @staticmethod
    def _envelope(path: str, content: str, start: int = 1) -> str:
        numbered = "\n".join(
            f"{line_number}: {line}" for line_number, line in enumerate(content.splitlines(), start=start)
        )
        return f"<path>{path}</path>\n<type>file</type>\n<content>\n{numbered}\n</content>"

    @staticmethod
    def _directory_envelope(path: str, entries: list[str]) -> str:
        content = "\n".join(entries)
        return f"<path>{path}</path>\n<type>directory</type>\n<content>\n{content}\n</content>"

    @staticmethod
    def _directory_entries(path: Path) -> list[str]:
        entries = [
            f"{entry.name}/" if entry.is_dir() else entry.name
            for entry in sorted(path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower()))
        ]
        if len(entries) > 200:
            return [*entries[:200], f"... {len(entries) - 200} additional entries omitted"]
        return entries

    async def read(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read one file, serving a digest for eligible whole-file reads."""
        if os.environ.get("REASONRENDER_ARM") == "full":
            self._event("packet_insufficient", path=path)
        source_path = Path(path)
        try:
            if source_path.is_dir():
                entries = await asyncio.to_thread(self._directory_entries, source_path)
                self._event("directory_listing", path=path, entries=len(entries))
                return self._directory_envelope(path, entries)
        except OSError as error:
            self._count("errors", op="read_directory", path=path, error=str(error))
            return f"ContextMesh read failed for {path}: {error}"
        try:
            source = await asyncio.to_thread(source_path.read_text, encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            self._count("errors", op="read", path=path, error=str(error))
            return f"ContextMesh read failed for {path}: {error}"

        all_lines = source.splitlines()
        if offset is not None or limit is not None:
            start = max(offset or 1, 1)
            end = start - 1 + limit if limit is not None else None
            self._event("escape_hatch", path=path, offset=offset, limit=limit)
            return self._envelope(path, "\n".join(all_lines[start - 1 : end]), start=start)
        if len(all_lines) < self.min_lines:
            return self._envelope(path, source)

        hash_value = self._hash(source)
        key = f"digest:{hash_value[:56]}:v1"
        record = await self.resolve_digest(hash_value, key, path, len(all_lines), source)
        if record is None or not record.digest:
            return self._envelope(path, source)

        self._count("digest_hit", path=path, hash=hash_value)
        return self._envelope(
            path,
            "\n".join(
                (
                    "[ContextMesh] Structural digest; use offset and limit only when exact source is necessary.",
                    "",
                    record.digest,
                    "",
                    f"(ContextMesh digest of {len(all_lines)} lines.)",
                )
            ),
        )

    async def expand_result(self, task_id: str) -> str:
        try:
            result = await self._task_result_lookup(f"taskresult:{task_id}")
        except Exception as error:  # Same fail-open contract as read.
            self._count("errors", op="expand_result", task_id=task_id, error=str(error))
            return f"expand_result failed ({error}); no stored result is available."
        if result is None:
            return f"No stored result found for task_id={task_id}"
        return result


TOOLS = [
    {
        "name": "read",
        "description": "Read a file or list a directory; whole-file reads may receive a ContextMesh structural digest.",
        "inputSchema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
            },
        },
    },
    {
        "name": "expand_result",
        "description": "Retrieve a full ContextMesh task result stored under its task id.",
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string"}},
        },
    },
]


async def handle_request(mesh: ContextMesh, request: Mapping[str, Any]) -> dict[str, Any] | None:
    """Handle one newline-delimited JSON-RPC MCP request."""
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") if isinstance(request.get("params"), Mapping) else {}
    if not isinstance(method, str):
        return _error(request_id, -32600, "invalid request")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "contextmesh", "version": "1"},
            },
        )
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method != "tools/call":
        return _error(request_id, -32601, f"method not found: {method}")

    name = params.get("name")
    arguments = params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
    try:
        if name == "read" and isinstance(arguments.get("path"), str):
            text = await mesh.read(
                arguments["path"],
                arguments.get("offset") if isinstance(arguments.get("offset"), int) else None,
                arguments.get("limit") if isinstance(arguments.get("limit"), int) else None,
            )
        elif name == "expand_result" and isinstance(arguments.get("task_id"), str):
            text = await mesh.expand_result(arguments["task_id"])
        else:
            return _result(request_id, {"content": [{"type": "text", "text": "invalid tool arguments"}], "isError": True})
    except Exception as error:  # The protocol must remain live after one malformed call.
        mesh._count("errors", op="tools/call", error=str(error))
        return _result(request_id, {"content": [{"type": "text", "text": str(error)}], "isError": True})
    return _result(request_id, {"content": [{"type": "text", "text": text}]})


def _result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def serve_stdio(
    mesh: ContextMesh | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None
) -> None:
    """Serve newline-delimited MCP JSON-RPC without a third-party SDK."""
    mesh = mesh or ContextMesh()
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    write_lock = asyncio.Lock()
    pending: set[asyncio.Task[None]] = set()

    async def respond(request: Mapping[str, Any]) -> None:
        response = await handle_request(mesh, request)
        if response is None:
            return
        async with write_lock:
            stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            stdout.flush()

    while line := await asyncio.to_thread(stdin.readline):
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            async with write_lock:
                stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
                stdout.flush()
            continue
        if not isinstance(request, Mapping):
            continue
        task = asyncio.create_task(respond(request))
        pending.add(task)
        task.add_done_callback(pending.discard)
    if pending:
        await asyncio.gather(*pending)


if __name__ == "__main__":
    asyncio.run(serve_stdio())
