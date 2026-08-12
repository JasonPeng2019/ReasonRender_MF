from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "contextmesh/scripts/rrd_verify_guard.sh"

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "darwin", reason="M0 write guard is the macOS Seatbelt backend"
    ),
    pytest.mark.skipif(
        os.environ.get("RRD_VERIFY_GUARD_ACTIVE") == "1",
        reason="the guard sentinel self-test is intentionally the sole unwrapped verification command",
    ),
]


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "operator-home")
    env.pop("CODEX_HOME", None)
    (tmp_path / "operator-home/.codex").mkdir(parents=True)
    (tmp_path / "operator-home/.claude").mkdir()
    (tmp_path / "operator-home/.ssh").mkdir()
    (tmp_path / "operator-home/.aws").mkdir()
    (tmp_path / "operator-home/.config/gcloud").mkdir(parents=True)
    return env


def test_guard_proves_class_b_denial_then_runs_inner(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    marker = tmp_path / "inner-ran"
    result = subprocess.run(
        [
            GUARD,
            "--evidence-dir",
            evidence,
            "--",
            "/bin/sh",
            "-c",
            'printf yes >"$1"',
            "inner",
            marker,
        ],
        cwd=REPO,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text() == "yes"
    rows = list(evidence.glob("guard-*.json"))
    assert len(rows) == 1
    row = json.loads(rows[0].read_text())
    assert row["backend"] == "sandbox-exec"
    assert row["inner_started"] is True
    assert row["model_bearing"] is False
    assert row["probe_count"] >= 7
    assert row["status"] == 0
    assert rows[0].stat().st_mode & 0o777 == 0o600


def test_guarded_inner_cannot_write_repo_class_b_path(tmp_path: Path) -> None:
    target = REPO / ".codex/rrcv2-inner-write-probe"
    target.unlink(missing_ok=True)
    result = subprocess.run(
        [
            GUARD,
            "--evidence-dir",
            tmp_path / "evidence",
            "--",
            "/bin/sh",
            "-c",
            'printf bad >"$1"',
            "inner",
            target,
        ],
        cwd=REPO,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not target.exists()


def test_probe_failure_runs_zero_inner_commands(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    env = _env(tmp_path)
    env["RRD_VERIFY_GUARD_FORCE_PROBE_FAILURE"] = "1"
    result = subprocess.run(
        [
            GUARD,
            "--evidence-dir",
            tmp_path / "evidence",
            "--",
            "/bin/sh",
            "-c",
            'printf bad >"$1"',
            "inner",
            marker,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "before inner command" in result.stderr
    assert not marker.exists()
    assert not list((tmp_path / "evidence").glob("guard-*.json"))
