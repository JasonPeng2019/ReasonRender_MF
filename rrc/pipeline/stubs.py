"""Deterministic fakes for Lane A tests and the Lane B integration seam."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from rrc.contract import (
    Candidate,
    Completion,
    Config,
    ModelRole,
    RunContext,
    ScoreV1,
    SolveOutcome,
    Task,
    Template,
    Usage,
)


def fake_completion(text: str, *, model: str = "fake", tokens: int = 1) -> Completion:
    """Build a deterministic metered completion."""

    return Completion(text, Usage(0, tokens, tokens), model)


@dataclass
class FakeModel:
    """Stage-keyed FIFO model whose calls are observable."""

    responses: Mapping[str, Sequence[str | Completion | Callable[[str], str | Completion]]]
    provider: str = "fake"
    calls: list[tuple[ModelRole, str, RunContext, str]] = field(default_factory=list)
    _remaining: dict[str, list[str | Completion | Callable[[str], str | Completion]]] = field(
        init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._remaining = {stage: list(items) for stage, items in self.responses.items()}

    def complete(
        self,
        role: ModelRole,
        prompt: str,
        ctx: RunContext,
        stage: str,
    ) -> Completion:
        self.calls.append((role, prompt, ctx, stage))
        queue = self._remaining.get(stage)
        if not queue:
            raise AssertionError(f"no fake completion queued for stage {stage}")
        response = queue.pop(0)
        if callable(response):
            response = response(prompt)
        return response if isinstance(response, Completion) else fake_completion(response)


@dataclass
class InMemoryRetrieval:
    """Tiny exact-template store with deterministic retrieve/get/store counters."""

    templates: dict[str, Template] = field(default_factory=dict)
    retrieve_calls: int = 0
    get_calls: int = 0
    store_calls: int = 0
    stored: list[tuple[Task, Template, SolveOutcome]] = field(default_factory=list)
    store_error: Exception | None = None
    authority_id: str = "in-memory-test"
    database_uuid: str = "0" * 64

    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
        self.retrieve_calls += 1
        refs = list(reversed(tuple(self.templates)))[: cfg.top_k]
        return [Candidate(external_ref=ref, score=ScoreV1(1, 1)) for ref in refs]

    def get_template(self, external_ref: str) -> Template | None:
        self.get_calls += 1
        return self.templates.get(external_ref)

    def store(self, task: Task, template: Template, outcome: SolveOutcome) -> None:
        self.store_calls += 1
        if self.store_error is not None:
            raise self.store_error
        self.templates[template.external_ref] = template
        self.stored.append((task, template, outcome))
