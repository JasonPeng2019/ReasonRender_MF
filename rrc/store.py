"""RRC-owned SQLite storage for exact generic templates."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from rrc.contract import Spec, Template


class SQLiteTemplateStore:
    """Persist generic templates keyed only by their external reference."""

    def __init__(self, database: str | Path) -> None:
        self._connection = sqlite3.connect(str(database))
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
                external_ref TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def put(self, template: Template) -> None:
        """Upsert one generic template by its external reference."""

        payload = json.dumps(
            {
                "signature": template.spec.signature,
                "template": template.spec.template,
                "tests": template.spec.tests,
                "slot_names": list(template.slot_names),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO templates (external_ref, payload)
                VALUES (?, ?)
                ON CONFLICT(external_ref) DO UPDATE SET payload = excluded.payload
                """,
                (template.external_ref, payload),
            )

    def get(self, external_ref: str) -> Template | None:
        """Return the exact template for a reference, or ``None`` on a miss."""

        row = self._connection.execute(
            "SELECT payload FROM templates WHERE external_ref = ?",
            (external_ref,),
        ).fetchone()
        if row is None:
            return None

        payload = json.loads(row[0])
        return Template(
            external_ref=external_ref,
            spec=Spec(
                signature=payload["signature"],
                template=payload["template"],
                tests=payload["tests"],
            ),
            slot_names=tuple(payload["slot_names"]),
        )
