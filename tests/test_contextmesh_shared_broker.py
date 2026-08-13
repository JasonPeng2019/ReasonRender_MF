from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest
from contextmesh.mcp.broker_service import ledger_payload, load_ledger
from contextmesh.mcp.file_brief import SUMMARY_FIELDS
from contextmesh.mcp.shared_broker import (
    MAX_PLAN_SCOPED_SOURCE_VIEW_BYTES,
    MAX_SOURCE_CHUNK_BYTES,
    BrokerError,
    SharedBriefBroker,
    SharedBrokerClient,
    SharedBrokerServer,
    SourceClaim,
)
from contextmesh.bench.rrc_long_spec_demo import materialize
from harness.four_worker_plan import build_overlap_ledger, freeze_worker_plans, manifest_sha256
from harness.staged_workload import staged_overlap_ledgers


def _entry(path: str):
    plans = freeze_worker_plans()
    return next(item for item in build_overlap_ledger(plans, manifest_sha256(plans)) if item.canonical_path == path)


def _brief(entry) -> dict[str, str]:
    facts = "; ".join(entry.required_facts)
    return {
        "purpose_and_api": "evaluate accepts value and returns it.",
        "data_and_dependencies": "No imports, collaborators, mutation, or side effects.",
        "behaviour_and_failures": "Direct return preserves the provided value.",
        "plan_step_facts": facts,
        "anchors": "evaluate@1-2",
    }


def _peer_summary(entry) -> list[str]:
    return list(_brief(entry).values())


def _source_body() -> str:
    return "\n".join(f"def evaluate_{index}(value): return value" for index in range(1, 31)) + "\n"


