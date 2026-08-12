from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTEXTMESH = REPO / "contextmesh"
CONVERGENCE_PATTERNS = {
    "everos": re.compile(r"EverOS", re.IGNORECASE),
    "fast_profile": re.compile(r"fast profile", re.IGNORECASE),
    "ollama": re.compile(r"Ollama", re.IGNORECASE),
    "one_repair": re.compile(r"one repair", re.IGNORECASE),
    "opencode": re.compile(r"OpenCode", re.IGNORECASE),
    "plan_spec_packet": re.compile(r"PlanSpecPacket", re.IGNORECASE),
    "plan_spec_templates": re.compile(r"plan_spec_templates", re.IGNORECASE),
    "reason_render_coding": re.compile(r"ReasonRenderCoding", re.IGNORECASE),
    "rrc": re.compile(r"rrc(?:v2)?", re.IGNORECASE),
    "rrcv2_fast": re.compile(r"RRCv2_fast", re.IGNORECASE),
    "rrd": re.compile(r"rrd", re.IGNORECASE),
    "tollgate": re.compile(r"tollgate", re.IGNORECASE),
}
HISTORICAL_GUIDANCE_BANNER = "**Historical / non-RRCv2 / superseded.**"
HISTORICAL_PRESCRIPTIVE_DOCS = {
    "docs/IMPLEMENTATION-BRIEF.md",
    "docs/RRCv2-lane-A-pipeline.md",
    "docs/RRCv2-lane-B-memory-measure.md",
    "docs/RRCv2-plan.md",
    "docs/RRCv2_fast.md",
    "docs/decisions/0001-rrcv2-full-two-store-contract.md",
    "docs/everos-rundown.md",
    "docs/rfc-codex-ollama-contextmesh-rrc-demo.md",
    "docs/rfc-contextmesh-rrc-multiagent-demo.md",
    "docs/rfc-rrd-dual-memory-backends.md",
    "docs/rfc-rrd-hierarchical-four-way-ablation.md",
    "docs/rfc-rrd-live-product-matrix.md",
    "docs/rfc-rrd-native-codex-migration.md",
    "docs/rfc-rrd-unbounded-native-matrix.md",
    "docs/rrd-native-matrix-results-2026-08-09.md",
    "rrc/REPO_LAYOUT.md",
    "rrc/lane_b/LIVE_EVEROS_HANDOFF.md",
    "rrc/lane_b/ORCHESTRATOR_POLICY_SPEC.md",
    "rrc/lane_b/PLAN.md",
    "rrc/lane_b/SHAVE_IF_LOW_TIME.md",
    "rrc/lane_b/SUBAGENT_PROTOCOL.md",
    "rrc/lane_b/TEST_PLAN.md",
    "rrc/lane_b/VALIDATION_HANDOFF.md",
    "rrc/lane_b/Workflow.md",
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_native_config_has_no_custom_provider_or_ollama(tmp_path: Path) -> None:
    native = _load("rrd_native_config", CONTEXTMESH / "scripts" / "rrd_native_config.py")
    home = tmp_path / "home"
    native.write_home(
        home=home,
        model="gpt-5.5",
        python_bin=Path(sys.executable).resolve(),
        hook_path=(CONTEXTMESH / "scripts" / "rrd_codex_hook.py").resolve(),
    )
    config = (home / "config.toml").read_text()
    lowered = config.lower()
    assert "model_provider" not in config
    assert "base_url" not in config
    assert "env_key" not in config
    assert "ollama" not in lowered
    assert 'cli_auth_credentials_store = "keyring"' in config
    assert "disable_response_storage" not in config
    assert 'model = "gpt-5.5"' in config
    assert 'default_subagent_model = "gpt-5.6-luna"' in config
    assert 'default_subagent_reasoning_effort = "low"' in config
    assert 'web_search = "disabled"' in config
    for feature in ("apps", "plugins", "browser_use", "computer_use", "image_generation"):
        assert f"{feature} = false" in config
    assert not (home / "auth.json").exists()
    profile = home / "credential-deny.sb"
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert ".ssh" in profile.read_text()
    assert ".codex/sessions" in profile.read_text()


def test_native_config_rejects_invalid_worker_reasoning() -> None:
    native = _load("rrd_native_config_worker", CONTEXTMESH / "scripts" / "rrd_native_config.py")
    with pytest.raises(native.ConfigError, match="worker reasoning"):
        native.config_text(
            model="gpt-5.5",
            reasoning="medium",
            worker_model="gpt-5.6-luna",
            worker_reasoning="bogus",
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS boundary")
def test_generated_outer_sandbox_denies_direct_and_discovery_credential_reads(
    tmp_path: Path,
) -> None:
    native = _load("rrd_native_config_sandbox", CONTEXTMESH / "scripts/rrd_native_config.py")
    user_home = tmp_path / "user"
    poison_dir = user_home / ".ssh"
    poison_dir.mkdir(parents=True)
    poison = poison_dir / "id_matrix_poison"
    poison.write_text("SECRET-POISON-BYTES")
    home = tmp_path / "generated-home"
    native.write_home(
        home=home,
        model="gpt-5.5",
        python_bin=Path(sys.executable).resolve(),
        hook_path=(CONTEXTMESH / "scripts" / "rrd_codex_hook.py").resolve(),
        user_home=user_home,
        repo_root=tmp_path / "repo",
    )
    profile = home / native.SANDBOX_PROFILE

    direct = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", profile, "/bin/cat", poison],
        capture_output=True,
        text=True,
        check=False,
    )
    discovery = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-f",
            profile,
            "/usr/bin/find",
            user_home,
            "-name",
            poison.name,
            "-exec",
            "/bin/cat",
            "{}",
            ";",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert direct.returncode != 0
    assert "SECRET-POISON-BYTES" not in direct.stdout
    assert "SECRET-POISON-BYTES" not in discovery.stdout


def test_post_wait_compresses_locally_and_seals_raw_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = _load("rrd_codex_hook_native", CONTEXTMESH / "scripts" / "rrd_codex_hook.py")
    events = tmp_path / "events.jsonl"
    raw_dir = tmp_path / "raw"
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    monkeypatch.setenv("RRD_RAW_RESULTS", str(raw_dir))
    monkeypatch.setenv("RRD_MEMORY_BACKEND", "sqlite")
    report = "\n".join(
        [
            "## src/handlers/users.js",
            *[
                f"- high | src/handlers/users.js:{line} | Authorization finding number {line}."
                for line in range(1, 90)
            ],
        ]
    )
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "multi_agent_v1wait_agent",
        "tool_use_id": "wait-1",
        "tool_input": {"targets": ["agent-1"], "timeout_ms": 1000},
        "tool_response": {"status": {"agent-1": {"completed": report}}, "timed_out": False},
    }
    result = hook.handle(payload)
    assert result is not None
    assert result["continue"] is False
    delivered = result["stopReason"]
    canonical = json.dumps(
        payload["tool_response"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert len(delivered.encode()) <= 2000
    assert len(delivered.encode()) < int(len(canonical) * 0.65)
    receipts = list(raw_dir.glob("*.txt"))
    assert len(receipts) == 1
    assert receipts[0].read_text() == report
    assert (receipts[0].stat().st_mode & 0o777) == 0o600


def test_small_wait_result_is_preserved_as_an_intentional_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = _load("rrd_codex_hook_bypass", CONTEXTMESH / "scripts" / "rrd_codex_hook.py")
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    monkeypatch.setenv("RRD_RAW_RESULTS", str(tmp_path / "raw"))
    monkeypatch.setenv("RRD_MEMORY_BACKEND", "sqlite")
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "multi_agent_v1wait_agent",
        "tool_use_id": "wait-small",
        "tool_input": {"targets": ["agent-1"]},
        "tool_response": {
            "status": {"agent-1": {"completed": "- low | file.js:1 | Small report."}},
            "timed_out": False,
        },
    }

    assert hook.handle(payload) is None
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["wait_result", "compression_bypass"]
    assert rows[1]["agent_ids"] == ["agent-1"]


def test_public_demo_surface_has_no_legacy_provider_dependency() -> None:
    manifest = CONTEXTMESH / "active-runtime-files.txt"
    names = [line for line in manifest.read_text().splitlines() if line]
    assert names == sorted(set(names))
    discovered = {
        "contextmesh/Dockerfile.everos",
        "contextmesh/RRD-demo-prompt.txt",
        "contextmesh/RRDdemo-everos.sh",
        "contextmesh/RRDdemo-local.sh",
        "contextmesh/RRDdemo.sh",
        "contextmesh/bench/run_bench.py",
        "contextmesh/bench/run_rrcv2_bench.py",
        "contextmesh/bench/rubric-independent.json",
        "contextmesh/demo-prompt.txt",
        "contextmesh/demo.sh",
        "contextmesh/env.example",
        "contextmesh/scripts/smoke_everos.py",
        "contextmesh/scripts/rrc_finisher.py",
        "contextmesh/scripts/rrcv2_demo_prompt.py",
        "contextmesh/scripts/rrcv2_product_cell.py",
        "contextmesh/scripts/rrcv2_product_guard.py",
        "contextmesh/scripts/rrcv2_product_smoke.py",
        *{
            "rrc/attempts.py",
            "rrc/cell_journal.py",
            "rrc/contextmesh.py",
            "rrc/contextmesh_runtime.py",
            "rrc/contract.py",
            "rrc/dispatch_permit.py",
            "rrc/economic_authority.py",
            "rrc/everos.py",
            "rrc/journal.py",
            "rrc/model.py",
            "rrc/multiagent_demo.py",
            "rrc/pipeline/prompts.py",
            "rrc/pipeline/sandbox.py",
            "rrc/pipeline/solve.py",
            "rrc/pipeline/stages.py",
            "rrc/pipeline/template.py",
            "rrc/pipeline/verify.py",
            "rrc/policy.py",
            "rrc/product_runtime.py",
            "rrc/retrieval.py",
            "rrc/sandbox_capability.py",
            "rrc/store.py",
            "rrc/workload.py",
        },
        *{
            str(path.relative_to(REPO))
            for path in (CONTEXTMESH / "scripts").glob("rrd_*")
            if path.is_file() and not path.name.startswith("rrd_audit_")
        },
    }
    assert set(names) == discovered
    forbidden = ("ollama", "opencode", "tollgate")
    for name in names:
        path = REPO / name
        assert path.is_file(), path
        text = path.read_text().lower()
        assert not any(item in text for item in forbidden), path


def test_rrcv2_convergence_inventory_is_current_and_has_no_transitional_legacy() -> None:
    inventory_path = REPO / "docs/rrcv2-convergence-inventory.json"
    inventory = json.loads(inventory_path.read_bytes())
    assert inventory["kind"] == "RRCV2ConvergenceInventoryV1"
    assert inventory["immutable_design_sha256"] == (
        "2a036584574a610ebcf1f32517249166d3f802b1026b83f3cfab4d41a1c5a80e"
    )
    assert inventory["zero_transitional_legacy"] is True
    listed = {row["path"]: row for row in inventory["entries"]}
    assert len(listed) == len(inventory["entries"])

    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        check=True,
    )
    active_runtime = {
        line for line in (CONTEXTMESH / "active-runtime-files.txt").read_text().splitlines() if line
    }
    discovered: dict[str, list[str]] = {}
    relatives = sorted(
        raw_relative for raw_relative in completed.stdout.split(b"\0") if raw_relative
    )
    for raw_relative in relatives:
        relative = raw_relative.decode("utf-8", errors="strict")
        if relative == "docs/rrcv2-convergence-inventory.json":
            continue
        path = REPO / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        searchable = f"{relative}\n{text}"
        matched = [
            name for name, pattern in CONVERGENCE_PATTERNS.items() if pattern.search(searchable)
        ]
        if relative in active_runtime:
            matched.append("active_runtime")
        matched = sorted(set(matched))
        if matched:
            discovered[relative] = matched

    assert set(listed) == set(discovered)
    for relative, matched in discovered.items():
        row = listed[relative]
        assert row["matched_symbols"] == matched
        assert row["sha256"] == hashlib.sha256((REPO / relative).read_bytes()).hexdigest()
        assert row["classification"] in {
            "immutable_fixture_non_rrc",
            "immutable_source_contract",
            "updated_m0",
        }
        assert row["classification"] != "transitional_legacy"
        assert row["rationale"]
        assert row["owner_milestone"]
    assert [row["path"] for row in inventory["entries"]] == sorted(listed)
    source_contracts = [
        row for row in inventory["entries"] if row["classification"] == "immutable_source_contract"
    ]
    assert [row["path"] for row in source_contracts] == ["docs/RRCv2.md"]
    assert active_runtime <= set(listed)


