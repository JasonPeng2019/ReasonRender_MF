from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from rrc.contract import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "contextmesh/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

guard = importlib.import_module("rrcv2_product_guard")
refresh = importlib.import_module("rrcv2_reference_refresh")
config_text = importlib.import_module("rrd_native_config").config_text


def test_reviewed_fixture_and_producer_constants_reopen_exactly() -> None:
    fixture = ROOT / "tests/fixtures/rrcv2_cli_smoke/manifest.json"
    value = guard.load_fixture(fixture)
    assert [row["kind"] for row in value["cases"]] == ["miss", "hit", "near"]
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == guard.FIXTURE_SHA256
    assert hashlib.sha256(guard._producer_preimage()).hexdigest() == guard.PRODUCER_SHA256  # noqa: SLF001
    assert guard.ROUND_TOKEN.endswith(guard.PRODUCER_SHA256[:32])
    guard._validate_self()  # noqa: SLF001


def test_v22_predecessor_and_launch_authorities_reopen_exactly() -> None:
    guard._validate_predecessor_manifest(ROOT)  # noqa: SLF001
    guard._validate_launch_manifest(ROOT)  # noqa: SLF001


def test_diff_review_requires_current_forked_ship_subject(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repo, check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("initial\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    review_root = repo / ".generated/state/rrcv2-convergence/reviews"
    review_root.mkdir(parents=True)
    transcript = review_root / "v21-diff.txt"
    transcript.write_text("VERDICT: SHIP\n")
    transcript.chmod(0o600)
    record = repo / ".generated/state/reviews/diff-worktree.toml"
    record.parent.mkdir(parents=True)
    subject = guard._current_diff_subject(repo)  # noqa: SLF001
    record.write_text(
        "\n".join(
            (
                'kind = "diff"',
                'scope = "worktree"',
                'verdict = "SHIP"',
                f'subject_hash = "{subject}"',
                f'transcript_hash = "{hashlib.sha256(transcript.read_bytes()).hexdigest()}"',
                'origin = "forked"',
                'mode = "unleashed"',
                'audience_entry_id = ""',
                'persona = ""',
                'recorded_at = "2026-08-12T00:00:00Z"',
                f'repo_root = "{repo}"',
                'workspace_session = ""',
                'workspace_runtime_root = ""',
                "",
            )
        )
    )
    record.chmod(0o600)
    guard._validate_fresh_diff_review(repo)  # noqa: SLF001
    tracked.write_text("changed\n")
    with pytest.raises(guard.GuardError, match="absent, stale, or not SHIP"):
        guard._validate_fresh_diff_review(repo)  # noqa: SLF001


def test_prepublication_commands_reject_forged_true_receipts(tmp_path: Path) -> None:
    specs = guard._prepublication_command_specs(tmp_path)  # noqa: SLF001
    commands = [
        {
            "argv": ["true"],
            "environment": {},
            "exit_code": 0,
            "name": name,
            "output_bytes": 0,
            "output_path": (
                f".generated/state/rrcv2-convergence/verify/v22-command-receipts/{name}.log"
            ),
            "output_sha256": hashlib.sha256(b"").hexdigest(),
        }
        for name in specs
    ]
    with pytest.raises(guard.GuardError, match="command receipt differs"):
        guard._validate_prepublication_commands(  # noqa: SLF001
            tmp_path, commands, command_specs=specs
        )


def test_prepublication_commands_reopen_exact_argv_environment_and_output(
    tmp_path: Path,
) -> None:
    output = b"735 passed, 63 deselected\n"
    relative = ".generated/state/rrcv2-convergence/verify/v22-command-receipts/ordinary.log"
    output_path = tmp_path / relative
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(output)
    output_path.chmod(0o600)
    specs = {
        "ordinary": (
            ["uv", "run", "--locked", "pytest", "-q"],
            {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            (b"735 passed", b"63 deselected"),
        )
    }
    row = {
        "argv": specs["ordinary"][0],
        "environment": specs["ordinary"][1],
        "exit_code": 0,
        "name": "ordinary",
        "output_bytes": len(output),
        "output_path": relative,
        "output_sha256": hashlib.sha256(output).hexdigest(),
    }
    guard._validate_prepublication_commands(  # noqa: SLF001
        tmp_path, [row], command_specs=specs
    )

    row["argv"] = ["true"]
    with pytest.raises(guard.GuardError, match="command receipt differs"):
        guard._validate_prepublication_commands(  # noqa: SLF001
            tmp_path, [row], command_specs=specs
        )
    row["argv"] = specs["ordinary"][0]
    output_path.write_bytes(b"forged\n")
    output_path.chmod(0o600)
    with pytest.raises(guard.GuardError, match="output differs"):
        guard._validate_prepublication_commands(  # noqa: SLF001
            tmp_path, [row], command_specs=specs
        )


def test_stable_home_lock_acquisition_has_a_wall_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    lock = home / ".rrcv2-product-home.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(guard, "_STABLE_HOME_LOCK_SECONDS", 0.01)
        with pytest.raises(guard.GuardError, match="lock acquisition timed out"):
            guard.prepare_stable_home(repo=ROOT, home=home, user_home=tmp_path)
    finally:
        os.close(descriptor)


def test_isolated_smoke_bootstrap_imports_only_from_bound_paths(tmp_path: Path) -> None:
    script = ROOT / "contextmesh/scripts/rrcv2_product_smoke.py"
    command = guard._smoke_inner_command(  # noqa: SLF001
        repo=ROOT,
        script=script,
        mode="import",
        args=(),
    )
    environment = {
        "HOME": str(tmp_path),
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "dumb",
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": str(tmp_path / "poison-pythonpath"),
        "PYTHONUSERBASE": str(tmp_path / "poison-userbase"),
    }
    completed = subprocess.run(command, env=environment, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr

    old = subprocess.run(
        [str(Path(sys.executable).resolve()), "-I", str(script)],
        env=environment,
        capture_output=True,
        check=False,
    )
    assert old.returncode != 0

    alternate = tmp_path / "alternate.py"
    alternate.write_text("value = 1\n")
    mutations = (
        [*command[:-3], str(tmp_path), command[-2], command[-1]],
        [*command[:-2], str(alternate), command[-1]],
        [*command[:-1], "invalid"],
        [*command, "extra"],
    )
    for mutation in mutations:
        rejected = subprocess.run(mutation, env=environment, capture_output=True, check=False)
        assert rejected.returncode == 125


def test_main_smoke_command_has_one_outer_sandbox_and_bound_suffix(tmp_path: Path) -> None:
    profile = tmp_path / "product.sb"
    fixture = ROOT / "tests/fixtures/rrcv2_cli_smoke/manifest.json"
    command = guard._smoke_command(  # noqa: SLF001
        profile=profile,
        repo=ROOT,
        script=ROOT / "contextmesh/scripts/rrcv2_product_smoke.py",
        fixture=fixture,
        round_root=tmp_path / "round",
    )
    assert command[:3] == ["/usr/bin/sandbox-exec", "-f", str(profile)]
    assert command.count("/usr/bin/sandbox-exec") == 1
    assert command[-5:] == [
        "main",
        "--fixture",
        str(fixture),
        "--round-root",
        str(tmp_path / "round"),
    ]


def test_fixture_rejects_extra_entry_and_hash_drift(tmp_path: Path) -> None:
    copied = tmp_path / "fixture"
    shutil.copytree(ROOT / "tests/fixtures/rrcv2_cli_smoke", copied)
    (copied / "extra").mkdir()
    with pytest.raises(guard.GuardError, match="extra or missing"):
        guard.load_fixture(copied / "manifest.json")
    (copied / "extra").rmdir()
    (copied / "near/starter.py").write_text("pass\n")
    with pytest.raises(guard.GuardError, match="nested hash"):
        guard.load_fixture(copied / "manifest.json")


def test_product_environment_has_the_closed_prefix_inventory(tmp_path: Path) -> None:
    for name in ("repo", "home", "user", "round", "producer"):
        (tmp_path / name).mkdir()
    for name in ("codex", "uv"):
        path = tmp_path / name
        path.write_text("binary")
        path.chmod(0o755)
    env = guard.product_environment(
        repo=tmp_path / "repo",
        native_home=tmp_path / "home",
        user_home=tmp_path / "user",
        codex_bin=tmp_path / "codex",
        uv_bin=tmp_path / "uv",
        round_root=tmp_path / "round",
        producer_root=tmp_path / "producer",
        nonce="a" * 32,
    )
    assert len([key for key in env if key.startswith("RRD_")]) == 15
    assert len([key for key in env if key.startswith("RRC_")]) == 14
    assert len([key for key in env if key.startswith("RRCV2_")]) == 16
    assert set(env).isdisjoint(
        {"OPENAI_API_KEY", "RRC_CONTROL", "RRC_EVEROS_URL", "RRCV2_EVEROS_TARGET"}
    )
    assert env["RRD_MEMORY_BACKEND"] == "sqlite"
    assert env["RRD_WORKER_MODEL"] == "gpt-5.6-luna"
    assert env["RRCV2_DOCKER_BIN"] == str(Path("/usr/local/bin/docker").resolve())


def test_product_verifier_uses_only_the_bound_docker_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rrc.pipeline import sandbox

    monkeypatch.setenv("RRCV2_PRODUCT_SMOKE", "1")
    monkeypatch.setenv("RRCV2_DOCKER_BIN", "/usr/local/bin/docker")
    assert sandbox._docker_binary() == "/usr/local/bin/docker"  # noqa: SLF001
    monkeypatch.setenv("RRCV2_DOCKER_BIN", "docker")
    with pytest.raises(sandbox.SandboxUnavailable, match="authority is missing"):
        sandbox._docker_binary()  # noqa: SLF001


def test_stable_home_reconciles_stale_config_and_recovers_exact_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "contextmesh/scripts"
    scripts.mkdir(parents=True)
    hook = scripts / "rrd_codex_hook.py"
    hook.write_text("pass\n")
    hook.chmod(0o755)
    home = repo / "contextmesh/.codex-rrd-native"
    home.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    user_home = tmp_path / "user"
    (user_home / ".codex").mkdir(parents=True)
    for name, raw in {
        "config.toml": b'model = "stale"\n',
        "hooks.json": b"{}\n",
        "credential-deny.sb": b"(version 1)\n(allow default)\n",
    }.items():
        path = home / name
        path.write_bytes(raw)
        path.chmod(0o600)
    expected_config = config_text(
        model="gpt-5.5",
        reasoning="medium",
        worker_model="gpt-5.6-luna",
        worker_reasoning="low",
    ).encode()
    temporary = home / ".rrcv2-product-config.toml.next"
    temporary.write_bytes(expected_config)
    temporary.chmod(0o600)
    monkeypatch.setattr(guard.sys, "executable", sys.executable)
    lease = guard.prepare_stable_home(repo=repo, home=home, user_home=user_home)
    try:
        assert (home / "config.toml").read_bytes() == expected_config
        assert b"gpt-5.6-luna" in (home / "config.toml").read_bytes()
        assert "[projects." not in (home / "config.toml").read_text()
        assert not temporary.exists()
        guard.verify_stable_home(home, lease)
        (home / "hooks.json").write_text("{}\n")
        os.chmod(home / "hooks.json", 0o600)
        with pytest.raises(guard.GuardError, match="changed"):
            guard.verify_stable_home(home, lease)
    finally:
        lease.close()


def test_credential_profile_probe_requires_kernel_denial_and_writable_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = tmp_path / "user"
    repo = tmp_path / "repo"
    native_home = tmp_path / "native"
    round_root = tmp_path / "round"
    for path in (
        user_home / ".codex",
        repo / "contextmesh",
        native_home,
        round_root / "tmp",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (user_home / ".codex/config.toml").write_text("readable canary")
    profile = round_root / "product.sb"
    profile.write_text("(version 1)\n(allow default)\n")
    profile.chmod(0o600)

    def denied(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        payload = json.loads(command[-1])
        observed = {
            "targets": [
                {
                    "kind": ("absent" if row["kind"] == "absent" else "denied_" + row["kind"]),
                    "path": row["path"],
                }
                for row in payload["targets"]
            ],
            "v": 1,
        }
        return subprocess.CompletedProcess(command, 0, guard.canonical_json_bytes(observed), b"")

    monkeypatch.setattr(guard.subprocess, "run", denied)
    guard._probe_credential_profile(  # noqa: SLF001
        profile=profile,
        user_home=user_home,
        repo=repo,
        native_home=native_home,
        round_root=round_root,
    )
    value = json.loads((round_root / "credential-probe.json").read_bytes())
    canary = next(row for row in value["targets"] if row["path"].endswith(".codex/config.toml"))
    assert canary["kind"] == "denied_regular"


def test_producer_is_one_shot_and_recovers_linked_temporary(tmp_path: Path) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    preparation = root / "preparation.json"
    preparation.write_bytes(guard._preparation_value())  # noqa: SLF001
    preparation.chmod(0o600)
    round_root = root / "round"
    round_root.mkdir(mode=0o700)
    round_authority = guard._owned_directory(round_root)  # noqa: SLF001
    assert guard.publish_producer(root, round_authority=round_authority) is True
    assert guard.publish_producer(root, round_authority=round_authority) is False
    final = root / "producer.json"
    temporary = root / ".producer.v22.tmp"
    assert guard.publish_producer(root, round_authority=round_authority) is False
    assert temporary.is_file()
    assert temporary.stat().st_ino == final.stat().st_ino
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert json.loads(final.read_bytes())["producer_sha256"] == guard.PRODUCER_SHA256


def test_producer_rejects_foreign_temporary_without_mutating_it(tmp_path: Path) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    temporary = root / ".producer.v22.tmp"
    temporary.write_bytes(b"foreign")
    temporary.chmod(0o600)
    preparation = root / "preparation.json"
    preparation.write_bytes(guard._preparation_value())  # noqa: SLF001
    preparation.chmod(0o600)
    round_root = root / "round"
    round_root.mkdir(mode=0o700)
    before = hashlib.sha256(temporary.read_bytes()).hexdigest()
    with pytest.raises(guard.GuardError, match="temporary differs"):
        guard.publish_producer(
            root,
            round_authority=guard._owned_directory(round_root),  # noqa: SLF001
        )
    assert hashlib.sha256(temporary.read_bytes()).hexdigest() == before
    assert not (root / "producer.json").exists()


def test_zero_provider_prepublication_failure_removes_only_exact_prepared_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    round_root = root / "round"
    round_root.mkdir(mode=0o700)
    producer_authority = guard._owned_directory(root)  # noqa: SLF001
    round_authority = guard._owned_directory(round_root)  # noqa: SLF001
    assert (
        guard._reconcile_unpublished_root(root, producer_authority, round_authority)  # noqa: SLF001
        is False
    )
    assert not root.exists()

    root.mkdir(mode=0o700)
    round_root.mkdir(mode=0o700)
    (root / "preparation.json").write_bytes(guard._preparation_value())  # noqa: SLF001
    (root / "preparation.json").chmod(0o600)
    producer_authority = guard._owned_directory(root)  # noqa: SLF001
    round_authority = guard._owned_directory(round_root)  # noqa: SLF001
    assert guard.publish_producer(root, round_authority=round_authority) is True
    assert (
        guard._reconcile_unpublished_root(root, producer_authority, round_authority)  # noqa: SLF001
        is True
    )
    assert (root / "producer.json").is_file()


def test_publish_preserves_and_reopens_exact_preparation_marker_before_launch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    (root / "round").mkdir(mode=0o700)
    preparation = root / "preparation.json"
    preparation.write_bytes(guard._preparation_value())  # noqa: SLF001
    preparation.chmod(0o600)

    round_authority = guard._owned_directory(root / "round")  # noqa: SLF001
    assert guard.publish_producer(root, round_authority=round_authority) is True
    assert (root / "producer.json").read_bytes() == guard.producer_value()
    assert preparation.read_bytes() == guard._preparation_value()  # noqa: SLF001
    assert {path.name for path in root.iterdir()} == {
        "preparation.json",
        ".producer.v22.tmp",
        "producer.json",
        "round",
    }


def test_publish_requires_preparation_for_every_fresh_attempt(tmp_path: Path) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    round_root = root / "round"
    round_root.mkdir(mode=0o700)
    with pytest.raises(guard.GuardError, match="preparation authority is missing"):
        guard.publish_producer(
            root,
            round_authority=guard._owned_directory(round_root),  # noqa: SLF001
        )


@pytest.mark.parametrize("mutation", ["replace", "rename", "inject"])
def test_publish_rejects_marker_or_inventory_substitution_without_deleting_foreign_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    (root / "round").mkdir(mode=0o700)
    marker = root / "preparation.json"
    marker.write_bytes(guard._preparation_value())  # noqa: SLF001
    marker.chmod(0o600)
    original_fsync = guard._fsync_dir  # noqa: SLF001
    mutated = False

    def mutate_after_publish(path: Path) -> None:
        nonlocal mutated
        original_fsync(path)
        if not mutated and (root / "producer.json").exists():
            if mutation in {"replace", "rename"}:
                marker.rename(root / "original-preparation.json")
            if mutation == "replace":
                marker.write_bytes(b"foreign")
                marker.chmod(0o600)
            elif mutation == "inject":
                (root / "foreign-entry").write_bytes(b"foreign")
            mutated = True

    monkeypatch.setattr(guard, "_fsync_dir", mutate_after_publish)
    with pytest.raises(guard.GuardError, match="publication inventory|preparation"):
        guard.publish_producer(
            root,
            round_authority=guard._owned_directory(root / "round"),  # noqa: SLF001
        )
    if mutation == "replace":
        assert marker.read_bytes() == b"foreign"
    elif mutation == "rename":
        assert (root / "original-preparation.json").read_bytes() == guard._preparation_value()  # noqa: SLF001
    else:
        assert (root / "foreign-entry").read_bytes() == b"foreign"


@pytest.mark.parametrize("recovery", [False, True])
def test_publish_rejects_temporary_substitution_without_deleting_foreign_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery: bool,
) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    round_root = root / "round"
    round_root.mkdir(mode=0o700)
    marker = root / "preparation.json"
    marker.write_bytes(guard._preparation_value())  # noqa: SLF001
    marker.chmod(0o600)
    temporary = root / ".producer.v22.tmp"
    if recovery:
        temporary.write_bytes(guard.producer_value())
        temporary.chmod(0o600)
    original_fsync = guard._fsync_dir  # noqa: SLF001
    mutated = False

    def replace_temporary(path: Path) -> None:
        nonlocal mutated
        original_fsync(path)
        if not mutated and (root / "producer.json").exists():
            temporary.rename(root / "original-temporary")
            temporary.write_bytes(b"foreign")
            temporary.chmod(0o600)
            mutated = True

    monkeypatch.setattr(guard, "_fsync_dir", replace_temporary)
    with pytest.raises(guard.GuardError, match="linked publication|links|publication|authority"):
        guard.publish_producer(
            root,
            round_authority=guard._owned_directory(round_root),  # noqa: SLF001
        )
    assert temporary.read_bytes() == b"foreign"
    assert (root / "original-temporary").read_bytes() == guard.producer_value()


@pytest.mark.parametrize("recovery", [False, True])
def test_publish_rejects_exact_content_linked_pair_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery: bool,
) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    round_root = root / "round"
    round_root.mkdir(mode=0o700)
    marker = root / "preparation.json"
    marker.write_bytes(guard._preparation_value())  # noqa: SLF001
    marker.chmod(0o600)
    temporary = root / ".producer.v22.tmp"
    final = root / "producer.json"
    if recovery:
        temporary.write_bytes(guard.producer_value())
        temporary.chmod(0o600)
    original_authority = tmp_path / "authenticated-temporary"
    original_fsync = guard._fsync_dir  # noqa: SLF001
    mutated = False

    def replace_exact_pair(path: Path) -> None:
        nonlocal mutated
        original_fsync(path)
        if not mutated and final.exists():
            temporary.rename(original_authority)
            final.unlink()
            temporary.write_bytes(guard.producer_value())
            temporary.chmod(0o600)
            os.link(temporary, final, follow_symlinks=False)
            mutated = True

    monkeypatch.setattr(guard, "_fsync_dir", replace_exact_pair)
    with pytest.raises(guard.GuardError, match="publication authority|published producer"):
        guard.publish_producer(
            root,
            round_authority=guard._owned_directory(round_root),  # noqa: SLF001
        )
    assert original_authority.read_bytes() == guard.producer_value()
    assert temporary.read_bytes() == guard.producer_value()
    assert final.read_bytes() == guard.producer_value()
    assert temporary.stat().st_ino == final.stat().st_ino
    assert temporary.stat().st_ino != original_authority.stat().st_ino


def test_publish_rejects_missing_replaced_round_and_preexisting_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing-round"
    missing.mkdir(mode=0o700)
    (missing / "preparation.json").write_bytes(guard._preparation_value())  # noqa: SLF001
    (missing / "preparation.json").chmod(0o600)
    with pytest.raises(guard.GuardError, match="inventory"):
        guard.publish_producer(
            missing,
            round_authority=guard._owned_directory(tmp_path),  # noqa: SLF001
        )

    terminal = tmp_path / "preexisting-terminal"
    terminal.mkdir(mode=0o700)
    terminal_round = terminal / "round"
    terminal_round.mkdir(mode=0o700)
    (terminal / "preparation.json").write_bytes(guard._preparation_value())  # noqa: SLF001
    (terminal / "preparation.json").chmod(0o600)
    (terminal / "terminal.json").write_bytes(b"foreign")
    (terminal / "terminal.json").chmod(0o600)
    with pytest.raises(guard.GuardError, match="inventory"):
        guard.publish_producer(
            terminal,
            round_authority=guard._owned_directory(terminal_round),  # noqa: SLF001
        )

    root = tmp_path / "replace-round"
    root.mkdir(mode=0o700)
    round_root = root / "round"
    round_root.mkdir(mode=0o700)
    authority = guard._owned_directory(round_root)  # noqa: SLF001
    (root / "preparation.json").write_bytes(guard._preparation_value())  # noqa: SLF001
    (root / "preparation.json").chmod(0o600)
    original_fsync = guard._fsync_dir  # noqa: SLF001
    mutated = False

    def replace_round(path: Path) -> None:
        nonlocal mutated
        original_fsync(path)
        if not mutated and (root / "producer.json").exists():
            round_root.rename(root / "original-round")
            round_root.mkdir(mode=0o700)
            mutated = True

    monkeypatch.setattr(guard, "_fsync_dir", replace_round)
    with pytest.raises(guard.GuardError, match="directory identity|inventory"):
        guard.publish_producer(root, round_authority=authority)


def test_preflight_executable_resolution_failure_does_not_wedge_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "producer"

    def reject(_value: str) -> Path:
        raise guard.GuardError("injected executable failure")

    monkeypatch.setattr(guard, "resolve_executable", reject)
    with pytest.raises(guard.GuardError, match="injected executable"):
        guard._preflight_before_publish(  # noqa: SLF001
            repo=ROOT,
            fixture=ROOT / "tests/fixtures/rrcv2_cli_smoke/manifest.json",
            producer_root=root,
        )
    assert not root.exists()


def test_experiment_reservation_prevents_a_late_loser_from_deleting_winner(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "experiment"
    parent.mkdir(mode=0o700)
    winner = guard._acquire_experiment_lease(parent)  # noqa: SLF001
    producer = parent / "producer"
    producer.mkdir(mode=0o700)
    marker = producer / "producer.json"
    marker.write_bytes(b"winner")
    try:
        with pytest.raises(guard.GuardError, match="already reserved"):
            guard._acquire_experiment_lease(parent)  # noqa: SLF001
        assert marker.read_bytes() == b"winner"
    finally:
        winner.close()

    successor = guard._acquire_experiment_lease(parent)  # noqa: SLF001
    try:
        assert marker.read_bytes() == b"winner"
    finally:
        successor.close()


def test_abandoned_marker_bound_preparation_is_recovered_without_a_paid_call(
    tmp_path: Path,
) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    (root / "preparation.json").write_bytes(guard._preparation_value())  # noqa: SLF001
    (root / "preparation.json").chmod(0o600)
    round_root = root / "round"
    round_root.mkdir(mode=0o700)
    (round_root / "partial").write_text("zero-provider preparation")

    guard._reconcile_abandoned_preparation(root)  # noqa: SLF001

    assert not root.exists()


def test_unmarked_abandoned_root_is_never_deleted(tmp_path: Path) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    marker = root / "foreign"
    marker.write_text("preserve")
    with pytest.raises((FileNotFoundError, guard.GuardError)):
        guard._reconcile_abandoned_preparation(root)  # noqa: SLF001
    assert marker.read_text() == "preserve"


def _lifecycle_environment(tmp_path: Path) -> dict[str, str]:
    cancellation = tmp_path / "cancellation"
    children = cancellation / "children"
    children.mkdir(parents=True, mode=0o700)
    return {
        **os.environ,
        "RRCV2_PRODUCT_SMOKE": "1",
        "RRCV2_SMOKE_RUN_NONCE": "9" * 32,
        "RRCV2_SMOKE_CANCELLATION_ROOT": str(cancellation),
        "RRCV2_SMOKE_CANCELLATION_REQUEST": str(cancellation / "request"),
        "RRCV2_SMOKE_CHILD_REGISTRY": str(children),
    }


def test_main_launch_is_registered_before_release_and_cleanup_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _lifecycle_environment(tmp_path)
    sentinel = tmp_path / "ran"
    process = guard._launch_main(  # noqa: SLF001
        [
            sys.executable,
            "-c",
            (
                "import signal,time;from pathlib import Path;"
                f"Path({str(sentinel)!r}).touch();"
                "signal.signal(signal.SIGTERM,lambda *_:None);time.sleep(30)"
            ),
        ],
        environment=environment,
        cwd=tmp_path,
    )
    deadline = guard.time.monotonic() + 2
    while not sentinel.exists() and guard.time.monotonic() < deadline:
        guard.time.sleep(0.01)
    assert sentinel.exists()
    rows = list(Path(environment["RRCV2_SMOKE_CHILD_REGISTRY"]).iterdir())
    assert len(rows) == 1
    assert json.loads(rows[0].read_bytes())["state"] == "live"
    monkeypatch.setattr(guard, "_docker_cleanup", lambda _environment, _docker_bin: None)
    guard._cleanup_product(  # noqa: SLF001
        process=process,
        environment=environment,
        cancellation_root=Path(environment["RRCV2_SMOKE_CANCELLATION_ROOT"]),
        docker_bin=Path("/usr/local/bin/docker"),
    )
    assert process.poll() is not None
    assert (Path(environment["RRCV2_SMOKE_CANCELLATION_REQUEST"])).is_file()


def test_main_registration_failure_never_releases_the_child(tmp_path: Path) -> None:
    environment = _lifecycle_environment(tmp_path)
    registry = Path(environment["RRCV2_SMOKE_CHILD_REGISTRY"])
    registry.rmdir()
    registry.write_text("poison")
    sentinel = tmp_path / "ran"
    with pytest.raises((guard.GuardError, OSError)):
        guard._launch_main(  # noqa: SLF001
            [
                sys.executable,
                "-c",
                f"from pathlib import Path;Path({str(sentinel)!r}).touch()",
            ],
            environment=environment,
            cwd=tmp_path,
        )
    assert not sentinel.exists()


def _reference_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, argparse.Namespace, argparse.Namespace]:
    repo = tmp_path / "repo"
    for relative in (
        "rrc/product_runtime.py",
        "contextmesh/bench/run_rrcv2_bench.py",
        "docs/decisions/0002-rrcv2-full-contextmesh-profile.md",
        "PLAN.md",
        ".generated/state/rrcv2-convergence/reviews/plan-m6-post-v20-hook-bootstrap-v2.txt",
        ".generated/state/rrcv2-convergence/reviews/plan-m6-post-v20-hook-bootstrap-v2.seal.json",
        ".generated/state/rrcv2-convergence/verify/reference-refresh.v20.json",
        ".generated/state/reviews/plan.toml",
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for row in refresh.ROWS:
        target = repo / row.relative
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() == row.post_sha256:
            raw = raw.replace(row.new, row.old)
            target.write_bytes(raw)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == row.pre_sha256
    verify = repo / ".generated/state/rrcv2-convergence/verify"
    verify.mkdir(parents=True, exist_ok=True)
    fake_script = repo / "contextmesh/scripts/rrcv2_reference_refresh.py"
    fake_script.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(refresh, "__file__", str(fake_script))
    record = repo / ".generated/state/reviews/plan.toml"
    record.write_text(record.read_text().replace(str(ROOT), str(repo)))
    monkeypatch.setattr(refresh, "RECORD_SHA256", hashlib.sha256(record.read_bytes()).hexdigest())
    seal_path = (
        repo
        / ".generated/state/rrcv2-convergence/reviews/plan-m6-post-v20-hook-bootstrap-v2.seal.json"
    )
    seal = json.loads(seal_path.read_bytes())
    for name in ("plan_path", "record_path", "repo_root", "transcript_path"):
        seal[name] = str(seal[name]).replace(str(ROOT), str(repo))
    seal["record_sha256"] = refresh.RECORD_SHA256
    seal_path.write_bytes(canonical_json_bytes(seal))
    monkeypatch.setattr(refresh, "SEAL_SHA256", hashlib.sha256(seal_path.read_bytes()).hexdigest())
    common = {
        "plan": repo / "PLAN.md",
        "transcript": repo
        / ".generated/state/rrcv2-convergence/reviews/plan-m6-post-v20-hook-bootstrap-v2.txt",
        "seal": repo
        / ".generated/state/rrcv2-convergence/reviews/plan-m6-post-v20-hook-bootstrap-v2.seal.json",
        "record": record,
        "output": verify / "reference-refresh.post-v20-hook-bootstrap-v2.json",
    }
    apply_args = argparse.Namespace(
        **common,
        pending=verify / "reference-refresh.post-v20-hook-bootstrap-v2.pending.json",
    )
    return repo, apply_args, argparse.Namespace(**common)


def test_reference_refresh_converges_validates_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, apply_args, validate_args = _reference_fixture(tmp_path, monkeypatch)
    assert refresh.apply(apply_args) == 0
    assert refresh.validate(validate_args) == 0
    assert refresh.apply(apply_args) == 0
    assert not apply_args.pending.exists()
    for row in refresh.ROWS:
        assert hashlib.sha256((repo / row.relative).read_bytes()).hexdigest() == row.post_sha256


def test_reference_refresh_resumes_mixed_pre_post_state_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, apply_args, validate_args = _reference_fixture(tmp_path, monkeypatch)
    original = refresh._replace_target
    calls = 0

    def interrupt_after_first(target_repo: Path, row: object) -> None:
        nonlocal calls
        original(target_repo, row)
        calls += 1
        if calls == 1:
            raise OSError("simulated crash")

    monkeypatch.setattr(refresh, "_replace_target", interrupt_after_first)
    with pytest.raises(OSError, match="simulated crash"):
        refresh.apply(apply_args)
    assert apply_args.pending.exists()
    assert (
        hashlib.sha256((repo / refresh.ROWS[0].relative).read_bytes()).hexdigest()
        == refresh.ROWS[0].post_sha256
    )
    monkeypatch.setattr(refresh, "_replace_target", original)
    assert refresh.apply(apply_args) == 0
    assert refresh.validate(validate_args) == 0


def test_reference_refresh_preserves_foreign_publication_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, apply_args, _validate_args = _reference_fixture(tmp_path, monkeypatch)
    temporary = apply_args.pending.with_name("." + apply_args.pending.name + ".tmp")
    temporary.write_bytes(b"foreign")
    temporary.chmod(0o600)
    before = hashlib.sha256(temporary.read_bytes()).hexdigest()
    with pytest.raises(refresh.RefreshError, match="foreign publication"):
        refresh.apply(apply_args)
    assert hashlib.sha256(temporary.read_bytes()).hexdigest() == before
