"""EverOS-to-SQLite retrieval join for canonical RRC templates."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from rrc.contract import Candidate, Config, RetrievalPort, ScoreV1, SolveOutcome, Task, Template
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
        # Legacy adapter only. The canonical M3 adapter is repository-bound.
        self.authority_id = "legacy-everos-adapter"
        self.database_uuid = "0" * 64

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
            task_case_shape(task),
            top_k=cfg.top_k,
            min_score=cfg.tau_floor.numerator / cfg.tau_floor.denominator,
        )
        result: list[Candidate] = []
        for external_ref, score in candidates:
            if not external_ref or not 0.0 <= score <= 1.0:
                continue
            quantized = Decimal(str(score)).quantize(Decimal("0.000000001"), ROUND_HALF_EVEN)
            fraction = Fraction(quantized)
            result.append(
                Candidate(external_ref, ScoreV1(fraction.numerator, fraction.denominator))
            )
        return result

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
