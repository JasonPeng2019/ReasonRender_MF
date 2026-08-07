"""Frozen public seam shared by the RRCv2 implementation lanes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


@dataclass(frozen=True)
class Task:
    """One coding task in an evaluation run."""

    task_id: str
    text: str
    oracle_tests: str | None = None


@dataclass(frozen=True)
class Slots:
    """Instance values labelled by the SPEC stage for deterministic reuse."""

    entity: str | None = None
    identifiers: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    constants: tuple[str, ...] = ()
    edge_values: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Spec:
    """Model-authored implementation specification and acceptance tests."""

    plan: str
    signature: str
    contract: str
    tests: tuple[str, ...]
    slots: Slots


@dataclass(frozen=True)
class Template:
    """Genericized specification stored under an RRCv2-owned fingerprint."""

    external_ref: str
    spec_skeleton: Spec
    slot_names: tuple[str, ...]


class BranchDecision(str, Enum):
    """Result of retrieval and deterministic structural matching."""

    REUSE = "reuse"
    PRIME = "prime"
    MISS = "miss"


class ArmMode(str, Enum):
    """Evaluation arm selected for a solve."""

    BASELINE = "baseline"
    CHEAP_ALONE = "cheap_alone"
    CASCADE = "cascade"
    COLD = "cold"
    WARM = "warm"


class ModelRole(str, Enum):
    """Provider-independent model capability requested by Lane A."""

    STRONG = "strong"
    SMALL = "small"


@dataclass(frozen=True)
class Candidate:
    """Similarity-index hit resolved through the RRCv2 template store."""

    external_ref: str
    score: float


@dataclass(frozen=True)
class Usage:
    """Token usage reported by a model provider."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class Completion:
    """One provider completion and its metering information."""

    text: str
    usage: Usage
    model: str


@dataclass(frozen=True)
class CostEvent:
    """Provider-scoped cost event emitted for one model call."""

    arm: str
    task_id: str
    stage: str
    model: str
    usage: Usage
    provider: str


@dataclass(frozen=True)
class SolveOutcome:
    """Complete result returned by the Lane A solve pipeline."""

    task_id: str
    arm: str
    code: str
    passed: bool
    pass_at_1: bool | None
    branch: BranchDecision
    repairs: int
    escalated: bool
    template: Template | None
    cost_events: tuple[CostEvent, ...]


class StoreFailure(RuntimeError):
    """A post-success persistence failure retaining the completed outcome."""

    def __init__(self, outcome: SolveOutcome) -> None:
        super().__init__(f"failed to store successful outcome for task {outcome.task_id}")
        self.outcome = outcome


@dataclass
class RunContext:
    """Stable arm/task identity used for provider query attribution."""

    arm: str
    task_id: str

    def tag(self, stage: str) -> str:
        """Return the frozen Snowflake query-tag format."""

        return f"rrc:arm={self.arm};task={self.task_id};stage={stage}"


@dataclass
class Config:
    """Configuration shared across the two implementation lanes."""

    repair_cap_N: int = 1
    pyright_mode: str = "basic"
    model_provider: str = "codex"
    tau_floor: float = 0.35
    top_k: int = 3
    prefer_prime_on_shape_diff: bool = True
    everos_base: str = "http://127.0.0.1:8000/api/v2/memory"
    app_id: str = "default"
    project_id: str = "default"
    agent_identity: str = "rrc"


class ModelPort(Protocol):
    """Single-completion provider consumed by the Lane A control loop."""

    provider: str

    def complete(
        self,
        role: ModelRole,
        prompt: str,
        ctx: RunContext,
        stage: str,
    ) -> Completion:
        """Return exactly one completion for a pipeline stage."""

        ...


class RetrievalPort(Protocol):
    """Similarity-index and exact-template operations consumed by Lane A."""

    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
        """Return candidates whose score meets the configured floor."""

        ...

    def get_template(self, external_ref: str) -> Template | None:
        """Fetch an exact template from RRCv2's store of record."""

        ...

    def store(self, task: Task, template: Template, outcome: SolveOutcome) -> None:
        """Persist an accepted template and seed the similarity index."""

        ...


class Solver(Protocol):
    """Typed callable public entry point implemented by :func:`rrc.pipeline.solve`."""

    def __call__(
        self,
        task: Task,
        *,
        mode: ArmMode,
        model: ModelPort,
        retrieval: RetrievalPort,
        cfg: Config,
    ) -> SolveOutcome:
        """Solve one task through the selected Lane A arm."""

        ...


class NullRetrieval:
    """No-op retrieval implementation for cold arms and offline development."""

    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
        return []

    def get_template(self, external_ref: str) -> Template | None:
        return None

    def store(self, task: Task, template: Template, outcome: SolveOutcome) -> None:
        return None
