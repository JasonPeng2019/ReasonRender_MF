from __future__ import annotations

import os
import runpy
import shutil
import subprocess
from pathlib import Path

SOURCE_SCRIPT = Path(__file__).parents[1] / "scripts" / "demo_tui.sh"
SOURCE_WRAPPER = Path(__file__).parents[1] / "demo.sh"
SOURCE_BENCH = Path(__file__).parents[1] / "bench" / "run_bench.py"
SOURCE_PROMPT = Path(__file__).parents[1] / "demo-prompt.txt"


def _demo_root(tmp_path: Path, curl_script: str) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SOURCE_SCRIPT, scripts / "demo_tui.sh")
    (root / ".env.local").write_text(
        "OLLAMA_API_KEY=test-only\n"
        "OLLAMA_BASE_URL=https://example.invalid/v1\n"
        "CONTEXTMESH_MODEL=test-model\n"
    )
    (root / "configs").mkdir()
    (root / "configs" / "arm-a.json").write_text("{}\n")
    (root / "configs" / "arm-b.json").write_text("{}\n")
    target = root / "bench" / "target-template"
    target.mkdir(parents=True)
    (target / "README.md").write_text("test workspace\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(f"#!/bin/sh\n{curl_script}\n")
    curl.chmod(0o755)

    marker = tmp_path / "opencode-invoked"
    opencode = fake_bin / "opencode"
    opencode.write_text(
        "#!/bin/sh\n"
        'echo invoked >> "$OPENCODE_MARKER"\n'
        'if [ "${1:-}" = "--version" ]; then echo 1.18.15; exit 0; fi\n'
        "exit 99\n"
    )
    opencode.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "CONTEXTMESH_OPENCODE_BIN": str(opencode),
            "OPENCODE_MARKER": str(marker),
        }
    )
    return root, marker, env


def test_side_a_fails_before_opencode_when_tollgate_is_unreachable(tmp_path: Path) -> None:
    root, marker, env = _demo_root(tmp_path, "exit 7")

    result = subprocess.run(
        [root / "scripts" / "demo_tui.sh", "a"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "Tollgate is not reachable on http://127.0.0.1:8788" in result.stderr
    assert "demo.sh prep" in result.stderr
    assert not marker.exists()


def test_side_b_fails_before_opencode_when_everos_is_unreachable(tmp_path: Path) -> None:
    root, marker, env = _demo_root(
        tmp_path,
        'case "$*" in *127.0.0.1:8788*) exit 0;; *) exit 7;; esac',
    )

    result = subprocess.run(
        [root / "scripts" / "demo_tui.sh", "b"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "EverOS is not reachable on http://127.0.0.1:8000" in result.stderr
    assert "demo.sh prep" in result.stderr
    assert not marker.exists()


def test_top_level_side_launch_starts_stack_before_tui(tmp_path: Path) -> None:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SOURCE_WRAPPER, root / "demo.sh")
    events = tmp_path / "events"
    (scripts / "start_stack.sh").write_text('#!/bin/sh\necho start >> "$DEMO_TEST_EVENTS"\n')
    (scripts / "demo_tui.sh").write_text('#!/bin/sh\necho "tui:$1" >> "$DEMO_TEST_EVENTS"\n')
    (scripts / "live_meter.py").write_text("")
    for script in scripts.iterdir():
        script.chmod(0o755)

    env = os.environ.copy()
    env["DEMO_TEST_EVENTS"] = str(events)
    result = subprocess.run(
        [root / "demo.sh", "a"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert events.read_text().splitlines() == ["start", "tui:a"]


def test_tui_copies_the_canonical_website_prompt(tmp_path: Path) -> None:
    root, _marker, env = _demo_root(tmp_path, "exit 0")
    expected = "CANONICAL WEBSITE DEMO PROMPT\nwith deliberate shared-file overlap\n"
    (root / "demo-prompt.txt").write_text(expected)

    result = subprocess.run(
        [root / "scripts" / "demo_tui.sh", "a"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 99
    assert (root / "runs" / "demo-prompt.txt").read_text() == expected


def test_bench_uses_the_same_canonical_website_prompt() -> None:
    expected = SOURCE_PROMPT.read_text().strip()

    module = runpy.run_path(str(SOURCE_BENCH))

    assert module["TASK"] == expected


def test_prep_copies_the_canonical_website_prompt_to_clipboard(tmp_path: Path) -> None:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SOURCE_WRAPPER, root / "demo.sh")
    expected = "website prompt from contextmesh\n"
    (root / "demo-prompt.txt").write_text(expected)
    for name in ("start_stack.sh", "demo_tui.sh", "demo_preflight.sh"):
        script = scripts / name
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)

    clipboard = tmp_path / "clipboard"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pbcopy = fake_bin / "pbcopy"
    pbcopy.write_text('#!/bin/sh\ncat > "$DEMO_TEST_CLIPBOARD"\n')
    pbcopy.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["DEMO_TEST_CLIPBOARD"] = str(clipboard)

    result = subprocess.run(
        [root / "demo.sh", "prep"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert clipboard.read_text() == expected
