"""Perform the local EverOS-backed RRCv2 cache lookup used by the Full arm."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from contextmesh.bench.rrc_long_spec_demo import CASE_SHAPE, ORACLE_TESTS
from rrc.everos import EverOSClient
from rrc.orchestrator_contract import OrchestratorTask, PlanSpecPacket, PlanSpecTemplate
from rrc.orchestrator_policy import POLICY
from rrc.orchestrator_runtime import OrchestratorRuntime
from rrc.store import SQLiteTemplateStore


class RRCManifestError(ValueError):
    """Raised when the local generic cache cannot support a real cache lookup."""


def _load_cache(workspace: Path) -> tuple[PlanSpecPacket, Mapping[str, Mapping[str, object]], bytes, bytes]:
    cache = workspace / ".rrc-cache"
    template_path = cache / "template.json"
    bindings_path = cache / "bindings.json"
    try:
        template_bytes = template_path.read_bytes()
        bindings_bytes = bindings_path.read_bytes()
        template = json.loads(template_bytes)
        raw_bindings = json.loads(bindings_bytes).get("bindings")
    except (FileNotFoundError, AttributeError, json.JSONDecodeError) as error:
        raise RRCManifestError(f"invalid RRC cache files: {error}") from error
    if not isinstance(template, Mapping) or not isinstance(raw_bindings, Mapping):
        raise RRCManifestError("invalid RRC cache shape")
    bindings = {
        task_id: values
        for task_id, values in raw_bindings.items()
        if isinstance(task_id, str) and isinstance(values, Mapping)
    }
    if len(bindings) != len(raw_bindings):
        raise RRCManifestError("RRC bindings must map task ids to slot objects")
    try:
        return PlanSpecPacket.from_dict(template), bindings, template_bytes, bindings_bytes
    except (TypeError, ValueError) as error:
        raise RRCManifestError(f"invalid generic RRC packet: {error}") from error


def _task(task_id: str, values: Mapping[str, object]) -> OrchestratorTask:
    return OrchestratorTask(
        task_id=task_id,
        family="ruleforge-policy",
        case_shape=CASE_SHAPE,
        slot_values=values,
        oracle_tests=ORACLE_TESTS,
    )


def _unreachable_model(_prompt: str, _model: str) -> tuple[str, int]:
    raise AssertionError("a cache lookup must not invoke a planner or worker model")


def _start_local_everos(repo_root: Path) -> None:
    """Start the repository-owned local-only EverOS service if health is absent."""

    if os.name == "nt":
        translated = subprocess.run(
            ["wsl.exe", "wslpath", "-a", str(repo_root)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        if translated.returncode != 0 or not translated.stdout.strip():
            raise RRCManifestError("could not resolve the repository path inside WSL for local EverOS")
        command = [
            "wsl.exe",
            "--",
            "bash",
            "-lc",
            f"cd {shlex.quote(translated.stdout.strip())} && ./contextmesh/scripts/start_stack.sh",
        ]
    else:
        command = ["bash", str(repo_root / "contextmesh" / "scripts" / "start_stack.sh")]
    started = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=130, check=False)
    if started.returncode != 0:
        raise RRCManifestError("local EverOS startup failed: " + started.stdout[-1000:])


def _rrc_project_id(cache_key: str) -> str:
    """Return the deterministic local EverOS scope for one generic template."""

    return f"rrc-template-index-{cache_key}"


def _local_everos(repo_root: Path, base_url: str, cache_key: str) -> EverOSClient:
    # The process is persistent across harness recoveries.  Scope the index by
    # immutable template identity so stale refs from unrelated historical runs
    # cannot hide a newly prewarmed generic plan behind EverOS's top-k bound.
    client = EverOSClient(base_url, timeout=10, project_id=_rrc_project_id(cache_key))
    try:
        client.wait_for_index(timeout=5, poll_interval=0.2)
    except (OSError, TimeoutError, ValueError):
        _start_local_everos(repo_root)
        try:
            client.wait_for_index(timeout=45, poll_interval=0.25)
        except (OSError, TimeoutError, ValueError) as error:
            raise RRCManifestError(f"local EverOS did not become ready: {error}") from error
    return client


def _cache_identity(template_bytes: bytes) -> tuple[str, str]:
    """Identify the reusable coordinator template, never a stage binding.

    Bindings are deliberately dynamic: Terra renders them into four distinct
    worker plans after a generic RRC HIT.  Including them in this key turned a
    normal later-stage binding change into a false cache miss and forced the
    local EverOS lookup to search for a needless new template reference.
    """

    cache_key = hashlib.sha256(template_bytes).hexdigest()
    return cache_key, "rrcv2-" + cache_key


def _warm_record_path(root: Path) -> Path:
    return root / ".rrc-cache" / "rrc-warm.json"


def _load_warm_record(root: Path, cache_key: str, external_ref: str) -> tuple[dict[str, Any], str]:
    """Require a retained cache population record from before the measured HIT."""

    path = _warm_record_path(root)
    try:
        raw = path.read_bytes()
        record = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise RRCManifestError(f"RRC cache is not prewarmed: {error}") from error
    if not isinstance(record, dict) or (
        record.get("schema_version") != 1
        or record.get("event") != "rrc_cache_warm"
        or record.get("cache_key") != cache_key
        or record.get("template_external_ref") != external_ref
    ):
        raise RRCManifestError("RRC cache warm record does not match this frozen cache")
    return record, hashlib.sha256(raw).hexdigest()


def warm_rrc_cache(
    workspace: str | Path,
    output: str | Path | None = None,
    *,
    client: EverOSClient | None = None,
) -> dict[str, Any]:
    """Populate and verify the generic RRC cache before the measured Terra starts."""

    root = Path(workspace).resolve()
    packet, bindings, template_bytes, bindings_bytes = _load_cache(root)
    task_ids = tuple(bindings)
    if len(task_ids) != 4:
        raise RRCManifestError("RRC cache warm requires exactly four task bindings")
    first = _task(task_ids[0], bindings[task_ids[0]])
    decision = POLICY.decide(first)
    cache_key, external_ref = _cache_identity(template_bytes)
    template = PlanSpecTemplate(
        external_ref=external_ref,
        case_shape=CASE_SHAPE,
        packet=packet,
        profile=decision.profile,
        estimated_implementation_tokens=decision.estimated_implementation_tokens,
        packet_token_budget=decision.packet_token_budget,
    )
    store = SQLiteTemplateStore(root / ".rrc-cache" / "plan-spec.sqlite")
    try:
        store.put_plan_spec(template)
        everos = client or _local_everos(
            Path(__file__).resolve().parents[1],
            os.environ.get("CONTEXTMESH_EVEROS_URL", "http://127.0.0.1:8000"),
            cache_key,
        )
        # The content is only CASE_SHAPE; no source body or binding is ever indexed.
        everos.index(CASE_SHAPE, external_ref)
        everos.wait_for_index(timeout=45, poll_interval=0.25)
        runtime = OrchestratorRuntime(_unreachable_model, "unused", _unreachable_model, "unused", store, everos)
        verified = runtime.lookup_cached_template(first)
        if verified is None or verified[0].external_ref != external_ref:
            raise RRCManifestError("local EverOS/RRC warm lookup did not find the stored template")
    finally:
        store.close()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "event": "rrc_cache_warm",
        "cache_key": cache_key,
        "template_external_ref": external_ref,
        "template_sha256": hashlib.sha256(template_bytes).hexdigest(),
        "bindings_sha256": hashlib.sha256(bindings_bytes).hexdigest(),
        "task_ids": list(task_ids),
        "backend": "local_everos+rrc_runtime",
    }
    destination = Path(output) if output is not None else _warm_record_path(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if destination.resolve() != _warm_record_path(root).resolve():
        _warm_record_path(root).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def build_rrc_hit(
    workspace: str | Path,
    output: str | Path,
    *,
    client: EverOSClient | None = None,
) -> dict[str, Any]:
    """Retrieve one genuinely prewarmed generic template through EverOS + RRC."""

    root = Path(workspace).resolve()
    packet, bindings, template_bytes, bindings_bytes = _load_cache(root)
    task_ids = tuple(bindings)
    if len(task_ids) != 4:
        raise RRCManifestError("RRC HIT requires exactly four task bindings")
    cache_key, external_ref = _cache_identity(template_bytes)
    _load_warm_record(root, cache_key, external_ref)
    store = SQLiteTemplateStore(root / ".rrc-cache" / "plan-spec.sqlite")
    try:
        if store.get_plan_spec(external_ref) is None:
            raise RRCManifestError("RRC cache is not prewarmed in its local template store")
        everos = client or _local_everos(
            Path(__file__).resolve().parents[1],
            os.environ.get("CONTEXTMESH_EVEROS_URL", "http://127.0.0.1:8000"),
            cache_key,
        )
        runtime = OrchestratorRuntime(_unreachable_model, "unused", _unreachable_model, "unused", store, everos)
        matches: list[dict[str, object]] = []
        for task_id in task_ids:
            found = runtime.lookup_cached_template(_task(task_id, bindings[task_id]))
            if found is None:
                raise RRCManifestError(f"local EverOS/RRC lookup missed {task_id} after indexing")
            found_template, score = found
            if found_template.external_ref != external_ref:
                raise RRCManifestError(f"local EverOS/RRC lookup selected an unexpected template for {task_id}")
            matches.append({"task_id": task_id, "external_ref": external_ref, "score": score})
    finally:
        store.close()
    warm_record, warm_sha256 = _load_warm_record(root, cache_key, external_ref)
    payload: dict[str, Any] = {
        "schema_version": 2,
        "event": "rrc_hit",
        "cache_key": cache_key,
        "template_external_ref": external_ref,
        "template_sha256": hashlib.sha256(template_bytes).hexdigest(),
        "bindings_sha256": hashlib.sha256(bindings_bytes).hexdigest(),
        "task_ids": list(task_ids),
        "cache_warm": {
            "event": warm_record["event"],
            "record_sha256": warm_sha256,
            "backend": warm_record["backend"],
        },
        "lookup": {"backend": "local_everos+rrc_runtime", "matches": matches},
    }
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="prove and retain one local EverOS-backed RRC cache hit")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        build_rrc_hit(args.workspace, args.output)
    except (OSError, RRCManifestError, subprocess.SubprocessError, ValueError) as error:
        print(f"rrc hit error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the headless Terra command.
    raise SystemExit(main())
