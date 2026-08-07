"""EverOS-to-SQLite retrieval join for generic RRC templates."""

from __future__ import annotations

import uuid

from rrc.contract import Memory, Spec, Task, Template
from rrc.everos import EverOSClient
from rrc.store import SQLiteTemplateStore


class EverOSRetrieval(Memory):
    """Join fixed-namespace EverOS candidates to RRC-owned SQLite rows."""

    def __init__(self, store: SQLiteTemplateStore, client: EverOSClient) -> None:
        self._store = store
        self._client = client
        self._last_stored_ref: str | None = None
        self._last_retrieved_ref: str | None = None
        self._selected_template: Template | None = None

    @property
    def last_stored_ref(self) -> str | None:
        """Return the ref whose SQLite row and EverOS index write succeeded."""

        return self._last_stored_ref

    @property
    def last_retrieved_ref(self) -> str | None:
        """Return the ref for the last successfully joined template."""

        return self._last_retrieved_ref

    @property
    def selected_template(self) -> Template | None:
        """Return the last selected generic template, if any."""

        return self._selected_template

    def put(self, task: Task, spec: Spec) -> None:
        """Persist a generic template before indexing its task shape."""

        if task.case_shape is None:
            raise ValueError("task case_shape is required for retrieval")
        template = Template(str(uuid.uuid4()), spec, task.slot_names)
        self._store.put(template)
        self._client.index(task.case_shape, template.external_ref)
        self._last_stored_ref = template.external_ref

    def get(self, task: Task) -> Spec | None:
        """Return the first candidate whose stored slot schema matches exactly."""

        if task.case_shape is None:
            raise ValueError("task case_shape is required for retrieval")
        self._last_retrieved_ref = None
        self._selected_template = None
        for external_ref, _score in self._client.search(task.case_shape):
            template = self._store.get(external_ref)
            if template is not None and set(template.slot_names) == set(task.slot_names):
                self._last_retrieved_ref = external_ref
                self._selected_template = template
                return template.spec
        return None
