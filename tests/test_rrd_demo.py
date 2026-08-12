from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from rrc.contract import ArmMode, Completion, Spec, Task, canonical_json_bytes
from rrc.demo import format_meter, run_demo_arm
from rrc.pipeline.stubs import FakeModel, InMemoryRetrieval, fake_completion
from rrc.pipeline.verify import (
    CodeArtifactV1,
    RepairEvidenceV1,
    VerificationResultV1,
    VerificationRunV1,
    VerificationTestsV1,
    VerificationTierRowV1,
    code_artifact_bytes,
    verification_result_bytes,
)

SCRIPT = Path(__file__).parents[1] / "contextmesh" / "RRDdemo.sh"
EVEROS_SCRIPT = SCRIPT.parent / "RRDdemo-everos.sh"
LOCAL_SCRIPT = SCRIPT.parent / "RRDdemo-local.sh"
DISPATCH_SCRIPT = SCRIPT.parent / "scripts" / "rrd_demo.sh"
TUI_SCRIPT = SCRIPT.parent / "scripts" / "rrd_demo_tui.sh"


def _spec(function: str, number: int) -> str:
    value = str(number)
    return json.dumps(
        {
            "plan": f"Implement {function} and always return {value}.",
            "signature": f"def {function}(value: int) -> int",
            "contract": f"Return the constant {value} for every integer input.",
            "tests": [f"def test_behavior():\n    assert {function}(99) == {value}"],
            "slots": {
                "entity": None,
                "identifiers": [function],
                "types": [],
                "fields": [],
                "constants": [value],
                "edge_values": [],
            },
        },
        separators=(",", ":"),
    )


def _independent(function: str, number: int) -> str:
    return json.dumps(
        {"tests": [f"def test_independent():\n    assert {function}(-7) == {number}"], "v": 1},
        sort_keys=True,
        separators=(",", ":"),
    )


