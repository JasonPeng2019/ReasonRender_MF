"""EverOS-to-SQLite retrieval join for canonical RRC templates."""

from __future__ import annotations

import re

from rrc.contract import Candidate, Config, RetrievalPort, SolveOutcome, Task, Template
from rrc.everos import EverOSClient
from rrc.pipeline.template import parse_task_metadata
from rrc.store import SQLiteTemplateStore


def task_case_shape(task: Task) -> str:
    """Return stable task text with concrete slot values removed."""

    _, values = parse_task_metadata(task)
    nonblank = [line.strip() for line in task.text.splitlines() if line.strip()]
    # The strict Lane A grammar guarantees that the final line is RRC_SLOT_VALUES.
    shape = "\n".join(nonblank[:-1])
    for name, concrete in sorted(values.items(), key=lambda item: len(item[1]), reverse=True):
        placeholder = "{" + name + "}"
        if concrete.isidentifier():
            shape = re.sub(rf"(?<!\w){re.escape(concrete)}(?!\w)", placeholder, shape)
        else:
            shape = shape.replace(concrete, placeholder)
    return shape


class EverOSRetrieval(RetrievalPort):
    """Join EverOS candidates to the canonical SQLite template store."""

    def __init__(self, store: SQLiteTemplateStore, client: EverOSClient) -> None:
        self._store = store
        self._client = client
        self._last_stored_ref: str | None = None
        self._last_retrieved_ref: str | None = None
        self._selected_template: Template | None = None

    @property
    def last_stored_ref(self) -> str | None:
        return self._last_stored_ref

    @property
    def last_retrieved_ref(self) -> str | None:
        return self._last_retrieved_ref

    @property
    def selected_template(self) -> Template | None:
        return self._selected_template

    def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
        """Return bounded EverOS candidates without reading their content."""

        self._last_retrieved_ref = None
        self._selected_template = None
        candidates = self._client.search(
            task_case_shape(task), top_k=cfg.top_k, min_score=cfg.tau_floor
        )
        return [
            Candidate(external_ref, score)
            for external_ref, score in candidates
            if external_ref and 0.0 <= score <= 1.0
        ]

    def get_template(self, external_ref: str) -> Template | None:
        """Resolve one candidate strictly through the SQLite store of record."""

        template = self._store.get(external_ref)
        if template is not None:
            self._last_retrieved_ref = external_ref
            self._selected_template = template
        return template

    def store(
        self,
        task: Task,
        template: Template,
        outcome: SolveOutcome,
    ) -> None:
        """Write SQLite first, then seed and flush the EverOS case index."""

        del outcome
        self._store.put(template)
        self._client.index(task_case_shape(task), template.external_ref)
        self._last_stored_ref = template.external_ref