def _sources(root: Path) -> None:
    for path in (
        "ruleforge/domain.py",
        "ruleforge/evaluator.py",
        "ruleforge/registry.py",
        "ruleforge/rules/base.py",
    ):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_source_body(), encoding="utf-8")


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _commit_stage(root: Path, message: str) -> str:
    _git(root, "add", ".")
    _git(root, "-c", "user.name=ContextMesh test", "-c", "user.email=contextmesh@example.test", "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _stage_entry(entry, stage: str):
    return replace(entry, brief_id=f"{entry.brief_id}-{stage}")


def test_owner_publishes_once_and_peer_receives_only_a_brief(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    owner, peer_id = entry.source_owner, entry.peer_workers[0]
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, [entry], state_dir=tmp_path / "state")

    async def exercise():
        peer = asyncio.create_task(broker.get_file_brief(entry.brief_id, peer_id))
        await asyncio.sleep(0)
        claim = await broker.claim_source(entry.brief_id, owner)
        await broker.publish_file_brief(entry.brief_id, owner, claim.source_hash, _brief(entry))
        return claim, await peer

    claim, served = asyncio.run(exercise())

    assert claim.source_content.startswith("def evaluate")
    assert claim.brief_template["schema_version"] == "file-brief/v1"
    assert set(claim.brief_template["required_fields"]) == set(SUMMARY_FIELDS)
    assert claim.brief_template["max_summary_bytes"] < len(claim.source_content.encode("utf-8"))
    assert served == {"brief_id": entry.brief_id, "summary": _peer_summary(entry)}
    assert [row["event"] for row in broker.events].count("source_claim_raw") == 1
    assert [row["event"] for row in broker.events].count("brief_published") == 1
    assert [row["event"] for row in broker.events].count("brief_served") == 1
    assert len(list((tmp_path / "state").glob("*.json"))) == 1


def test_dispatch_prefetch_serves_only_an_already_valid_authorized_brief(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    owner, peer_id = entry.source_owner, entry.peer_workers[0]
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, [entry])

    async def exercise() -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
        before = await broker.get_ready_file_briefs([entry.brief_id], peer_id)
        claim = await broker.claim_source(entry.brief_id, owner)
        await broker.publish_file_brief(entry.brief_id, owner, claim.source_hash, _brief(entry))
        after = await broker.get_ready_file_briefs([entry.brief_id], peer_id)
        return before, after

    before, after = asyncio.run(exercise())

    assert before == {}
    assert after == {entry.brief_id: {"brief_id": entry.brief_id, "summary": _peer_summary(entry)}}
    assert [row["event"] for row in broker.events].count("brief_prefetched_dispatch") == 1
    assert not any(row["event"] == "brief_wait" for row in broker.events)


def test_dispatch_prefetch_can_project_only_worker_required_facts(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    owner, peer_id = entry.source_owner, entry.peer_workers[0]
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, [entry])

    async def exercise() -> dict[str, dict[str, object]]:
        claim = await broker.claim_source(entry.brief_id, owner)
        await broker.publish_file_brief(entry.brief_id, owner, claim.source_hash, _brief(entry))
        return await broker.get_ready_worker_briefs(
            [{"brief_id": entry.brief_id, "required_facts": [entry.required_facts[0]]}], peer_id
        )

    assert asyncio.run(exercise()) == {
        entry.brief_id: {"brief_id": entry.brief_id, "facts": [entry.required_facts[0]]}
    }
    assert [row["event"] for row in broker.events].count("brief_prefetched_worker_projection") == 1


def test_owner_repairs_a_malformed_brief_without_a_second_raw_claim(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    owner, peer_id = entry.source_owner, entry.peer_workers[0]
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, [entry])

    async def exercise() -> None:
        with pytest.raises(BrokerError, match="cannot claim raw") as raw:
            await broker.claim_source(entry.brief_id, peer_id)
        assert raw.value.code == "not_source_owner"
        claim = await broker.claim_source(entry.brief_id, owner)
        peer = asyncio.create_task(broker.get_file_brief(entry.brief_id, peer_id))
        with pytest.raises(BrokerError, match="exactly the compact summary fields") as bad:
            await broker.publish_file_brief(entry.brief_id, owner, claim.source_hash, {})
        assert bad.value.code == "brief_incomplete"
        with pytest.raises(BrokerError, match="already claimed"):
            await broker.claim_source(entry.brief_id, owner)
        await broker.publish_file_brief(entry.brief_id, owner, claim.source_hash, _brief(entry))
        assert await peer == {"brief_id": entry.brief_id, "summary": _peer_summary(entry)}

    asyncio.run(exercise())
    assert [row["event"] for row in broker.events].count("brief_repair_requested") == 1


def test_owner_publish_error_preserves_the_exact_validation_reason(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    owner = entry.source_owner
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, [entry])

    async def exercise() -> None:
        claim = await broker.claim_source(entry.brief_id, owner)
        incomplete = _brief(entry)
        incomplete["plan_step_facts"] = "No source contract is present."
        with pytest.raises(BrokerError, match="missing required_source_fact") as missing:
            await broker.publish_file_brief(entry.brief_id, owner, claim.source_hash, incomplete)
        assert missing.value.code == "brief_incomplete"
        assert "missing required_source_fact" in str(missing.value)
        oversized = _brief(entry)
        oversized["anchors"] = "x" * 10_000
        with pytest.raises(BrokerError, match="byte content budget") as budget:
            await broker.publish_file_brief(entry.brief_id, owner, claim.source_hash, oversized)
        assert budget.value.code == "brief_incomplete"
        assert "byte content budget" in str(budget.value)

    asyncio.run(exercise())
    assert [row["event"] for row in broker.events].count("brief_repair_requested") == 2


def test_owner_claim_reconciles_one_unambiguous_copied_id(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, [entry])
    supplied_id = f"{entry.brief_id[:-1]}0"
    assert supplied_id != entry.brief_id

    claim = asyncio.run(broker.claim_source(supplied_id, entry.source_owner))

    assert claim.brief_id == entry.brief_id
    assert broker.events[0] == {
        "event": "owner_claim_id_reconciled",
        "ts": broker.events[0]["ts"],
        "brief_id": entry.brief_id,
        "supplied_brief_id": supplied_id,
        "owner_id": entry.source_owner,
    }


def test_owner_claim_rejects_an_ambiguous_copied_id(tmp_path: Path) -> None:
    plans = freeze_worker_plans()
    entries = tuple(
        entry for entry in build_overlap_ledger(plans, manifest_sha256(plans)) if entry.source_owner == "worker-03"
    )
    assert len(entries) == 2
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, entries)

    async def exercise() -> None:
        with pytest.raises(BrokerError, match="unknown brief_id") as error:
            await broker.claim_source("brief-typo", "worker-03")
        assert error.value.code == "unknown_brief"

    asyncio.run(exercise())