def _code(source: str, *, tokens: int = 4) -> Callable[[str], Completion]:
    def response(prompt: str) -> Completion:
        match = re.search(
            r"attempt_id and artifact_path must be exactly '([0-9a-f]{64})' and '([^']+)'",
            prompt,
        )
        assert match is not None
        return fake_completion(
            json.dumps(
                {
                    "artifact_path": match.group(2),
                    "attempt_id": match.group(1),
                    "source": source,
                    "v": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            tokens=tokens,
        )

    return response


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _verification(
    *,
    attempt_id: str,
    task: Task,
    source: str,
    tests: VerificationTestsV1,
    specification: Spec | None,
) -> VerificationRunV1:
    artifact = CodeArtifactV1(attempt_id, task.artifact_path, source)
    artifact_sha = _sha(code_artifact_bytes(artifact))
    accepted = not any("999" in test for test in tests.spec)
    names = ["assembly", "ruff"]
    if specification is not None:
        names.append("signature_conformance")
    names.extend(("pyright", "pytest"))
    rows = tuple(
        VerificationTierRowV1(
            name,  # type: ignore[arg-type]
            "passed" if accepted or index < len(names) - 1 else "failed",
            artifact_sha,
            _sha(b"passed\n" if accepted or index < len(names) - 1 else b"failed\n"),
            _sha(
                b""
                if accepted or index < len(names) - 1
                else canonical_json_bytes({"kind": "verification_failure", "tier": name, "v": 1})
            ),
        )
        for index, name in enumerate(names)
    )
    result = VerificationResultV1(
        attempt_id,
        task.verification_profile,
        artifact_sha,
        rows,
        accepted,
    )
    result_sha = _sha(verification_result_bytes(result))
    excerpt = "failed"
    return VerificationRunV1(
        result,
        artifact,
        (),
        repair_evidence=(
            None
            if accepted
            else RepairEvidenceV1(
                attempt_id,
                result_sha,
                names[-1],  # type: ignore[arg-type]
                _sha(excerpt.encode()),
                excerpt,
            )
        ),
    )


@pytest.fixture(autouse=True)
def fake_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    import rrc.pipeline.solve as solve_module

    monkeypatch.setattr(solve_module, "_run_verifier", _verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: True)


def test_cold_demo_runs_the_public_pipeline_twice_without_reuse(tmp_path: Path) -> None:
    model = FakeModel(
        {
            "spec": [
                fake_completion(_spec("return_two", 2), tokens=10),
                fake_completion(_spec("return_three", 3), tokens=10),
            ],
            "independent_tests": [
                fake_completion(_independent("return_two", 2), tokens=2),
                fake_completion(_independent("return_three", 3), tokens=2),
            ],
            "implement": [
                _code("def return_two(value: int) -> int:\n    return 2"),
                _code("def return_three(value: int) -> int:\n    return 3"),
            ],
        }
    )

    evidence = run_demo_arm(
        round_id="test-round",
        mode=ArmMode.COLD,
        model=model,
        retrieval=InMemoryRetrieval(),
        evidence_path=tmp_path / "a" / "evidence.json",
    )

    assert evidence["pipeline"] == "rrc.pipeline.solve"
    assert evidence["proof_pass"] is True
    assert evidence["branches"] == ["miss", "miss"]
    assert evidence["stages"] == [
        ["spec", "independent_tests", "implement"],
        ["spec", "independent_tests", "implement"],
    ]
    assert evidence["total_tokens"] == 32


def test_warm_demo_misses_then_reuses_without_a_second_spec(tmp_path: Path) -> None:
    model = FakeModel(
        {
            "spec": [fake_completion(_spec("return_two", 2), tokens=10)],
            "independent_tests": [fake_completion(_independent("return_two", 2), tokens=2)],
            "implement": [
                _code("def return_two(value: int) -> int:\n    return 2"),
                _code("def return_three(value: int) -> int:\n    return 3"),
            ],
        }
    )
    retrieval = InMemoryRetrieval()
    after_first: list[bool] = []

    evidence = run_demo_arm(
        round_id="test-round",
        mode=ArmMode.WARM,
        model=model,
        retrieval=retrieval,
        evidence_path=tmp_path / "b" / "evidence.json",
        after_first=lambda: after_first.append(True),
    )

    assert evidence["pipeline"] == "rrc.pipeline.solve"
    assert evidence["proof_pass"] is True
    assert evidence["branches"] == ["miss", "reuse"]
    assert evidence["stages"] == [
        ["spec", "independent_tests", "implement"],
        ["implement"],
    ]
    assert evidence["total_tokens"] == 20
    assert after_first == [True]


def test_meter_matches_the_box_format_of_the_contextmesh_demo(tmp_path: Path) -> None:
    cold = {
        "proof_pass": True,
        "total_tokens": 28,
        "model_calls": 4,
        "branches": ["miss", "miss"],
    }
    warm = {
        "proof_pass": True,
        "total_tokens": 18,
        "model_calls": 3,
        "branches": ["miss", "reuse"],
    }

    output = format_meter(cold, warm, round_id="test-round")

    assert output.startswith("┌")
    assert "ReasonRenderCoding live token meter — round test-round" in output
    assert "COLD (demo-a)" in output and "WARM (demo-b)" in output
    assert "SPEC REUSE REMOVED 10 TOKENS" in output
    assert "MISS → REUSE" in output
    assert output.endswith("┘")


def test_rejected_spec_has_an_actionable_failure_reason(tmp_path: Path) -> None:
    model = FakeModel({"spec": ["{}", "{}"]})

    evidence = run_demo_arm(
        round_id="bad-spec",
        mode=ArmMode.COLD,
        model=model,
        retrieval=InMemoryRetrieval(),
        evidence_path=tmp_path / "evidence.json",
    )

    assert evidence["proof_pass"] is False
    assert evidence["failure_reasons"] == [
        "bad-spec-proof-first: SPEC output failed strict schema/task validation",
        "bad-spec-proof-second: SPEC output failed strict schema/task validation",
    ]


def test_report_distinguishes_bad_spec_tests_from_bad_implementation(tmp_path: Path) -> None:
    first = json.loads(_spec("return_two", 2))
    first["tests"] = ["def test_bad_expectation():\n    assert return_two(99) == 999"]
    second = json.loads(_spec("return_three", 3))
    second["tests"] = ["def test_bad_expectation():\n    assert return_three(99) == 999"]
    model = FakeModel(
        {
            "spec": [json.dumps(first), json.dumps(second)],
            "independent_tests": [
                _independent("return_two", 2),
                _independent("return_three", 3),
            ],
            "implement": [
                _code("def return_two(value: int) -> int:\n    return 2"),
                _code("def return_three(value: int) -> int:\n    return 3"),
            ],
            "repair_1": [
                _code("def return_two(value: int) -> int:\n    return 2"),
                _code("def return_three(value: int) -> int:\n    return 3"),
            ],
            "repair_2": [
                _code("def return_two(value: int) -> int:\n    return 2"),
                _code("def return_three(value: int) -> int:\n    return 3"),
            ],
            "fallback_spec": [json.dumps(first), json.dumps(second)],
            "fallback_independent_tests": [
                _independent("return_two", 2),
                _independent("return_three", 3),
            ],
            "fallback_implement": [
                _code("def return_two(value: int) -> int:\n    return 2"),
                _code("def return_three(value: int) -> int:\n    return 3"),
            ],
        }
    )

    evidence = run_demo_arm(
        round_id="bad-tests",
        mode=ArmMode.COLD,
        model=model,
        retrieval=InMemoryRetrieval(),
        evidence_path=tmp_path / "evidence.json",
    )

    assert evidence["failure_reasons"] == [
        "bad-tests-proof-first: model-authored SPEC tests rejected code that passed the hidden oracle",
        "bad-tests-proof-second: model-authored SPEC tests rejected code that passed the hidden oracle",
    ]


@pytest.mark.parametrize("side", ["a", "b"])
def test_shell_sides_launch_the_codex_tui(tmp_path: Path, side: str) -> None:
    root = tmp_path / "repo"
    contextmesh = root / "contextmesh"
    scripts = contextmesh / "scripts"
    scripts.mkdir(parents=True)
    copied_script = contextmesh / "RRDdemo.sh"
    copied_script.write_bytes(SCRIPT.read_bytes())
    copied_script.chmod(0o755)
    shutil.copy2(DISPATCH_SCRIPT, scripts / "rrd_demo.sh")
    (scripts / "rrd_demo.sh").chmod(0o755)
    start = scripts / "rrd_start_stack.sh"
    start.write_text("#!/bin/sh\nexit 0\n")
    start.chmod(0o755)
    calls = tmp_path / "tui-calls"
    tui = scripts / "rrd_demo_tui.sh"
    tui.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$RRC_DEMO_CALLS"\n')
    tui.chmod(0o755)
    env = os.environ.copy()
    env["RRC_DEMO_CALLS"] = str(calls)

    result = subprocess.run(
        [contextmesh / "RRDdemo.sh", side],
        cwd=contextmesh,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert calls.read_text() == f"{side}\n"


def test_rrd_script_describes_the_same_three_terminal_codex_tui_flow() -> None:
    script = DISPATCH_SCRIPT.read_text()

    assert "open the COLD Codex TUI" in script
    assert "open the WARM Codex TUI" in script
    assert 'exec "$ROOT/scripts/rrd_demo_tui.sh" "$cmd"' in script
    assert "headless" not in script.lower()


def _smoke_tui_fixture(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    scripts = repo / "contextmesh/scripts"
    scripts.mkdir(parents=True)
    tui = scripts / "rrd_demo_tui.sh"
    shutil.copy2(TUI_SCRIPT, tui)
    tui.chmod(0o755)
    (scripts / "rrcv2_product_guard.py").write_text("# fixed guard\n")
    python = repo / ".venv/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SMOKE_CALLS"\n')
    python.chmod(0o755)
    return tui, scripts / "rrcv2_product_guard.py"


def test_tui_smoke_route_execs_only_the_fixed_guard_with_closed_arguments(tmp_path: Path) -> None:
    tui, guard = _smoke_tui_fixture(tmp_path)
    calls = tmp_path / "calls"
    result = subprocess.run(
        [
            tui,
            "smoke",
            "--fixture",
            "fixture.json",
            "--round-id",
            "rrcv2-cli-smoke-" + "a" * 32,
            "--timeout-ms",
            "900000",
        ],
        env={**os.environ, "SMOKE_CALLS": str(calls), "RRD_MEMORY_BACKEND": "malicious"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert calls.read_text() == (
        f"{guard} --fixture fixture.json --round-id rrcv2-cli-smoke-{'a' * 32} "
        "--timeout-ms 900000\n"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["smoke"],
        ["smoke", "--round-id", "x", "--fixture", "f", "--timeout-ms", "1"],
        ["smoke", "--fixture", "f", "--round-id", "x", "--runner", "other.py"],
        ["smoke", "--fixture", "f", "--round-id", "x", "--timeout-ms", "1", "extra"],
    ],
)
def test_tui_smoke_route_rejects_every_other_argument_shape(
    tmp_path: Path, arguments: list[str]
) -> None:
    tui, _ = _smoke_tui_fixture(tmp_path)
    calls = tmp_path / "calls"
    result = subprocess.run(
        [tui, *arguments],
        env={**os.environ, "SMOKE_CALLS": str(calls)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert not calls.exists()


@pytest.mark.parametrize(
    ("script", "backend"),
    [
        (SCRIPT, "everos"),
        (EVEROS_SCRIPT, "everos"),
        (LOCAL_SCRIPT, "sqlite"),
    ],
)
def test_public_rrd_launchers_force_their_memory_backend(
    tmp_path: Path, script: Path, backend: str
) -> None:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    launcher = root / script.name
    shutil.copy2(script, launcher)
    launcher.chmod(0o755)
    dispatcher = scripts / "rrd_demo.sh"
    dispatcher.write_text('#!/bin/sh\nprintf "%s\\n" "$RRD_MEMORY_BACKEND"\n')
    dispatcher.chmod(0o755)

    result = subprocess.run(
        [launcher, "prompt"],
        env={**os.environ, "RRD_MEMORY_BACKEND": "sqlite" if backend == "everos" else "everos"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == f"{backend}\n"


def test_private_rrd_dispatcher_rejects_an_invalid_backend_before_side_effects(
    tmp_path: Path,
) -> None:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(DISPATCH_SCRIPT, scripts / "rrd_demo.sh")
    (scripts / "rrd_start_stack.sh").write_text('#!/bin/sh\ntouch "$SIDE_EFFECT"\n')
    for path in scripts.iterdir():
        path.chmod(0o755)
    side_effect = tmp_path / "side-effect"

    result = subprocess.run(
        [scripts / "rrd_demo.sh", "up"],
        env={**os.environ, "RRD_MEMORY_BACKEND": "invalid", "SIDE_EFFECT": str(side_effect)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "RRD_MEMORY_BACKEND" in result.stderr
    assert not side_effect.exists()


def test_local_stack_and_down_are_service_free(tmp_path: Path) -> None:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("rrd_start_stack.sh", "rrd_stop_stack.sh"):
        shutil.copy2(SCRIPT.parent / "scripts" / name, scripts / name)
        (scripts / name).chmod(0o755)

    up = subprocess.run(
        [scripts / "rrd_start_stack.sh"],
        env={**os.environ, "RRD_MEMORY_BACKEND": "sqlite"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    down = subprocess.run(
        [scripts / "rrd_stop_stack.sh"],
        env={**os.environ, "RRD_MEMORY_BACKEND": "sqlite"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert up.returncode == down.returncode == 0
    assert "no service required" in up.stdout
    assert "nothing to stop" in down.stdout
    assert not (root / "runs/rrd-everos-native.json").exists()


def test_everos_stack_bootstraps_storage_only_without_external_model_credentials() -> None:
    script = (SCRIPT.parent / "scripts/rrd_start_stack.sh").read_text()

    assert 'EVEROS_STORAGE_MODEL="disabled-storage-only"' in script
    assert 'EVEROS_STORAGE_KEY="disabled-storage-only"' in script
    assert 'EVEROS_STORAGE_URL="http://127.0.0.1:9/v1"' in script
    assert '-e EVEROS_LLM__MODEL="$EVEROS_STORAGE_MODEL"' in script
    assert '-e EVEROS_LLM__API_KEY="$EVEROS_STORAGE_KEY"' in script
    assert '-e EVEROS_LLM__BASE_URL="$EVEROS_STORAGE_URL"' in script
    assert "EVEROS_EMBEDDING__" not in script


def _fake_codex(path: Path) -> None:
    disabled = (
        "apps plugins recommended_plugins remote_plugin plugin_sharing browser_use "
        "browser_use_external browser_use_full_cdp_access in_app_browser computer_use "
        "image_generation view_image in_app_updates skill_mcp_dependency_install "
        "tool_call_mcp_elicitation"
    ).split()
    lines = ["hooks stable true", "multi_agent stable true", "multi_agent_v2 stable false"]
    lines.extend(f"{name} stable false" for name in disabled)
    path.write_text(
        "#!/bin/bash\n"
        "if [ \"${1:-}\" = --version ]; then echo 'codex-cli 0.147.0'; exit 0; fi\n"
        "if [ \"${1:-}\" = features ]; then cat <<'EOF'\n"
        + "\n".join(lines)
        + "\nEOF\nexit 0\nfi\n"
        "if [[ \"$*\" == *'login status'* ]]; then echo 'Logged in using ChatGPT'; exit 0; fi\n"
        "exit 3\n"
    )
    path.chmod(0o755)


def test_local_preflight_uses_native_keyring_config_without_service_check(tmp_path: Path) -> None:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("rrd_demo_preflight.sh", "rrd_native_config.py", "rrd_codex_hook.py"):
        shutil.copy2(SCRIPT.parent / "scripts" / name, scripts / name)
        (scripts / name).chmod(0o755)
    (root / "RRD-demo-prompt.txt").write_text("native prompt\n")
    codex = tmp_path / "codex"
    _fake_codex(codex)

    result = subprocess.run(
        [scripts / "rrd_demo_preflight.sh"],
        env={
            **os.environ,
            "RRD_MEMORY_BACKEND": "sqlite",
            "RRD_CODEX_BIN": str(codex),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "local SQLite mode requires no service" in result.stdout
    config = (root / ".codex-rrd-native/config.toml").read_text()
    assert 'cli_auth_credentials_store = "keyring"' in config
    assert "[features]" in config and "plugins = false" in config
    assert not (root / ".codex-rrd-native/auth.json").exists()


def test_tui_reset_generates_a_backend_bound_native_round(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = repo / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("rrd_demo_tui.sh", "rrd_native_config.py", "rrd_codex_hook.py"):
        shutil.copy2(SCRIPT.parent / "scripts" / name, scripts / name)
        (scripts / name).chmod(0o755)
    shutil.copytree(SCRIPT.parent / "bench", root / "bench")
    (root / "RRD-demo-prompt.txt").write_text("native prompt\n")
    codex = tmp_path / "codex"
    _fake_codex(codex)

    result = subprocess.run(
        [scripts / "rrd_demo_tui.sh", "reset"],
        env={
            **os.environ,
            "RRD_MEMORY_BACKEND": "sqlite",
            "RRD_CODEX_BIN": str(codex),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    round_id = (root / "runs/rrd-demo/round-sqlite").read_text().strip()
    meta = json.loads((root / f"runs/rrd-demo/{round_id}/round-meta.json").read_text())
    assert meta == {
        "v": 2,
        "round_id": round_id,
        "memory_backend": "sqlite",
        "provider": "native-codex",
        "model": "gpt-5.5",
    }
    assert (root / f"runs/rrd-demo/{round_id}/a/target/.git").is_dir()
    assert (root / f"runs/rrd-demo/{round_id}/b/target/.git").is_dir()


def test_native_matrix_planner_has_nine_comparable_cells(tmp_path: Path) -> None:
    script = SCRIPT.parent / "bench/run_bench.py"
    output = tmp_path / "matrix.json"
    result = subprocess.run(
        [sys.executable, script, "--plan", "--output", output],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    value = json.loads(output.read_text())
    assert value["v"] == 3
    assert len(value["cells"]) == 9
    assert {cell["workers"] for cell in value["cells"]} == {1, 2, 4}
    assert {cell["variant"] for cell in value["cells"]} == {
        "baseline",
        "combined-local",
        "combined-everos",
    }
    assert value["billing_exact"] is value["hidden_retry_observable"] is False
    assert value["cell_timeout_seconds"] == 720
    assert "ceiling" not in json.dumps(value).lower()


def test_rrd_prompt_points_only_to_the_generated_rrcv2_coding_contract() -> None:
    prompt = (SCRIPT.parent / "RRD-demo-prompt.txt").read_text()
    assert "rrcv2_demo_prompt.py" in prompt
    assert "not the historical four-handler audit prompt" in prompt
    assert "src/handlers/" not in prompt


def test_public_native_assets_have_no_custom_provider_or_model_proxy() -> None:
    manifest = SCRIPT.parent / "active-runtime-files.txt"
    names = [line for line in manifest.read_text().splitlines() if line]
    forbidden = ("ollama", "opencode", "tollgate")
    for name in names:
        path = SCRIPT.parent.parent / name
        lowered = path.read_text().lower()
        assert not any(token in lowered for token in forbidden), path
