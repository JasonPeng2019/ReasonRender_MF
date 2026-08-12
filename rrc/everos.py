"""Small fixed-scope HTTP client for the RRC EverOS case index."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from rrc.contract import Candidate, Config, ScoreV1, Task, Template, canonical_json_bytes

if TYPE_CHECKING:
    from rrc.retrieval import RetrievalObservationV1

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


class _RawJSONNumber:
    """One exact JSON number lexeme retained before Decimal conversion."""

    __slots__ = ("raw",)

    def __init__(self, raw: str) -> None:
        self.raw = raw


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _url(value: str, *, memory: bool) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise ValueError("EverOS URL must be HTTPS or loopback HTTP without credentials/query")
    expected = "/api/v2/memory" if memory else "/health"
    if parsed.path.rstrip("/") != expected:
        raise ValueError("EverOS URL path is not canonical")
    return urlunsplit((parsed.scheme, parsed.netloc, expected, "", ""))


@dataclass(frozen=True)
class EverOSReadinessV1:
    mode: str = "cascade_pending_two_zero_v1"
    poll_interval_ms: int = 250
    max_wait_ms: int = 30_000
    consecutive_zeroes: int = 2

    def __post_init__(self) -> None:
        if (
            self.mode != "cascade_pending_two_zero_v1"
            or self.poll_interval_ms != 250
            or self.max_wait_ms != 30_000
            or self.consecutive_zeroes != 2
        ):
            raise ValueError("EverOS readiness differs from the frozen policy")

    def as_json(self) -> dict[str, object]:
        return {
            "consecutive_zeroes": 2,
            "max_wait_ms": 30_000,
            "mode": self.mode,
            "poll_interval_ms": 250,
        }


@dataclass(frozen=True)
class EverOSIsolationV1:
    instance_id: str
    data_root_sha256: str
    owner_scope: str
    mode: str = "dedicated_empty_instance_v1"

    def __post_init__(self) -> None:
        if self.mode != "dedicated_empty_instance_v1":
            raise ValueError("EverOS isolation mode is invalid")
        if _SCOPE.fullmatch(self.instance_id) is None or _SCOPE.fullmatch(self.owner_scope) is None:
            raise ValueError("EverOS isolation identity is invalid")
        if _HEX64.fullmatch(self.data_root_sha256) is None:
            raise ValueError("EverOS data-root hash is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "data_root_sha256": self.data_root_sha256,
            "instance_id": self.instance_id,
            "mode": self.mode,
            "owner_scope": self.owner_scope,
        }


@dataclass(frozen=True)
class EverOSTargetV1:
    base_url: str
    health_url: str
    workload_session_id: str
    isolation: EverOSIsolationV1
    top_k: int = 3
    min_score: ScoreV1 = ScoreV1(7, 20)
    readiness: EverOSReadinessV1 = EverOSReadinessV1()
    protocol: str = "external_ref_passthrough_v1"
    app_id: str = "default"
    project_id: str = "default"
    user_id: str = "rrc"
    track: str = "episodes"
    search_method: str = "hybrid"
    v: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _url(self.base_url, memory=True))
        object.__setattr__(self, "health_url", _url(self.health_url, memory=False))
        left = urlsplit(self.base_url)
        right = urlsplit(self.health_url)
        if (left.scheme, left.netloc) != (right.scheme, right.netloc):
            raise ValueError("EverOS base and health URLs must share one origin")
        if _SCOPE.fullmatch(self.workload_session_id) is None:
            raise ValueError("EverOS workload session ID is invalid")
        if self.isolation.owner_scope == "":  # defensive; dataclass already rejects
            raise ValueError("EverOS isolation owner is missing")
        if (
            self.protocol != "external_ref_passthrough_v1"
            or self.app_id != "default"
            or self.project_id != "default"
            or self.user_id != "rrc"
            or self.track != "episodes"
            or self.search_method != "hybrid"
            or self.top_k != 3
            or self.min_score != ScoreV1(7, 20)
            or self.v != 1
        ):
            raise ValueError("EverOS target differs from the frozen v1 route")

    def as_json(self) -> dict[str, object]:
        return {
            "app_id": self.app_id,
            "base_url": self.base_url,
            "health_url": self.health_url,
            "isolation": self.isolation.as_json(),
            "min_score": self.min_score.as_json(),
            "project_id": self.project_id,
            "protocol": self.protocol,
            "readiness": self.readiness.as_json(),
            "search_method": self.search_method,
            "top_k": self.top_k,
            "track": self.track,
            "user_id": self.user_id,
            "v": 1,
            "workload_session_id": self.workload_session_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())

    @property
    def sha256(self) -> str:
        return _sha(self.canonical_bytes())


@dataclass(frozen=True)
class EverOSDispatchV1:
    target_sha256: str
    observation_id: str
    external_ref: str
    document_sha256: str
    task_text_sha256: str
    session_id: str
    timestamp_ms: int
    add_body: dict[str, object]
    flush_body: dict[str, object]
    v: int = 1

    def __post_init__(self) -> None:
        for value, name in (
            (self.target_sha256, "target_sha256"),
            (self.observation_id, "observation_id"),
            (self.external_ref, "external_ref"),
            (self.document_sha256, "document_sha256"),
            (self.task_text_sha256, "task_text_sha256"),
        ):
            if _HEX64.fullmatch(value) is None:
                raise ValueError(f"EverOS {name} is invalid")
        if _SCOPE.fullmatch(self.session_id) is None:
            raise ValueError("EverOS dispatch session is invalid")
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("EverOS dispatch timestamp is invalid")
        if self.v != 1 or len(self.canonical_bytes()) > 320 * 1024:
            raise ValueError("EverOS dispatch version/size is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "add_body": self.add_body,
            "document_sha256": self.document_sha256,
            "external_ref": self.external_ref,
            "flush_body": self.flush_body,
            "observation_id": self.observation_id,
            "session_id": self.session_id,
            "target_sha256": self.target_sha256,
            "task_text_sha256": self.task_text_sha256,
            "timestamp_ms": self.timestamp_ms,
            "v": 1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())

    @property
    def sha256(self) -> str:
        return _sha(self.canonical_bytes())


@dataclass(frozen=True)
class EverOSOutboxRowV1:
    owner_scope: str
    observation_id: str
    external_ref: str
    document_sha256: str
    target_sha256: str
    dispatch_sha256: str
    case_v: int = 1
    backend: str = "everos"
    operation: str = "append_episode"

    def __post_init__(self) -> None:
        if _SCOPE.fullmatch(self.owner_scope) is None:
            raise ValueError("EverOS outbox owner scope is invalid")
        for value in (
            self.observation_id,
            self.external_ref,
            self.document_sha256,
            self.target_sha256,
            self.dispatch_sha256,
        ):
            if _HEX64.fullmatch(value) is None:
                raise ValueError("EverOS outbox hash is invalid")
        if self.case_v != 1 or self.backend != "everos" or self.operation != "append_episode":
            raise ValueError("EverOS outbox discriminator is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "case_v": self.case_v,
            "dispatch_sha256": self.dispatch_sha256,
            "document_sha256": self.document_sha256,
            "external_ref": self.external_ref,
            "observation_id": self.observation_id,
            "operation": self.operation,
            "owner_scope": self.owner_scope,
            "target_sha256": self.target_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())


def build_dispatch(
    *,
    target: EverOSTargetV1,
    task: Task,
    external_ref: str,
    document_sha256: str,
    accept_commit_id: str,
    owner_scope: str,
    timestamp_ms: int,
) -> tuple[EverOSDispatchV1, EverOSOutboxRowV1]:
    if not task.searchable_public:
        raise ValueError("EverOS dispatch requires searchable_public=true")
    observation_id = _sha(
        canonical_json_bytes(
            {
                "accept_commit_id": accept_commit_id,
                "document_sha256": document_sha256,
                "external_ref": external_ref,
                "owner_scope": owner_scope,
                "v": 1,
            }
        )
    )
    dispatch = EverOSDispatchV1(
        target_sha256=target.sha256,
        observation_id=observation_id,
        external_ref=external_ref,
        document_sha256=document_sha256,
        task_text_sha256=_sha(task.text.encode("utf-8")),
        session_id=target.workload_session_id,
        timestamp_ms=timestamp_ms,
        add_body={
            "app_id": target.app_id,
            "external_ref": external_ref,
            "messages": [
                {
                    "content": task.text,
                    "role": "user",
                    "sender_id": "rrc",
                    "timestamp": timestamp_ms,
                }
            ],
            "project_id": target.project_id,
            "session_id": target.workload_session_id,
        },
        flush_body={
            "app_id": target.app_id,
            "project_id": target.project_id,
            "session_id": target.workload_session_id,
        },
    )
    outbox = EverOSOutboxRowV1(
        owner_scope=owner_scope,
        observation_id=observation_id,
        external_ref=external_ref,
        document_sha256=document_sha256,
        target_sha256=target.sha256,
        dispatch_sha256=dispatch.sha256,
    )
    return dispatch, outbox


def parse_target(raw: bytes) -> EverOSTargetV1:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("EverOS target is not JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "app_id",
        "base_url",
        "health_url",
        "isolation",
        "min_score",
        "project_id",
        "protocol",
        "readiness",
        "search_method",
        "top_k",
        "track",
        "user_id",
        "v",
        "workload_session_id",
    }:
        raise ValueError("EverOS target schema is invalid")
    readiness = value.get("readiness")
    isolation = value.get("isolation")
    score = value.get("min_score")
    if (
        not isinstance(readiness, dict)
        or set(readiness) != {"consecutive_zeroes", "max_wait_ms", "mode", "poll_interval_ms"}
        or not isinstance(isolation, dict)
        or set(isolation) != {"data_root_sha256", "instance_id", "mode", "owner_scope"}
        or not isinstance(score, dict)
        or set(score) != {"denominator", "numerator"}
    ):
        raise ValueError("EverOS target nested schema is invalid")
    target = EverOSTargetV1(
        base_url=str(value["base_url"]),
        health_url=str(value["health_url"]),
        workload_session_id=str(value["workload_session_id"]),
        isolation=EverOSIsolationV1(
            str(isolation["instance_id"]),
            str(isolation["data_root_sha256"]),
            str(isolation["owner_scope"]),
            str(isolation["mode"]),
        ),
        top_k=value["top_k"],  # type: ignore[arg-type]
        min_score=ScoreV1(score["numerator"], score["denominator"]),  # type: ignore[arg-type]
        readiness=EverOSReadinessV1(
            str(readiness["mode"]),
            readiness["poll_interval_ms"],  # type: ignore[arg-type]
            readiness["max_wait_ms"],  # type: ignore[arg-type]
            readiness["consecutive_zeroes"],  # type: ignore[arg-type]
        ),
        protocol=str(value["protocol"]),
        app_id=str(value["app_id"]),
        project_id=str(value["project_id"]),
        user_id=str(value["user_id"]),
        track=str(value["track"]),
        search_method=str(value["search_method"]),
        v=value["v"],  # type: ignore[arg-type]
    )
    if target.canonical_bytes() != raw:
        raise ValueError("EverOS target is not canonical")
    return target


def parse_dispatch(raw: bytes) -> EverOSDispatchV1:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("EverOS dispatch is not JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "add_body",
            "document_sha256",
            "external_ref",
            "flush_body",
            "observation_id",
            "session_id",
            "target_sha256",
            "task_text_sha256",
            "timestamp_ms",
            "v",
        }
        or not isinstance(value.get("add_body"), dict)
        or not isinstance(value.get("flush_body"), dict)
    ):
        raise ValueError("EverOS dispatch schema is invalid")
    dispatch = EverOSDispatchV1(
        str(value["target_sha256"]),
        str(value["observation_id"]),
        str(value["external_ref"]),
        str(value["document_sha256"]),
        str(value["task_text_sha256"]),
        str(value["session_id"]),
        value["timestamp_ms"],  # type: ignore[arg-type]
        value["add_body"],  # type: ignore[arg-type]
        value["flush_body"],  # type: ignore[arg-type]
        value["v"],  # type: ignore[arg-type]
    )
    if dispatch.canonical_bytes() != raw:
        raise ValueError("EverOS dispatch is not canonical")
    return dispatch


def parse_outbox(raw: bytes) -> EverOSOutboxRowV1:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("EverOS outbox row is not JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "backend",
        "case_v",
        "dispatch_sha256",
        "document_sha256",
        "external_ref",
        "observation_id",
        "operation",
        "owner_scope",
        "target_sha256",
    }:
        raise ValueError("EverOS outbox schema is invalid")
    row = EverOSOutboxRowV1(
        owner_scope=str(value["owner_scope"]),
        observation_id=str(value["observation_id"]),
        external_ref=str(value["external_ref"]),
        document_sha256=str(value["document_sha256"]),
        target_sha256=str(value["target_sha256"]),
        dispatch_sha256=str(value["dispatch_sha256"]),
        case_v=value["case_v"],  # type: ignore[arg-type]
        backend=str(value["backend"]),
        operation=str(value["operation"]),
    )
    if row.canonical_bytes() != raw:
        raise ValueError("EverOS outbox row is not canonical")
    return row


def validate_route_tuple(
    target: EverOSTargetV1,
    dispatch: EverOSDispatchV1,
    outbox: EverOSOutboxRowV1,
) -> None:
    """Recheck all duplicated route/dispatch/outbox identities before commit or retry."""

    expected_add_keys = {"app_id", "external_ref", "messages", "project_id", "session_id"}
    expected_flush_keys = {"app_id", "project_id", "session_id"}
    messages = dispatch.add_body.get("messages")
    if (
        dispatch.target_sha256 != target.sha256
        or dispatch.session_id != target.workload_session_id
        or set(dispatch.add_body) != expected_add_keys
        or set(dispatch.flush_body) != expected_flush_keys
        or dispatch.add_body.get("app_id") != target.app_id
        or dispatch.add_body.get("project_id") != target.project_id
        or dispatch.add_body.get("session_id") != target.workload_session_id
        or dispatch.add_body.get("external_ref") != dispatch.external_ref
        or dispatch.flush_body
        != {
            "app_id": target.app_id,
            "project_id": target.project_id,
            "session_id": target.workload_session_id,
        }
        or not isinstance(messages, list)
        or len(messages) != 1
        or not isinstance(messages[0], dict)
        or set(messages[0]) != {"content", "role", "sender_id", "timestamp"}
        or messages[0].get("role") != "user"
        or messages[0].get("sender_id") != "rrc"
        or messages[0].get("timestamp") != dispatch.timestamp_ms
        or not isinstance(messages[0].get("content"), str)
        or _sha(str(messages[0]["content"]).encode("utf-8")) != dispatch.task_text_sha256
        or outbox.observation_id != dispatch.observation_id
        or outbox.external_ref != dispatch.external_ref
        or outbox.document_sha256 != dispatch.document_sha256
        or outbox.target_sha256 != target.sha256
        or outbox.dispatch_sha256 != dispatch.sha256
        or outbox.owner_scope != target.isolation.owner_scope
    ):
        raise ValueError("EverOS target/dispatch/outbox tuple is inconsistent")


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
                "user_id": self.USER_ID,
                "app_id": self.APP_ID,
                "project_id": self.PROJECT_ID,
                "query": case_shape,
                "method": "keyword",
                "top_k": self.TOP_K if top_k is None else top_k,
                "min_score": self.MIN_SCORE if min_score is None else min_score,
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


class EverOSOutboxRepository(Protocol):
    def pending_everos_outbox(
        self, owner_scope: str, *, limit: int = 100
    ) -> tuple[tuple[str, str], ...]: ...

    def claim_everos_outbox(
        self, owner_scope: str, observation_id: str, target_sha256: str
    ) -> tuple[int, bytes, bytes]: ...

    def finish_everos_outbox(
        self,
        owner_scope: str,
        observation_id: str,
        target_sha256: str,
        *,
        generation: int,
        success: bool,
        error: str | None = None,
    ) -> None: ...

    def mark_everos_draining(
        self,
        owner_scope: str,
        observation_id: str,
        target_sha256: str,
        *,
        generation: int,
    ) -> None: ...


class EverOSOutboxDispatcher:
    """Dispatch sealed EverOS append/flush rows with visible at-least-once retries."""

    def __init__(self, repository: EverOSOutboxRepository) -> None:
        self._repository = repository

    @staticmethod
    def _request(
        url: str,
        *,
        payload: dict[str, object] | None,
        deadline: float,
    ) -> dict[str, object]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("EverOS request wall deadline expired")
        request = urllib.request.Request(
            url,
            data=None if payload is None else canonical_json_bytes(payload),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        with urllib.request.urlopen(request, timeout=remaining) as response:
            raw = _response_bytes(response, deadline=deadline)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("EverOS response is not JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("EverOS response must be an object")
        return value

    def _ready(self, target: EverOSTargetV1, *, deadline: float) -> None:
        consecutive = 0
        while consecutive < target.readiness.consecutive_zeroes:
            payload = self._request(target.health_url, payload=None, deadline=deadline)
            cascade = payload.get("cascade")
            ready = (
                isinstance(cascade, dict)
                and cascade.get("healthy") is True
                and cascade.get("pending") == 0
            )
            consecutive = consecutive + 1 if ready else 0
            if consecutive < target.readiness.consecutive_zeroes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("EverOS readiness wall deadline expired")
                time.sleep(min(target.readiness.poll_interval_ms / 1000, remaining))

    def dispatch_pending(self, owner_scope: str, *, limit: int = 100) -> int:
        """Attempt each currently pending row once; return successful acknowledgements."""

        sent = 0
        for observation_id, target_sha in self._repository.pending_everos_outbox(
            owner_scope, limit=limit
        ):
            generation, target_raw, dispatch_raw = self._repository.claim_everos_outbox(
                owner_scope, observation_id, target_sha
            )
            try:
                target = parse_target(target_raw)
                dispatch = parse_dispatch(dispatch_raw)
                if target.sha256 != target_sha or dispatch.observation_id != observation_id:
                    raise ValueError("claimed EverOS identity differs from sealed payload")
                deadline = time.monotonic() + target.readiness.max_wait_ms / 1000
                add_response = self._request(
                    target.base_url + "/add",
                    payload=dispatch.add_body,
                    deadline=deadline,
                )
                add_data = add_response.get("data")
                if not isinstance(add_data, dict) or add_data.get("status") not in {
                    "accumulated",
                    "extracted",
                }:
                    raise ValueError("EverOS add acknowledgement is invalid")
                self._repository.mark_everos_draining(
                    owner_scope,
                    observation_id,
                    target_sha,
                    generation=generation,
                )
                flush_response = self._request(
                    target.base_url + "/flush",
                    payload=dispatch.flush_body,
                    deadline=deadline,
                )
                flush_data = flush_response.get("data")
                if not isinstance(flush_data, dict) or flush_data.get("status") not in {
                    "extracted",
                    "no_extraction",
                }:
                    raise ValueError("EverOS flush acknowledgement is invalid")
                self._ready(target, deadline=deadline)
            except Exception as exc:
                self._repository.finish_everos_outbox(
                    owner_scope,
                    observation_id,
                    target_sha,
                    generation=generation,
                    success=False,
                    error=f"{type(exc).__name__}:{str(exc)[:2048]}",
                )
                continue
            self._repository.finish_everos_outbox(
                owner_scope,
                observation_id,
                target_sha,
                generation=generation,
                success=True,
            )
            sent += 1
        return sent


class EverOSRetrievalRepository(EverOSOutboxRepository, Protocol):
    authority_id: str
    database_uuid: str

    def get_bundle(self, external_ref: str) -> bytes | None: ...

    def retrieval_observations(self, owner_scope: str) -> tuple[RetrievalObservationV1, ...]: ...

    def lexical_ranks(self, owner_scope: str, query: str) -> dict[tuple[str, str], int]: ...

    def everos_route_valid(self, owner_scope: str, target_sha256: str) -> bool: ...


def _response_bytes(response: object, *, deadline: float) -> bytes:
    """Read one response under a true loop-checked wall deadline and 64 KiB cap."""

    read = getattr(response, "read1", None)
    if not callable(read):
        read = getattr(response, "read", None)
    if not callable(read):
        raise ValueError("EverOS response is not readable")
    chunks: list[bytes] = []
    size = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("EverOS response exceeded its wall deadline")
        # urllib's timeout is otherwise a per-read idle timeout.  Refresh the
        # underlying socket deadline before every bounded read so a trickling
        # peer cannot multiply the original timeout by the number of chunks.
        candidates = (
            getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
            getattr(getattr(response, "fp", None), "_sock", None),
        )
        for candidate in candidates:
            setter = getattr(candidate, "settimeout", None)
            if callable(setter):
                setter(remaining)
                break
        chunk = read(min(16 * 1024, 64 * 1024 + 1 - size))
        if not isinstance(chunk, bytes):
            raise ValueError("EverOS response reader returned non-bytes")
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > 64 * 1024:
            raise ValueError("EverOS response exceeds 64 KiB")
    return b"".join(chunks)


def _http_request(url: str, body: bytes, *, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("EverOS request wall deadline expired")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=remaining) as response:
        return _response_bytes(response, deadline=deadline)


def _search_body(target: EverOSTargetV1, task: Task) -> bytes:
    # canonical_json_bytes deliberately rejects floats.  The frozen EverOS wire
    # requires the exact JSON number token 0.35, so splice that one policy-owned
    # token into an otherwise canonical object rather than accepting caller floats.
    marker = '"__rrcv2_score_7_20__"'
    raw = json.dumps(
        {
            "app_id": target.app_id,
            "method": target.search_method,
            "min_score": "__rrcv2_score_7_20__",
            "project_id": target.project_id,
            "query": task.text,
            "top_k": target.top_k,
            "user_id": target.user_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if raw.count(marker) != 1:
        raise ValueError("EverOS search score marker is ambiguous")
    return raw.replace(marker, "0.35").encode("utf-8")


def _remote_score(value: object) -> ScoreV1 | None:
    if not isinstance(value, _RawJSONNumber):
        return None
    raw = value.raw
    if len(raw.encode("ascii", errors="ignore")) != len(raw) or not 1 <= len(raw) <= 128:
        return None
    if _JSON_NUMBER.fullmatch(raw) is None:
        return None
    try:
        decimal = Decimal(raw)
    except InvalidOperation:
        return None
    if not decimal.is_finite() or decimal < 0 or decimal > 1:
        return None
    try:
        with localcontext() as context:
            context.prec = max(256, len(raw) * 2)
            quantized = decimal.quantize(Decimal("0.000000001"), rounding=ROUND_HALF_EVEN)
            scaled = int(quantized * 1_000_000_000)
    except (InvalidOperation, OverflowError, ValueError):
        return None
    reduced = Fraction(scaled, 1_000_000_000)
    try:
        return ScoreV1(reduced.numerator, reduced.denominator)
    except (TypeError, ValueError):
        return None


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON numeric constant: {value}")


class EverOSHybridRetrieval:
    """Optional patched-EverOS search with authoritative local bundle expansion."""

    def __init__(
        self,
        repository: EverOSRetrievalRepository,
        *,
        request: object = _http_request,
        dispatcher: EverOSOutboxDispatcher | None = None,
    ) -> None:
        from rrc.retrieval import SQLiteHybridRetrieval

        self._repository = repository
        self._local = SQLiteHybridRetrieval(repository)
        self._request = request
        self._dispatcher = dispatcher or EverOSOutboxDispatcher(repository)
        self.authority_id = repository.authority_id
        self.database_uuid = repository.database_uuid
        self._classifications: dict[str, Literal["exact", "near"]] = {}
        self.backend_status: Literal["remote", "local_fallback"] = "remote"

    @property
    def backend_valid(self) -> bool:
        return self.backend_status == "remote"

    def get_template(self, external_ref: str) -> Template | None:
        return self._local.get_template(external_ref)

    def projection_failure(self, task: Task) -> str | None:
        return self._local.projection_failure(task)

    def _fallback(self, task: Task, cfg: Config) -> list[Candidate]:
        self.backend_status = "local_fallback"
        candidates = self._local.retrieve(task, cfg)
        self._classifications = {
            candidate.external_ref: cast(
                Literal["exact", "near"],
                self._local.classify(task, candidate.external_ref),
            )
            for candidate in candidates
        }
        return candidates

    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
        from rrc.pipeline.template import TemplateError, render

        if cfg.memory_backend != "everos" or cfg.everos_target is None:
            return self._fallback(task, cfg)
        target = parse_target(cfg.everos_target)
        if target.isolation.owner_scope != cfg.owner_scope:
            return self._fallback(task, cfg)
        try:
            self._dispatcher.dispatch_pending(cfg.owner_scope)
            if self._repository.pending_everos_outbox(cfg.owner_scope):
                raise TimeoutError("EverOS dependent search still has pending observations")
            if not self._repository.everos_route_valid(cfg.owner_scope, target.sha256):
                raise RuntimeError("EverOS delivery generation is ambiguous")
            deadline = time.monotonic() + target.readiness.max_wait_ms / 1000
            requester = self._request
            if not callable(requester):
                raise TypeError("EverOS request adapter is not callable")
            raw = requester(
                target.base_url + "/search",
                _search_body(target, task),
                deadline=deadline,
            )
            if not isinstance(raw, bytes) or len(raw) > 64 * 1024:
                raise ValueError("EverOS search response is not bounded bytes")
            response = json.loads(
                raw,
                parse_float=_RawJSONNumber,
                parse_int=_RawJSONNumber,
                parse_constant=_reject_json_constant,
            )
            episodes = response.get("data", {}).get("episodes")
            if not isinstance(episodes, list):
                raise ValueError("EverOS search response has no episode list")
        except Exception:
            return self._fallback(task, cfg)

        observations = self._repository.retrieval_observations(cfg.owner_scope)
        by_ref: dict[str, list[object]] = {}
        for observation in observations:
            by_ref.setdefault(observation.external_ref, []).append(observation)
        ranked: list[tuple[ScoreV1, int, object]] = []
        for remote_rank, episode in enumerate(episodes[: target.top_k], 1):
            if not isinstance(episode, dict):
                continue
            external_ref = episode.get("external_ref")
            score = _remote_score(episode.get("score"))
            if (
                not isinstance(external_ref, str)
                or _HEX64.fullmatch(external_ref) is None
                or score is None
                or score < target.min_score
            ):
                continue
            for observation in by_ref.get(external_ref, ()):
                ranked.append((score, remote_rank, observation))

        exact: dict[str, tuple[ScoreV1, int, str]] = {}
        near: dict[str, tuple[ScoreV1, int, str]] = {}
        for score, remote_rank, opaque in ranked:
            observation = opaque
            external_ref = getattr(observation, "external_ref", None)
            document_sha = getattr(observation, "document_sha256", None)
            if not isinstance(external_ref, str) or not isinstance(document_sha, str):
                continue
            template = self.get_template(external_ref)
            if template is None or template.slot_contexts != getattr(
                observation, "slot_schema", None
            ):
                continue
            target_branch: dict[str, tuple[ScoreV1, int, str]] | None = None
            if (
                task.shape is not None
                and getattr(observation, "shape", None) is not None
                and task.shape == getattr(observation, "shape")
            ):
                try:
                    render(template, dict(task.slot_values or ()))
                except (TemplateError, TypeError, ValueError):
                    continue
                target_branch = exact
            elif task.family is not None and task.family == getattr(observation, "family", None):
                target_branch = near
            previous = None if target_branch is None else target_branch.get(external_ref)
            current = (score, remote_rank, document_sha)
            if target_branch is not None and (
                previous is None
                or previous[0] < score
                or (previous[0] == score and current[1:] < previous[1:])
            ):
                target_branch[external_ref] = current
        for external_ref in exact:
            near.pop(external_ref, None)
        selected = exact if exact else near
        ordered = sorted(
            selected.items(),
            key=lambda item: (
                -Fraction(item[1][0].numerator, item[1][0].denominator),
                item[1][1],
                item[0],
                item[1][2],
            ),
        )
        if not exact:
            ordered = ordered[:2]
        self._classifications = {
            external_ref: "exact" if exact else "near" for external_ref, _ in ordered
        }
        self.backend_status = "remote"
        return [Candidate(external_ref, row[0]) for external_ref, row in ordered]

    def classify(self, task: Task, external_ref: str) -> Literal["exact", "near", "miss"]:
        del task
        return self._classifications.get(external_ref, "miss")