def test_owner_publish_and_peer_read_reconcile_one_typo_id(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, [entry])
    owner_typo = f"{entry.brief_id[:-1]}0"
    peer_typo = entry.brief_id[:-1]
    assert owner_typo != entry.brief_id

    async def exercise() -> dict[str, object]:
        claim = await broker.claim_source(entry.brief_id, entry.source_owner)
        await broker.publish_file_brief(owner_typo, entry.source_owner, claim.source_hash, _brief(entry))
        return await broker.get_file_brief(peer_typo, entry.peer_workers[0])

    assert asyncio.run(exercise()) == {"brief_id": entry.brief_id, "summary": _peer_summary(entry)}


def test_owner_publication_normalizes_a_retyped_claim_hash(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, [entry])

    async def exercise() -> dict[str, object]:
        claim = await broker.claim_source(entry.brief_id, entry.source_owner)
        retyped_hash = "0" * 64 if claim.source_hash != "0" * 64 else "1" * 64
        await broker.publish_file_brief(entry.brief_id, entry.source_owner, retyped_hash, _brief(entry))
        return await broker.get_file_brief(entry.brief_id, entry.peer_workers[0])

    assert asyncio.run(exercise()) == {"brief_id": entry.brief_id, "summary": _peer_summary(entry)}
    assert any(row["event"] == "source_hash_normalized" for row in broker.events)
    assert [row["event"] for row in broker.events] == [
        "source_claim_raw",
        "source_hash_normalized",
        "brief_published",
        "brief_served",
    ]


