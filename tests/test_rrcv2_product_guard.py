from __future__ import annotations

import argparse
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
    assert len(guard._producer_preimage()) == 139  # noqa: SLF001
    assert hashlib.sha256(guard._producer_preimage()).hexdigest() == guard.PRODUCER_SHA256  # noqa: SLF001
    assert guard.ROUND_TOKEN.endswith(guard.PRODUCER_SHA256[:32])


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
    assert len([key for key in env if key.startswith("RRCV2_")]) == 15
    assert set(env).isdisjoint(
        {"OPENAI_API_KEY", "RRC_CONTROL", "RRC_EVEROS_URL", "RRCV2_EVEROS_TARGET"}
    )
    assert env["RRD_MEMORY_BACKEND"] == "sqlite"
    assert env["RRD_WORKER_MODEL"] == "gpt-5.6-luna"


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
    assert guard.publish_producer(root) is True
    assert guard.publish_producer(root) is False
    final = root / "producer.json"
    temporary = root / ".producer.v20.tmp"
    os.link(final, temporary)
    assert guard.publish_producer(root) is False
    assert not temporary.exists()
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert json.loads(final.read_bytes())["producer_sha256"] == guard.PRODUCER_SHA256


def test_producer_rejects_foreign_temporary_without_mutating_it(tmp_path: Path) -> None:
    root = tmp_path / "producer"
    root.mkdir(mode=0o700)
    temporary = root / ".producer.v20.tmp"
    temporary.write_bytes(b"foreign")
    temporary.chmod(0o600)
    before = hashlib.sha256(temporary.read_bytes()).hexdigest()
    with pytest.raises(guard.GuardError, match="temporary differs"):
        guard.publish_producer(root)
    assert hashlib.sha256(temporary.read_bytes()).hexdigest() == before
    assert not (root / "producer.json").exists()


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
    monkeypatch.setattr(guard, "_docker_cleanup", lambda _environment: None)
    guard._cleanup_product(  # noqa: SLF001
        process=process,
        environment=environment,
        cancellation_root=Path(environment["RRCV2_SMOKE_CANCELLATION_ROOT"]),
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
