#!/usr/bin/env python3
"""Generate and validate the credential-free native Codex demo home."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

CLI_VERSION = "codex-cli 0.147.0"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_WORKER_MODEL = "gpt-5.6-luna"
DEFAULT_WORKER_REASONING = "low"
SANDBOX_PROFILE = "credential-deny.sb"
DISABLED_FEATURES = (
    "apps",
    "plugins",
    "recommended_plugins",
    "remote_plugin",
    "plugin_sharing",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "in_app_browser",
    "computer_use",
    "image_generation",
    "view_image",
    "in_app_updates",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
)


class ConfigError(RuntimeError):
    """Native Codex configuration failed validation."""


def _quote(value: str) -> str:
    return json.dumps(value)


def resolve_executable(value: str | None) -> Path:
    candidate = value or shutil.which("codex")
    if not candidate:
        raise ConfigError("Codex is not installed; set RRD_CODEX_BIN to its absolute path")
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        raise ConfigError("RRD_CODEX_BIN must be an absolute path")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022:
        raise ConfigError("Codex binary must be a non-group/world-writable regular file")
    return resolved


def stable_home(root: Path) -> Path:
    return root / ".codex-rrd-native"


def config_text(
    *,
    model: str,
    reasoning: str = "medium",
    worker_model: str = DEFAULT_WORKER_MODEL,
    worker_reasoning: str = DEFAULT_WORKER_REASONING,
) -> str:
    if reasoning not in {"low", "medium", "high", "xhigh"}:
        raise ConfigError("RRD_CODEX_REASONING must be low, medium, high, or xhigh")
    if worker_reasoning not in {"none", "low", "medium", "high", "xhigh"}:
        raise ConfigError("worker reasoning must be none, low, medium, high, or xhigh")
    if not worker_model.strip():
        raise ConfigError("worker model must not be empty")
    features = "\n".join(f"{name} = false" for name in DISABLED_FEATURES)
    return f"""model = {_quote(model)}
model_reasoning_effort = {_quote(reasoning)}
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
cli_auth_credentials_store = "keyring"

[analytics]
enabled = false

[feedback]
enabled = false

[features]
hooks = true
multi_agent = true
multi_agent_v2 = false
{features}

[agents]
max_concurrent_threads_per_session = 4
default_subagent_model = {_quote(worker_model)}
default_subagent_reasoning_effort = {_quote(worker_reasoning)}

[agents.worker]
description = "Execute exactly one source-blind ReasonRenderCoding IMPLEMENT assignment. Use only the canonical Spec and public-test digest supplied by the controller, return the exact WorkerCandidateV1 JSON object, and do not call tools or read repository files."

[shell_environment_policy]
inherit = "none"
ignore_default_excludes = false

