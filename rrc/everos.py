"""Small fixed-scope HTTP client for the RRC EverOS case index."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from typing import Any


class EverOSClient:
    """Call only the EverOS namespace owned by the RRC runtime."""

    APP_ID = "reasonrender"
    PROJECT_ID = "rrc-template-index"
    USER_ID = "rrc-runtime"
    TOP_K = 3
    MIN_SCORE = 0.3

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        timeout: float = 30.0,
        *,
        app_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._app_id = app_id or self.APP_ID
        self._project_id = project_id or self.PROJECT_ID
        self._user_id = user_id or self.USER_ID

    def _get(self, path: str, *, timeout: float | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(
            request, timeout=self._timeout if timeout is None else timeout
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def wait_for_index(self, timeout: float = 30.0, poll_interval: float = 0.5) -> None:
        """Wait for two consecutive ready cascade health samples."""

        if timeout <= 0:
            raise TimeoutError("EverOS index readiness timed out before polling")

        deadline = time.monotonic() + timeout
        consecutive_ready = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"EverOS index did not become ready within {timeout:.1f}s")

            ready = False
            try:
                health = self._get("/health", timeout=min(self._timeout, remaining))
                cascade = health.get("cascade") if isinstance(health, dict) else None
                ready = (
                    isinstance(cascade, dict)
                    and cascade.get("healthy") is True
                    and cascade.get("pending") == 0
                )
            except (OSError, TypeError, ValueError):
                ready = False

            consecutive_ready = consecutive_ready + 1 if ready else 0
            if consecutive_ready >= 2:
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"EverOS index did not become ready within {timeout:.1f}s")
            time.sleep(min(max(poll_interval, 0.0), remaining))

    def index(self, case_shape: str, external_ref: str) -> None:
        """Park one stable-key buffer without invoking EverOS extraction."""

        session_id = _session_id(case_shape)
        response = self._post(
            "/api/v2/memory/add",
            {
                "session_id": session_id,
                "app_id": self._app_id,
                "project_id": self._project_id,
                "messages": [
                    {
                        # This is EverOS's exact-key path: assistant buffers
                        # remain unprocessed until a caller flushes them. RRC
                        # never flushes, so EverOS never invokes its LLM.
                        "role": "assistant",
                        "sender_id": self._user_id,
                        "timestamp": int(time.time() * 1000),
                        "content": json.dumps(
                            {"schema_version": 1, "case_shape": case_shape, "external_ref": external_ref},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                ],
            },
        )
        status = response.get("data", {}).get("status") if isinstance(response, dict) else None
        if status != "accumulated":
            raise ValueError(f"EverOS did not park the RRC exact-key buffer: {status!r}")

    def search(
        self,
        case_shape: str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[tuple[str, float]]:
        """Search only a stable case shape and return ref/score candidates."""

        response = self._post(
            "/api/v2/memory/search",
            {
                "user_id": self._user_id,
                "app_id": self._app_id,
                "project_id": self._project_id,
                "query": case_shape,
                "method": "keyword",
                "filters": {"session_id": _session_id(case_shape)},
                "top_k": self.TOP_K if top_k is None else top_k,
                "min_score": self.MIN_SCORE if min_score is None else min_score,
            },
        )
        messages = response.get("data", {}).get("unprocessed_messages", [])
        candidates: list[tuple[str, float]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            try:
                payload = json.loads(message.get("content", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            external_ref = payload.get("external_ref")
            if payload.get("case_shape") == case_shape and isinstance(external_ref, str) and external_ref.strip():
                candidates.append((external_ref, 1.0))
        return candidates


def _session_id(case_shape: str) -> str:
    """Derive the opaque EverOS buffer key from the public stable case shape."""

    return "rrc:" + hashlib.sha256(case_shape.encode("utf-8")).hexdigest()