def test_broker_rehydrates_a_valid_persisted_brief_without_a_second_raw_read(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    _sources(tmp_path)
    state_dir = tmp_path / "state"
    first = SharedBriefBroker(tmp_path, [entry], state_dir=state_dir)

    async def publish() -> None:
        claim = await first.claim_source(entry.brief_id, entry.source_owner)
        await first.publish_file_brief(entry.brief_id, entry.source_owner, claim.source_hash, _brief(entry))

    asyncio.run(publish())
    resumed = SharedBriefBroker(tmp_path, [entry], state_dir=state_dir)

    served = asyncio.run(resumed.get_file_brief(entry.brief_id, entry.peer_workers[0]))

    assert served == {"brief_id": entry.brief_id, "summary": _peer_summary(entry)}
    assert resumed.events == [
        {
            "event": "brief_rehydrated",
            "ts": resumed.events[0]["ts"],
            "brief_id": entry.brief_id,
            "source_hash": resumed.events[0]["source_hash"],
            "state_path": resumed.events[0]["state_path"],
        },
        {
            "event": "brief_served",
            "ts": resumed.events[1]["ts"],
            "brief_id": entry.brief_id,
            "peer_id": entry.peer_workers[0],
            "waited_ms": 0,
        },
    ]


def test_staged_broker_reuses_unchanged_brief_and_refreshes_changed_path_from_git_diff(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    owner, peer = entry.source_owner, entry.peer_workers[0]
    _git(tmp_path, "init")
    _sources(tmp_path)
    first_commit = _commit_stage(tmp_path, "stage one")
    state_dir = tmp_path / "state"
    first = SharedBriefBroker(
        tmp_path,
        [_stage_entry(entry, "stage-01")],
        state_dir=state_dir,
        workflow_id="ruleforge-staged-test",
        stage_id="stage-01",
        stage_commit=first_commit,
        git_root=tmp_path,
    )
    first_entry = _stage_entry(entry, "stage-01")

    async def publish_first() -> None:
        claim = await first.claim_source(first_entry.brief_id, owner)
        assert claim.claim_kind == "raw"
        await first.publish_file_brief(first_entry.brief_id, owner, claim.source_hash, _brief(first_entry))

    asyncio.run(publish_first())

    target = tmp_path / entry.canonical_path
    target.write_text(_source_body() + "# stage two change\n", encoding="utf-8")
    second_commit = _commit_stage(tmp_path, "stage two")
    second_entry = replace(
        _stage_entry(entry, "stage-02"),
        requirements_hash="stage-02-different-plan-contract",
        plan_steps=((owner, ("refresh the stage-two source contract",)), (peer, ("consume the refreshed contract",))),
        required_facts=(*entry.required_facts, "stage two change"),
    )
    second = SharedBriefBroker(
        tmp_path,
        [second_entry],
        state_dir=state_dir,
        workflow_id="ruleforge-staged-test",
        stage_id="stage-02",
        stage_commit=second_commit,
        parent_stage_commit=first_commit,
        git_root=tmp_path,
    )

    async def refresh_second() -> dict[str, object]:
        claim = await second.claim_source(second_entry.brief_id, owner)
        assert claim.claim_kind == "diff_refresh"
        assert claim.source_content == ""
        assert claim.prior_summary == _brief(first_entry)
        assert claim.git_diff is not None and "stage two change" in claim.git_diff
        await second.publish_file_brief(second_entry.brief_id, owner, claim.source_hash, _brief(second_entry))
        return await second.get_file_brief(second_entry.brief_id, peer)

    assert asyncio.run(refresh_second()) == {"brief_id": second_entry.brief_id, "summary": _peer_summary(second_entry)}
    assert not any(row["event"] == "source_claim_raw" for row in second.events)
    assert [row["event"] for row in second.events].count("brief_refresh_diff") == 1

    resumed_second = SharedBriefBroker(
        tmp_path,
        [second_entry],
        state_dir=state_dir,
        workflow_id="ruleforge-staged-test",
        stage_id="stage-02",
        stage_commit=second_commit,
        parent_stage_commit=first_commit,
        git_root=tmp_path,
    )
    assert asyncio.run(resumed_second.get_file_brief(second_entry.brief_id, peer)) == {
        "brief_id": second_entry.brief_id,
        "summary": _peer_summary(second_entry),
    }
    assert [row["event"] for row in resumed_second.events].count("brief_rehydrated_stage") == 1

    third_entry = replace(
        _stage_entry(entry, "stage-03"),
        requirements_hash="stage-03-different-plan-contract",
        plan_steps=((owner, ("use the retained stage-three contract",)), (peer, ("implement the distinct stage-three task",))),
        required_facts=second_entry.required_facts,
    )
    third = SharedBriefBroker(
        tmp_path,
        [third_entry],
        state_dir=state_dir,
        workflow_id="ruleforge-staged-test",
        stage_id="stage-03",
        stage_commit=second_commit,
        parent_stage_commit=second_commit,
        git_root=tmp_path,
    )

    served = asyncio.run(third.get_file_brief(third_entry.brief_id, peer))
    reused = asyncio.run(third.claim_source(third_entry.brief_id, owner))

    assert served == {"brief_id": third_entry.brief_id, "summary": _peer_summary(third_entry)}
    assert reused.claim_kind == "unchanged_reuse"
    assert reused.source_content == ""
    assert reused.reused_summary == _brief(third_entry)
    assert any(row["event"] == "brief_served_owner_reuse" and row["owner_id"] == owner for row in third.events)
    assert third.events == [
        {
            "event": "brief_reused_unchanged",
            "ts": third.events[0]["ts"],
            "brief_id": third_entry.brief_id,
            "workflow_id": "ruleforge-staged-test",
            "stage_id": "stage-03",
            "source_hash": third.events[0]["source_hash"],
            "parent_source_hash": third.events[0]["parent_source_hash"],
        },
        {
            "event": "brief_served",
            "ts": third.events[1]["ts"],
            "brief_id": third_entry.brief_id,
            "peer_id": peer,
            "waited_ms": 0,
        },
        {
            "event": "brief_served_owner_reuse",
            "ts": third.events[2]["ts"],
            "brief_id": third_entry.brief_id,
            "owner_id": owner,
            "waited_ms": 0,
        },
    ]


def test_staged_broker_records_explicit_invalidation_before_one_raw_owner_view(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    owner = entry.source_owner
    _git(tmp_path, "init")
    _sources(tmp_path)
    first_commit = _commit_stage(tmp_path, "stage one")
    state_dir = tmp_path / "state"
    first_entry = _stage_entry(entry, "stage-01")
    first = SharedBriefBroker(
        tmp_path,
        [first_entry],
        state_dir=state_dir,
        workflow_id="ruleforge-staged-invalidation",
        stage_id="stage-01",
        stage_commit=first_commit,
        git_root=tmp_path,
    )

    async def publish_first() -> None:
        claim = await first.claim_source(first_entry.brief_id, owner)
        await first.publish_file_brief(first_entry.brief_id, owner, claim.source_hash, _brief(first_entry))

    asyncio.run(publish_first())
    target = tmp_path / entry.canonical_path
    target.write_text(_source_body() + "# changed without usable diff\n", encoding="utf-8")
    second_commit = _commit_stage(tmp_path, "stage two")
    second_entry = _stage_entry(entry, "stage-02")
    second = SharedBriefBroker(
        tmp_path,
        [second_entry],
        state_dir=state_dir,
        workflow_id="ruleforge-staged-invalidation",
        stage_id="stage-02",
        stage_commit=second_commit,
        parent_stage_commit=first_commit,
        git_root=tmp_path / "not-a-git-root",
    )

    claim = asyncio.run(second.claim_source(second_entry.brief_id, owner))

    assert claim.claim_kind == "invalidated_raw"
    assert claim.invalidation_reason == "git_diff_unavailable"
    assert claim.source_content.startswith("def evaluate")
    assert [row["event"] for row in second.events].count("brief_refresh_invalidated") == 1
    assert [row["event"] for row in second.events].count("source_claim_raw") == 1


def test_staged_recovery_skips_a_newer_incompatible_lineage_record(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    owner, peer = entry.source_owner, entry.peer_workers[0]
    _git(tmp_path, "init")
    _sources(tmp_path)
    commit = _commit_stage(tmp_path, "seed")
    state_dir = tmp_path / "state"
    valid = _stage_entry(entry, "stage-valid")
    first = SharedBriefBroker(
        tmp_path, [valid], state_dir=state_dir, workflow_id="lineage-repair", stage_id="stage-valid", stage_commit=commit, git_root=tmp_path
    )
    bad = replace(_stage_entry(entry, "stage-bad"), requirements_hash="bad-contract", required_facts=("poison contract",))
    poison = SharedBriefBroker(
        tmp_path, [bad], state_dir=state_dir, workflow_id="lineage-repair", stage_id="stage-bad", stage_commit=commit, git_root=tmp_path
    )

    async def publish(broker: SharedBriefBroker, staged_entry) -> None:
        claim = await broker.claim_source(staged_entry.brief_id, owner)
        await broker.publish_file_brief(staged_entry.brief_id, owner, claim.source_hash, _brief(staged_entry))

    asyncio.run(publish(first, valid))
    asyncio.run(publish(poison, bad))
    recovered = SharedBriefBroker(
        tmp_path, [_stage_entry(entry, "stage-recovered")], state_dir=state_dir,
        workflow_id="lineage-repair", stage_id="stage-recovered", stage_commit=commit, git_root=tmp_path,
    )
    current = _stage_entry(entry, "stage-recovered")

    assert asyncio.run(recovered.get_file_brief(current.brief_id, peer)) == {
        "brief_id": current.brief_id, "summary": _peer_summary(current)
    }
    assert asyncio.run(recovered.claim_source(current.brief_id, owner)).claim_kind == "unchanged_reuse"


def test_live_broker_installs_the_next_stage_without_losing_lineage(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    owner = entry.source_owner
    _git(tmp_path, "init")
    _sources(tmp_path)
    first_commit = _commit_stage(tmp_path, "stage one")
    state_dir = tmp_path / "state"
    first_entry = _stage_entry(entry, "stage-01")
    broker = SharedBriefBroker(
        tmp_path,
        [first_entry],
        state_dir=state_dir,
        workflow_id="ruleforge-live-stage-test",
        stage_id="stage-01",
        stage_commit=first_commit,
        git_root=tmp_path,
    )

    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        server = SharedBrokerServer(broker, control_token="local-stage-control")
        host, port = await server.start()
        client = SharedBrokerClient(host, port)
        try:
            first_claim = await client.claim_source(first_entry.brief_id, owner)
            await client.publish_file_brief(first_entry.brief_id, owner, first_claim["source_hash"], _brief(first_entry))
            target = tmp_path / entry.canonical_path
            target.write_text(_source_body() + "# stage two change\n", encoding="utf-8")
            second_commit = _commit_stage(tmp_path, "stage two")
            second_entry = _stage_entry(entry, "stage-02")
            ledger = tmp_path / "stage-02-ledger.json"
            ledger.write_text(json.dumps(ledger_payload([second_entry])), encoding="utf-8")
            installed = await client.install_stage(
                control_token="local-stage-control",
                ledger_path=str(ledger),
                stage_id="stage-02",
                stage_commit=second_commit,
                parent_stage_commit=first_commit,
            )
            return installed, await client.claim_source(second_entry.brief_id, owner)
        finally:
            await server.close()

    installed, refreshed = asyncio.run(exercise())

    assert installed == {"installed": True, "stage_id": "stage-02"}
    assert refreshed["claim_kind"] == "diff_refresh"
    assert refreshed["source_content"] == ""
    assert refreshed["git_diff"] is not None
    assert "stage_installed" in [row["event"] for row in broker.events]


def test_large_owner_source_is_delivered_in_ordered_chunks_before_publication(tmp_path: Path) -> None:
    entry = _entry("ruleforge/evaluator.py")
    owner = entry.source_owner
    source = "\n".join(f"rule_{index} = {'x' * 80}" for index in range(MAX_SOURCE_CHUNK_BYTES // 90 + 100)) + "\n"
    assert len(source.encode("utf-8")) > MAX_SOURCE_CHUNK_BYTES
    target = tmp_path / entry.canonical_path
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    broker = SharedBriefBroker(tmp_path, [entry])

    async def exercise() -> tuple[SourceClaim, list[dict[str, Any]]]:
        claim = await broker.claim_source(entry.brief_id, owner)
        with pytest.raises(BrokerError, match="must read all source chunks"):
            await broker.publish_file_brief(entry.brief_id, owner, claim.source_hash, _brief(entry))
        chunks = [
            await broker.read_source_chunk(entry.brief_id, owner, index)
            for index in range(1, claim.source_chunk_count)
        ]
        await broker.publish_file_brief(entry.brief_id, owner, claim.source_hash, _brief(entry))
        return claim, chunks

    claim, chunks = asyncio.run(exercise())

    assert claim.source_chunk_count > 1
    assert claim.source_content + "".join(chunk["source_content"] for chunk in chunks) == source
    assert [row["event"] for row in broker.events].count("source_claim_raw") == 1
    assert [row["event"] for row in broker.events].count("source_chunk_raw") == claim.source_chunk_count - 1


def test_large_catalog_owner_receives_one_plan_scoped_raw_excerpt(tmp_path: Path) -> None:
    entry = _entry("ruleforge/policy_catalog.py")
    target = tmp_path / entry.canonical_path
    target.parent.mkdir(parents=True)
    required = "\n".join(f"{fact.split(' -> ', 1)[0]} = PolicyProfile(...)" for fact in entry.required_facts)
    source = "header_api = object()\n" + required + "\n" + ("irrelevant_generated_entry = 0\n" * 8_000) + "def profile(key): return key\n"
    target.write_text(source, encoding="utf-8")
    broker = SharedBriefBroker(tmp_path, [entry])

    async def exercise() -> SourceClaim:
        claim = await broker.claim_source(entry.brief_id, entry.source_owner)
        await broker.publish_file_brief(entry.brief_id, entry.source_owner, claim.source_hash, _brief(entry))
        return claim

    claim = asyncio.run(exercise())

    assert claim.source_chunk_count == 1
    assert len(claim.source_content.encode("utf-8")) <= MAX_PLAN_SCOPED_SOURCE_VIEW_BYTES
    assert "header_api" in claim.source_content
    assert "def profile" in claim.source_content
    assert all(fact.split(" -> ", 1)[0] in claim.source_content for fact in entry.required_facts)
    event = next(row for row in broker.events if row["event"] == "source_claim_raw")
    assert event["owner_view_kind"] == "plan_scoped_excerpt"


def test_operational_catalog_excerpt_stays_within_transport_limit(tmp_path: Path) -> None:
    """The real appended-stage catalog selectors must not trigger full-file MCP transport."""

    workspace = tmp_path / "workspace"
    materialize(workspace, "operational-01", "stage-04")
    entry = next(item for item in staged_overlap_ledgers()[-1] if item.canonical_path == "ruleforge/policy_catalog.py")
    source = (workspace / "workspace" / entry.canonical_path).read_text(encoding="utf-8")

    view, kind = SharedBriefBroker._owner_source_view(entry, source)

    assert kind == "plan_scoped_excerpt"
    assert len(view.encode("utf-8")) <= MAX_PLAN_SCOPED_SOURCE_VIEW_BYTES
    assert all(fact.split(" -> ", 1)[0] in view for fact in entry.required_facts)


def test_four_independent_clients_share_one_network_broker_and_unrelated_claims_progress(tmp_path: Path) -> None:
    payments = _entry("ruleforge/evaluator.py")
    registry = _entry("ruleforge/registry.py")
    payment_owner, payment_peer = payments.source_owner, payments.peer_workers[0]
    registry_owner, registry_peer = registry.source_owner, registry.peer_workers[0]
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, [payments, registry])

    async def exercise() -> None:
        server = SharedBrokerServer(broker)
        host, port = await server.start()
        one, two, three, four = (SharedBrokerClient(host, port) for _ in range(4))
        try:
            payment_claim, registry_claim = await asyncio.gather(
                one.claim_source(payments.brief_id, payment_owner),
                three.claim_source(registry.brief_id, registry_owner),
            )
            payment_wait = asyncio.create_task(two.get_file_brief(payments.brief_id, payment_peer))
            registry_wait = asyncio.create_task(four.get_file_brief(registry.brief_id, registry_peer))
            await asyncio.gather(
                one.publish_file_brief(payments.brief_id, payment_owner, payment_claim["source_hash"], _brief(payments)),
                three.publish_file_brief(registry.brief_id, registry_owner, registry_claim["source_hash"], _brief(registry)),
            )
            await asyncio.gather(payment_wait, registry_wait)
        finally:
            await server.close()

    asyncio.run(exercise())
    raw_events = [row for row in broker.events if row["event"] == "source_claim_raw"]
    assert {row["brief_id"] for row in raw_events} == {payments.brief_id, registry.brief_id}
    assert not any(row["event"] == "read_raw" for row in broker.events)


def test_peer_retrieves_multiple_declared_briefs_in_one_broker_call(tmp_path: Path) -> None:
    plans = freeze_worker_plans()
    entries = tuple(
        entry
        for entry in build_overlap_ledger(plans, manifest_sha256(plans))
        if entry.canonical_path in {"ruleforge/domain.py", "ruleforge/registry.py", "ruleforge/rules/base.py"}
    )
    _sources(tmp_path)
    broker = SharedBriefBroker(tmp_path, entries)

    async def exercise() -> dict[str, dict[str, object]]:
        waiting = asyncio.create_task(broker.get_file_briefs([entry.brief_id for entry in entries], "worker-04"))
        for entry in entries:
            claim = await broker.claim_source(entry.brief_id, entry.source_owner)
            await broker.publish_file_brief(entry.brief_id, entry.source_owner, claim.source_hash, _brief(entry))
        return await waiting

    replies = asyncio.run(exercise())

    assert set(replies) == {entry.brief_id for entry in entries}
    assert all(reply["summary"] == _peer_summary(entry) for entry, reply in zip(entries, replies.values(), strict=True))
    assert [row["event"] for row in broker.events].count("brief_served") == 3


def test_service_ledger_round_trip_preserves_bound_entries(tmp_path: Path) -> None:
    entries = (_entry("ruleforge/evaluator.py"), _entry("ruleforge/registry.py"))
    path = tmp_path / "ledger.json"
    path.write_text(__import__("json").dumps(ledger_payload(entries)), encoding="utf-8")

    assert load_ledger(path) == entries