[shell_environment_policy.set]
PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
HOME = "/private/tmp/rrd-empty-home"
"""


def credential_profile_text(*, user_home: Path, repo_root: Path) -> str:
    """Return a macOS Seatbelt overlay that denies common credential stores."""
    denied_subpaths = (
        user_home / ".ssh",
        user_home / ".aws",
        user_home / ".azure",
        user_home / ".config" / "gcloud",
        user_home / ".config" / "gh",
        user_home / ".gnupg",
        user_home / ".kube",
        user_home / ".codex" / "sessions",
        user_home / ".codex" / "logs",
    )
    denied_files = (
        user_home / ".codex" / "auth.json",
        user_home / ".codex" / "config.toml",
        user_home / ".codex" / "history.jsonl",
        user_home / ".docker" / "config.json",
        user_home / ".git-credentials",
        user_home / ".netrc",
        user_home / ".npmrc",
        user_home / ".pypirc",
        repo_root / ".env",
        repo_root / ".env.local",
        repo_root / "contextmesh" / ".env",
        repo_root / "contextmesh" / ".env.local",
    )
    lines = ["(version 1)", "(allow default)"]
    lines.extend(f"(deny file-read* (subpath {json.dumps(str(path))}))" for path in denied_subpaths)
    lines.extend(f"(deny file-read* (literal {json.dumps(str(path))}))" for path in denied_files)
    return "\n".join(lines) + "\n"


def hooks_value(*, python_bin: Path, hook_path: Path) -> dict[str, object]:
    command = f"{shlex.quote(str(python_bin))} {shlex.quote(str(hook_path))}"

    def handler(timeout: int) -> dict[str, object]:
        return {"type": "command", "command": command, "timeout": timeout}

    return {
        "description": "sealed native Codex ContextMesh/RRC hooks",
        "hooks": {
            "PreToolUse": [{"hooks": [handler(300)]}],
            "PostToolUse": [{"hooks": [handler(20)]}],
            "SubagentStart": [{"hooks": [handler(20)], "matcher": "worker"}],
            "SubagentStop": [{"hooks": [handler(20)], "matcher": "worker"}],
            "Stop": [{"hooks": [handler(10)]}],
        },
    }


def write_home(
    *,
    home: Path,
    model: str,
    python_bin: Path,
    hook_path: Path,
    reasoning: str = "medium",
    worker_model: str = DEFAULT_WORKER_MODEL,
    worker_reasoning: str = DEFAULT_WORKER_REASONING,
    user_home: Path | None = None,
    repo_root: Path | None = None,
) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    config = home / "config.toml"
    hooks = home / "hooks.json"
    profile = home / SANDBOX_PROFILE
    config.write_text(
        config_text(
            model=model,
            reasoning=reasoning,
            worker_model=worker_model,
            worker_reasoning=worker_reasoning,
        ),
        encoding="utf-8",
    )
    hooks.write_text(
        json.dumps(
            hooks_value(python_bin=python_bin, hook_path=hook_path),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    profile.write_text(
        credential_profile_text(
            user_home=(user_home or Path.home()).resolve(),
            repo_root=(repo_root or home.parent.parent).resolve(),
        ),
        encoding="utf-8",
    )
    os.chmod(config, 0o600)
    os.chmod(hooks, 0o600)
    os.chmod(profile, 0o600)
    auth = home / "auth.json"
    if auth.exists() or auth.is_symlink():
        raise ConfigError(f"generated Codex home must not contain {auth.name}")


def _run(command: Sequence[str], *, home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(Path.home()),
        "CODEX_HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
    }
    return subprocess.run(command, env=env, text=True, capture_output=True, check=False)


def validate(*, codex_bin: Path, home: Path, require_login: bool = True) -> None:
    profile = home / SANDBOX_PROFILE
    try:
        metadata = os.lstat(profile)
    except OSError as exc:
        raise ConfigError("credential-deny sandbox profile is missing") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ConfigError("credential-deny sandbox profile must be a mode-0600 regular file")
    if sys.platform == "darwin":
        probe = subprocess.run(
            ("/usr/bin/sandbox-exec", "-f", str(profile), "/usr/bin/true"),
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            raise ConfigError(f"credential-deny sandbox profile is invalid: {probe.stderr.strip()}")
    version = _run((str(codex_bin), "--version"), home=home)
    if version.returncode != 0 or version.stdout.strip() != CLI_VERSION:
        raise ConfigError(
            f"expected {CLI_VERSION}, got {version.stdout.strip() or version.stderr.strip()}"
        )
    features = _run((str(codex_bin), "features", "list"), home=home)
    if features.returncode != 0:
        raise ConfigError(f"could not read Codex features: {features.stderr.strip()}")
    states: dict[str, str] = {}
    for line in features.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            states[fields[0]] = fields[-1]
    for name in ("hooks", "multi_agent"):
        if states.get(name) != "true":
            raise ConfigError(f"Codex feature {name} is not enabled")
    for name in DISABLED_FEATURES:
        if states.get(name) != "false":
            raise ConfigError(f"Codex feature {name} is not disabled")
    if states.get("multi_agent_v2") != "false":
        raise ConfigError("Codex multi_agent_v2 must be disabled for the pinned v1 hook contract")
    if require_login:
        login = _run(
            (
                str(codex_bin),
                "-c",
                'cli_auth_credentials_store="keyring"',
                "login",
                "status",
            ),
            home=home,
        )
        if login.returncode != 0:
            command = (
                f"HOME={shlex.quote(str(Path.home()))} CODEX_HOME={shlex.quote(str(home))} "
                f"{shlex.quote(str(codex_bin))} -c "
                "'cli_auth_credentials_store=\"keyring\"' login"
            )
            raise ConfigError("native Codex keyring login is required; run:\n" + command)


def config_sha256(home: Path) -> str:
    digest = hashlib.sha256()
    for name in ("config.toml", "hooks.json", SANDBOX_PROFILE):
        digest.update(name.encode())
        digest.update((home / name).read_bytes())
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "check", "login-command"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--codex-bin")
    parser.add_argument("--model", default=os.environ.get("RRD_CODEX_MODEL", DEFAULT_MODEL))
    parser.add_argument("--reasoning", default=os.environ.get("RRD_CODEX_REASONING", "medium"))
    parser.add_argument(
        "--worker-model", default=os.environ.get("RRD_WORKER_MODEL", DEFAULT_WORKER_MODEL)
    )
    parser.add_argument(
        "--worker-reasoning",
        default=os.environ.get("RRD_WORKER_REASONING", DEFAULT_WORKER_REASONING),
    )
    parser.add_argument("--no-login", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve(strict=True)
    codex_bin = resolve_executable(args.codex_bin or os.environ.get("RRD_CODEX_BIN"))
    home = stable_home(root)
    python_bin = Path(sys.executable).resolve(strict=True)
    hook_path = (root / "scripts" / "rrd_codex_hook.py").resolve(strict=True)
    if args.command in {"init", "check"}:
        write_home(
            home=home,
            model=args.model,
            python_bin=python_bin,
            hook_path=hook_path,
            reasoning=args.reasoning,
            worker_model=args.worker_model,
            worker_reasoning=args.worker_reasoning,
            repo_root=root.parent,
        )
    if args.command == "check":
        validate(codex_bin=codex_bin, home=home, require_login=not args.no_login)
        print(
            f"native Codex ready: {CLI_VERSION}, model={args.model}, config={config_sha256(home)}"
        )
    elif args.command == "init":
        print(home)
    else:
        print(
            f"HOME={shlex.quote(str(Path.home()))} CODEX_HOME={shlex.quote(str(home))} "
            f"{shlex.quote(str(codex_bin))} -c 'cli_auth_credentials_store=\"keyring\"' login"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(f"NOT READY — {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