def test_prescriptive_legacy_documents_have_local_superseded_banners() -> None:
    for relative in sorted(HISTORICAL_PRESCRIPTIVE_DOCS):
        path = REPO / relative
        assert path.is_file(), relative
        prefix = "\n".join(path.read_text().splitlines()[:12])
        assert HISTORICAL_GUIDANCE_BANNER in prefix, relative
        assert "ADR 0002" in prefix, relative
        assert re.search(r"not\s*(?:>\s*)?current", prefix, re.IGNORECASE), relative


def test_root_readme_points_to_the_current_rrcv2_authority() -> None:
    text = (REPO / "README.md").read_text()
    assert "docs/decisions/0002-rrcv2-full-contextmesh-profile.md" in text
    assert "docs/rrcv2-requirement-map.md" in text
    assert "docs/rrcv2-convergence-report.md" in text
    assert "Ruff normalization" in text
    assert "Pyright basic" in text
    assert "pytest collection/execution" in text
    assert "ADR 0001 is superseded" in text
    assert "Ruff and Pyright stages are deferred" not in text
    assert "[ADR 0001](" not in text


def test_local_stack_never_probes_everos_or_model_proxy(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls"
    curl = fake_bin / "curl"
    curl.write_text(f'#!/bin/sh\necho "$*" >> {log!s}\nexit 99\n')
    curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "RRD_MEMORY_BACKEND": "sqlite",
    }
    result = subprocess.run(
        ["/bin/bash", str(CONTEXTMESH / "scripts" / "rrd_start_stack.sh")],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not log.exists()
    assert "no service" in result.stdout.lower()


def test_everos_start_refuses_same_owner_fixed_name_without_local_record(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "contextmesh" / "scripts"
    scripts.mkdir(parents=True)
    source = CONTEXTMESH / "scripts" / "rrd_start_stack.sh"
    target = scripts / source.name
    target.write_bytes(source.read_bytes())
    target.chmod(0o755)
    (repo / "contextmesh" / "Dockerfile.everos").write_text("FROM scratch\n")
    everos = repo / "EverOS"
    everos.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", everos], check=True)
    (everos / "README").write_text("fixture")
    subprocess.run(["/usr/bin/git", "-C", everos, "add", "README"], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            everos,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {log!s}\n"
        'case "${1:-}" in\n'
        "  info|build) exit 0 ;;\n"
        "  inspect)\n"
        '    case "$*" in\n'
        "      *org.contextmesh.owner*) echo rrd-native ;;\n"
        "      *) echo foreign-container-id ;;\n"
        "    esac\n"
        "    exit 0 ;;\n"
        "  rm|run) exit 97 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    result = subprocess.run(
        [target],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
            "RRD_MEMORY_BACKEND": "everos",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "without an exact local ownership record" in result.stderr
    calls = log.read_text().splitlines()
    assert not any(line.startswith(("rm ", "run ")) for line in calls)


def test_everos_partial_docker_cleanup_failure_preserves_exact_state(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "contextmesh" / "scripts"
    scripts.mkdir(parents=True)
    start = scripts / "rrd_start_stack.sh"
    start.write_bytes((CONTEXTMESH / "scripts" / start.name).read_bytes())
    start.chmod(0o755)
    (repo / "contextmesh" / "Dockerfile.everos").write_text("FROM scratch\n")
    everos = repo / "EverOS"
    everos.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", everos], check=True)
    (everos / "README").write_text("fixture")
    subprocess.run(["/usr/bin/git", "-C", everos, "add", "README"], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            everos,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["/usr/bin/git", "-C", everos, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {log!s}\n"
        'case "${1:-}" in\n'
        "  info|build|inspect) exit 0 ;;\n"
        "  run) echo owned-container-id; exit 0 ;;\n"
        "  rm) exit 42 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    process = subprocess.Popen(
        [start],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
            "RRD_MEMORY_BACKEND": "everos",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    runs = repo / "contextmesh" / "runs"
    try:
        for _ in range(200):
            if list(runs.glob("rrd-everos-native.json.tmp.*")):
                break
            if process.poll() is not None:
                pytest.fail(f"startup exited early: {process.communicate()}")
            time.sleep(0.02)
        else:
            pytest.fail("partial Docker ownership record was not created")
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=10)
        assert process.returncode != 0
        assert "exact ownership record preserved" in stderr
        state = runs / "rrd-everos-native.json"
        value = json.loads(state.read_text())
        assert value == {
            "v": 1,
            "owner": "rrd-native",
            "kind": "docker",
            "revision": revision,
            "container_id": "owned-container-id",
            "data_root": str(repo / "contextmesh" / "everos-native-root" / revision),
        }
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        assert not (runs / ".rrd-everos-native.lock").exists()
        assert any(
            line.startswith("rm -f owned-container-id") for line in log.read_text().splitlines()
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_everos_lifecycle_lock_blocks_a_concurrent_owner(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "contextmesh" / "scripts"
    scripts.mkdir(parents=True)
    source = CONTEXTMESH / "scripts" / "rrd_start_stack.sh"
    target = scripts / source.name
    target.write_bytes(source.read_bytes())
    target.chmod(0o755)
    lock = repo / "contextmesh" / "runs" / ".rrd-everos-native.lock"
    lock.mkdir(parents=True, mode=0o700)
    lock.chmod(0o700)
    owner = lock / "owner"
    owner.write_text(f"{os.getpid()}\n")
    owner.chmod(0o600)

    result = subprocess.run(
        [target],
        env={
            **os.environ,
            "RRD_MEMORY_BACKEND": "everos",
            "RRD_LIFECYCLE_LOCK_ATTEMPTS": "2",
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "timed out waiting" in result.stderr
    assert owner.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="host ownership uses macOS ps/lsof")
def test_everos_start_refuses_to_overwrite_live_unhealthy_host_state(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "contextmesh" / "scripts"
    scripts.mkdir(parents=True)
    start = scripts / "rrd_start_stack.sh"
    start.write_bytes((CONTEXTMESH / "scripts" / start.name).read_bytes())
    start.chmod(0o755)
    (repo / "contextmesh" / "Dockerfile.everos").write_text("FROM scratch\n")
    everos = repo / "EverOS"
    everos.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", everos], check=True)
    (everos / "README").write_text("fixture")
    subprocess.run(["/usr/bin/git", "-C", everos, "add", "README"], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            everos,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["/usr/bin/git", "-C", everos, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    data_root = repo / "contextmesh" / "everos-native-root" / revision
    data_root.mkdir(parents=True)
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/bash\nexec /bin/sleep 60\n")
    uv.chmod(0o755)
    nonce = "c" * 32
    wrapper = CONTEXTMESH / "scripts" / "rrd_everos_host.py"
    process = subprocess.Popen(
        [
            sys.executable,
            wrapper,
            "--nonce",
            nonce,
            "--cwd",
            everos,
            "--uv",
            uv,
            "--root",
            data_root,
        ]
    )
    try:
        for _ in range(100):
            if process.poll() is None and os.getpgid(process.pid) == process.pid:
                break
            time.sleep(0.02)
        else:
            pytest.fail("host wrapper fixture did not start")
        start_time = subprocess.run(
            ["ps", "-p", str(process.pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        executable_rows = subprocess.run(
            [
                "/usr/sbin/lsof",
                "-a",
                "-p",
                str(process.pid),
                "-d",
                "txt",
                "-Fn",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        executable = next(line[1:] for line in executable_rows if line.startswith("n"))
        runs = repo / "contextmesh" / "runs"
        runs.mkdir()
        state = runs / "rrd-everos-native.json"
        original = json.dumps(
            {
                "v": 1,
                "owner": "rrd-native",
                "kind": "host",
                "revision": revision,
                "pid": process.pid,
                "pgid": process.pid,
                "data_root": str(data_root),
                "start_time": start_time,
                "executable": executable,
                "cwd": str(everos),
                "nonce": nonce,
            }
        )
        state.write_text(original)
        state.chmod(0o600)

        result = subprocess.run(
            [start],
            env={**os.environ, "RRD_MEMORY_BACKEND": "everos"},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0
        assert "live but unhealthy" in result.stderr
        assert state.read_text() == original
        assert process.poll() is None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "darwin", reason="host ownership uses macOS process groups")
def test_everos_start_refuses_orphaned_live_recorded_group(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "contextmesh" / "scripts"
    scripts.mkdir(parents=True)
    start = scripts / "rrd_start_stack.sh"
    start.write_bytes((CONTEXTMESH / "scripts" / start.name).read_bytes())
    start.chmod(0o755)
    everos = repo / "EverOS"
    everos.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", everos], check=True)
    (everos / "README").write_text("fixture")
    subprocess.run(["/usr/bin/git", "-C", everos, "add", "README"], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            everos,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["/usr/bin/git", "-C", everos, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    data_root = repo / "contextmesh" / "everos-native-root" / revision
    data_root.mkdir(parents=True)
    pids = tmp_path / "pids"
    leader = tmp_path / "leader.py"
    leader.write_text(
        "import os,pathlib,signal,subprocess,sys\n"
        "os.setsid()\n"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()}|{child.pid}')\n"
    )
    subprocess.run([sys.executable, leader, pids], check=True)
    stale_pid, descendant = (int(value) for value in pids.read_text().split("|"))
    assert (
        subprocess.run(["ps", "-p", str(stale_pid)], capture_output=True, check=False).returncode
        != 0
    )
    assert os.getpgid(descendant) == stale_pid
    try:
        runs = repo / "contextmesh" / "runs"
        runs.mkdir()
        state = runs / "rrd-everos-native.json"
        original = json.dumps(
            {
                "v": 1,
                "owner": "rrd-native",
                "kind": "host",
                "revision": revision,
                "pid": stale_pid,
                "pgid": stale_pid,
                "data_root": str(data_root),
                "start_time": "recorded",
                "executable": sys.executable,
                "cwd": str(everos),
                "nonce": "d" * 32,
            }
        )
        state.write_text(original)
        state.chmod(0o600)
        result = subprocess.run(
            [start],
            env={**os.environ, "RRD_MEMORY_BACKEND": "everos"},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0
        assert "remains live without its leader" in result.stderr
        assert state.read_text() == original
        assert os.getpgid(descendant) == stale_pid
    finally:
        try:
            os.killpg(stale_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(sys.platform != "darwin", reason="host ownership uses macOS process groups")
def test_everos_start_signal_rolls_back_entire_partial_process_group(
    tmp_path: Path,
) -> None:
    probe = subprocess.run(
        ["/usr/bin/curl", "-sf", "-m", "1", "http://127.0.0.1:8000/health"],
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        pytest.skip("port 8000 is already in use")
    repo = tmp_path / "repo"
    scripts = repo / "contextmesh" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("rrd_start_stack.sh", "rrd_everos_host.py"):
        target = scripts / name
        target.write_bytes((CONTEXTMESH / "scripts" / name).read_bytes())
        target.chmod(0o755)
    everos = repo / "EverOS"
    everos.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", everos], check=True)
    (everos / "README").write_text("fixture")
    subprocess.run(["/usr/bin/git", "-C", everos, "add", "README"], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            everos,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["/usr/bin/git", "-C", everos, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    data_root = repo / "contextmesh" / "everos-native-root" / revision
    data_root.mkdir(parents=True)
    (data_root / "everos.toml").write_text("fixture")
    pids = tmp_path / "partial-pids"
    child = tmp_path / "partial-child.py"
    child.write_text(
        "import os,pathlib,signal,subprocess,sys\n"
        "stubborn=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()}|{stubborn.pid}')\n"
        "signal.pause()\n"
    )
    uv = tmp_path / "uv"
    uv.write_text(f"#!/bin/bash\nexec {sys.executable} {child} {pids}\n")
    uv.chmod(0o755)
    process = subprocess.Popen(
        [scripts / "rrd_start_stack.sh"],
        env={
            **os.environ,
            "PATH": "/usr/bin:/bin",
            "RRD_MEMORY_BACKEND": "everos",
            "RRC_DEMO_UV_BIN": str(uv),
            "RRD_GROUP_TERM_ATTEMPTS": "2",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pgid = 0
    try:
        for _ in range(150):
            if pids.exists():
                child_pid, stubborn_pid = (int(value) for value in pids.read_text().split("|"))
                pgid = os.getpgid(child_pid)
                assert os.getpgid(stubborn_pid) == pgid
                break
            if process.poll() is not None:
                pytest.fail(f"startup exited early: {process.communicate()}")
            time.sleep(0.02)
        else:
            pytest.fail("partial-start fixture did not launch")
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 143, stderr
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)
        assert not (repo / "contextmesh" / "runs" / "rrd-everos-native.json").exists()
        assert not (repo / "contextmesh" / "runs" / ".rrd-everos-native.lock").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_host_everos_wrapper_owns_and_terminates_its_process_group(tmp_path: Path) -> None:
    probe = subprocess.run(
        ["/usr/bin/curl", "-sf", "-m", "1", "http://127.0.0.1:8000/health"],
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        pytest.skip("port 8000 is already in use")
    cwd = tmp_path / "everos"
    cwd.mkdir()
    root = tmp_path / "data"
    root.mkdir()
    server = tmp_path / "server.py"
    server.write_text(
        "from http.server import BaseHTTPRequestHandler,HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        " def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        " def log_message(self,*args): pass\n"
        "HTTPServer(('127.0.0.1',8000),H).serve_forever()\n"
    )
    uv = tmp_path / "uv"
    uv.write_text(f"#!/bin/bash\nexec {sys.executable} {server}\n")
    uv.chmod(0o755)
    wrapper = CONTEXTMESH / "scripts" / "rrd_everos_host.py"
    process = subprocess.Popen(
        [
            sys.executable,
            wrapper,
            "--nonce",
            "a" * 32,
            "--cwd",
            cwd,
            "--uv",
            uv,
            "--root",
            root,
        ]
    )
    try:
        for _ in range(100):
            if (
                subprocess.run(
                    [
                        "/usr/bin/curl",
                        "-sf",
                        "-m",
                        "1",
                        "http://127.0.0.1:8000/health",
                    ],
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            ):
                break
            time.sleep(0.02)
        else:
            pytest.fail("fixture server did not become healthy")
        assert os.getpgid(process.pid) == process.pid
        listener = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-t", "-iTCP:8000", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert listener and os.getpgid(int(listener.splitlines()[0])) == process.pid
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    assert (
        subprocess.run(
            ["/usr/bin/curl", "-sf", "-m", "1", "http://127.0.0.1:8000/health"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="host ownership uses macOS ps/lsof")
def test_everos_stop_kills_signal_ignoring_nonlistener_descendant(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "contextmesh" / "scripts"
    scripts.mkdir(parents=True)
    stop = scripts / "rrd_stop_stack.sh"
    stop.write_bytes((CONTEXTMESH / "scripts" / stop.name).read_bytes())
    stop.chmod(0o755)
    runs = repo / "contextmesh" / "runs"
    runs.mkdir()

    cwd = tmp_path / "everos"
    cwd.mkdir()
    root = tmp_path / "data"
    root.mkdir()
    stubborn_pid_file = tmp_path / "stubborn.pid"
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib,signal,subprocess,sys\n"
        "stubborn=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(stubborn.pid))\n"
        "signal.pause()\n"
    )
    uv = tmp_path / "uv"
    uv.write_text(f"#!/bin/bash\nexec {sys.executable} {child} {stubborn_pid_file}\n")
    uv.chmod(0o755)
    nonce = "b" * 32
    wrapper = CONTEXTMESH / "scripts" / "rrd_everos_host.py"
    launch = tmp_path / "launch.py"
    launch.write_text(
        "import subprocess,sys\n"
        "p=subprocess.Popen(sys.argv[1:],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "print(p.pid)\n"
    )
    launched = subprocess.run(
        [
            sys.executable,
            launch,
            sys.executable,
            wrapper,
            "--nonce",
            nonce,
            "--cwd",
            cwd,
            "--uv",
            uv,
            "--root",
            root,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pid = int(launched.stdout.strip())
    try:
        for _ in range(100):
            if (
                stubborn_pid_file.exists()
                and subprocess.run(
                    ["ps", "-p", str(pid)], capture_output=True, check=False
                ).returncode
                == 0
            ):
                break
            time.sleep(0.02)
        else:
            pytest.fail("host wrapper fixture did not start")
        stubborn_pid = int(stubborn_pid_file.read_text())
        pgid = os.getpgid(pid)
        assert pgid == pid and os.getpgid(stubborn_pid) == pgid
        start_time = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        executable_rows = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "txt", "-Fn"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        executable = next(line[1:] for line in executable_rows if line.startswith("n"))
        state = runs / "rrd-everos-native.json"
        state.write_text(
            json.dumps(
                {
                    "v": 1,
                    "owner": "rrd-native",
                    "kind": "host",
                    "revision": "a" * 40,
                    "pid": pid,
                    "pgid": pgid,
                    "data_root": str(root),
                    "start_time": start_time,
                    "executable": executable,
                    "cwd": str(cwd),
                    "nonce": nonce,
                }
            )
        )
        state.chmod(0o600)

        result = subprocess.run(
            [stop],
            env={
                **os.environ,
                "RRD_MEMORY_BACKEND": "everos",
                "RRD_GROUP_TERM_ATTEMPTS": "2",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert not state.exists()
        for _ in range(100):
            if (
                subprocess.run(
                    ["ps", "-p", str(stubborn_pid)], capture_output=True, check=False
                ).returncode
                != 0
            ):
                break
            time.sleep(0.02)
        else:
            pytest.fail("signal-ignoring descendant survived owned group teardown")
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_everos_stop_refuses_changed_container_identity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "contextmesh" / "scripts"
    scripts.mkdir(parents=True)
    source = CONTEXTMESH / "scripts" / "rrd_stop_stack.sh"
    target = scripts / source.name
    target.write_bytes(source.read_bytes())
    target.chmod(0o755)
    runs = repo / "contextmesh" / "runs"
    runs.mkdir()
    state = runs / "rrd-everos-native.json"
    state.write_text(
        json.dumps(
            {
                "v": 1,
                "owner": "rrd-native",
                "kind": "docker",
                "revision": "a" * 40,
                "container_id": "owned-id",
                "data_root": str(repo / "contextmesh" / "data"),
            }
        )
    )
    state.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {log!s}\n"
        "case \"$*\" in *'{{.Id}}'*) echo different-id ;; esac\n"
    )
    docker.chmod(0o755)
    result = subprocess.run(
        [target],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
            "RRD_MEMORY_BACKEND": "everos",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "identity changed" in result.stderr
    assert not any(line.startswith("rm ") for line in log.read_text().splitlines())
    assert state.exists()


def test_example_env_contains_no_secret_placeholder() -> None:
    text = (CONTEXTMESH / "env.example").read_text().lower()
    assert "api_key" not in text
    assert "your-" not in text
    assert "rrd_codex_bin=" in text
