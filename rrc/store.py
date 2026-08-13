"""RRC-owned SQLite storage for exact generic templates."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from rrc.contract import Slots, Spec, Template
from rrc.orchestrator_contract import PlanSpecPacket, PlanSpecTemplate


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
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS plan_spec_templates (
                external_ref TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stage_plan_states (
                stage_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        """Release the SQLite file handle after a one-shot cache lookup."""

        self._connection.close()

    def put(self, template: Template) -> None:
        """Upsert one generic template by its external reference."""

        payload = json.dumps(
            {
                "external_ref": template.external_ref,
                "spec_skeleton": _spec_payload(template.spec_skeleton),
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

        try:
            payload = json.loads(row[0])
            if not isinstance(payload, Mapping):
                return None
            stored_ref = payload["external_ref"]
            if stored_ref != external_ref:
                return None
            slot_names = _string_tuple(payload["slot_names"], "slot_names")
            specification = _decode_spec(payload["spec_skeleton"])
            return Template(external_ref, specification, slot_names)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def put_plan_spec(self, template: PlanSpecTemplate) -> None:
        """Upsert one generic Plan + Spec template by its external reference."""

        payload = json.dumps(
            {
                "case_shape": template.case_shape,
                "packet": template.packet.to_dict(),
                "profile": template.profile,
                "estimated_implementation_tokens": template.estimated_implementation_tokens,
                "packet_token_budget": template.packet_token_budget,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO plan_spec_templates (external_ref, payload)
                VALUES (?, ?)
                ON CONFLICT(external_ref) DO UPDATE SET payload = excluded.payload
                """,
                (template.external_ref, payload),
            )

    def get_plan_spec(self, external_ref: str) -> PlanSpecTemplate | None:
        """Return the generic Plan + Spec template for a reference, if stored."""

        row = self._connection.execute(
            "SELECT payload FROM plan_spec_templates WHERE external_ref = ?",
            (external_ref,),
        ).fetchone()
        if row is None:
            return None

        payload = json.loads(row[0])
        return PlanSpecTemplate(
            external_ref=external_ref,
            case_shape=payload["case_shape"],
            packet=PlanSpecPacket.from_dict(payload["packet"]),
            profile=payload["profile"],
            estimated_implementation_tokens=payload["estimated_implementation_tokens"],
            packet_token_budget=payload["packet_token_budget"],
        )

    def put_stage_plan_state(self, stage_key: str, payload: Mapping[str, object]) -> None:
        """Persist source-free RRC state for one rendered workflow stage."""

        if not isinstance(stage_key, str) or not stage_key:
            raise ValueError("stage_key must be a non-empty string")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO stage_plan_states (stage_key, payload)
                VALUES (?, ?)
                ON CONFLICT(stage_key) DO UPDATE SET payload = excluded.payload
                """,
                (stage_key, encoded),
            )

    def get_stage_plan_state(self, stage_key: str) -> dict[str, object] | None:
        """Return one source-free RRC stage record, or ``None`` on a miss."""

        row = self._connection.execute(
            "SELECT payload FROM stage_plan_states WHERE stage_key = ?", (stage_key,)
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None
        return dict(payload) if isinstance(payload, Mapping) else None


def _spec_payload(specification: Spec) -> dict[str, object]:
    slots = specification.slots
    return {
        "plan": specification.plan,
        "signature": specification.signature,
        "contract": specification.contract,
        "tests": list(specification.tests),
        "slots": {
            "entity": slots.entity,
            "identifiers": list(slots.identifiers),
            "types": list(slots.types),
            "fields": list(slots.fields),
            "constants": list(slots.constants),
            "edge_values": list(slots.edge_values),
            "values": dict(slots.values),
        },
    }


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{field_name} must be a list of strings")
    return tuple(cast(list[str], value))


def _decode_spec(value: object) -> Spec:
    if not isinstance(value, Mapping):
        raise TypeError("spec_skeleton must be an object")
    slots_value = value["slots"]
    if not isinstance(slots_value, Mapping):
        raise TypeError("slots must be an object")
    entity = slots_value["entity"]
    if entity is not None and not isinstance(entity, str):
        raise TypeError("slots.entity must be a string or null")
    raw_values = slots_value["values"]
    if not isinstance(raw_values, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in raw_values.items()
    ):
        raise TypeError("slots.values must map strings to strings")
    text_fields = (value["plan"], value["signature"], value["contract"])
    if any(not isinstance(item, str) for item in text_fields):
        raise TypeError("spec text fields must be strings")
    return Spec(
        plan=cast(str, text_fields[0]),
        signature=cast(str, text_fields[1]),
        contract=cast(str, text_fields[2]),
        tests=_string_tuple(value["tests"], "tests"),
        slots=Slots(
            entity=cast(str | None, entity),
            identifiers=_string_tuple(slots_value["identifiers"], "identifiers"),
            types=_string_tuple(slots_value["types"], "types"),
            fields=_string_tuple(slots_value["fields"], "fields"),
            constants=_string_tuple(slots_value["constants"], "constants"),
            edge_values=_string_tuple(slots_value["edge_values"], "edge_values"),
            values={cast(str, key): cast(str, item) for key, item in raw_values.items()},
        ),
    )
