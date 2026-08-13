from __future__ import annotations

import json

import harness.staged_driver as staged_driver


def test_launch_uses_a_sibling_driver_directory_without_precreating_the_cohort(tmp_path, monkeypatch) -> None:
    class Process:
        pid = 123

    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(staged_driver.subprocess, "Popen", fake_popen)
    payload = staged_driver.launch_detached(tmp_path, "round-1", tmp_path / "metrics")

    assert payload["pid"] == 123
    assert (tmp_path / "metrics" / "round-1-staged-driver" / "launch.json").is_file()
    assert not (tmp_path / "metrics" / "round-1" / "staged-codex").exists()
    assert "_controller" in captured["command"]


def test_artifact_snapshot_uses_retained_files_only(tmp_path) -> None:
    root = tmp_path / "metrics" / "round-1" / "staged-codex"
    worker = root / "raw" / "stages" / "stage-01" / "workers" / "worker-01"
    worker.mkdir(parents=True)
    (root / "run-plan.json").write_text("{}\n", encoding="utf-8")
    (worker / "stream.jsonl").write_text("{}\n", encoding="utf-8")
    (worker / "attempts").mkdir()
    (worker / "attempts" / "001.json").write_text("{}\n", encoding="utf-8")
    (root / "report-stage-35-onward.json").write_text("{}\n", encoding="utf-8")

    snapshot = staged_driver.artifact_snapshot(tmp_path / "metrics", "round-1")

    assert snapshot["prepared"] is True
    assert snapshot["report"] is True
    assert snapshot["arms"]["raw"]["stages"] == [{
        "stage_id": "stage-01", "result": False, "terra_stream": False, "worker_streams": 1, "attempt_records": 1,
    }]
    assert json.dumps(snapshot)
