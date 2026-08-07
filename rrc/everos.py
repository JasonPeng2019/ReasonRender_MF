"""Small fixed-scope HTTP client for the RRC EverOS case index."""

from __future__ import annotations

import json
import time
import urllib.request
import uuid
from typing import Any


class EverOSClient:
    """Call only the EverOS namespace owned by the RRC runtime."""

    APP_ID = "reasonrender"
    PROJECT_ID = "rrc-template-index"
    USER_ID = "rrc-runtime"
    TOP_K = 3
    MIN_SCORE = 0.3

    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

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
                raise TimeoutError(
                    f"EverOS index did not become ready within {timeout:.1f}s"
                )

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
                raise TimeoutError(
                    f"EverOS index did not become ready within {timeout:.1f}s"
                )
            time.sleep(min(max(poll_interval, 0.0), remaining))

    def index(self, case_shape: str, external_ref: str) -> None:
        """Index only a stable case shape and its SQLite correlation ref."""

        session_id = str(uuid.uuid4())
        self._post(
            "/api/v2/memory/add",
            {
                "session_id": session_id,
                "external_ref": external_ref,
                "app_id": self.APP_ID,
                "project_id": self.PROJECT_ID,
                "messages": [
                    {
                        "role": "user",
                        "sender_id": self.USER_ID,
                        "timestamp": int(time.time() * 1000),
                        "content": case_shape,
                    }
                ],
            },
        )
        self._post(
            "/api/v2/memory/flush",
            {
                "session_id": session_id,
                "external_ref": external_ref,
                "app_id": self.APP_ID,
                "project_id": self.PROJECT_ID,
            },
        )

    def search(self, case_shape: str) -> list[tuple[str, float]]:
        """Search only a stable case shape and return ref/score candidates."""

        response = self._post(
            "/api/v2/memory/search",
            {
                "user_id": self.USER_ID,
                "app_id": self.APP_ID,
                "project_id": self.PROJECT_ID,
                "query": case_shape,
                "method": "keyword",
                "top_k": self.TOP_K,
                "min_score": self.MIN_SCORE,
            },
        )
        episodes = response.get("data", {}).get("episodes", [])
        candidates: list[tuple[str, float]] = []
        for episode in episodes:
            external_ref = episode.get("external_ref")
            score = episode.get("score")
            if (
                isinstance(external_ref, str)
                and external_ref.strip()
                and isinstance(score, (int, float))
                and not isinstance(score, bool)
            ):
                candidates.append((external_ref, float(score)))
        return candidates
