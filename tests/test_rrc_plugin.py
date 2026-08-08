from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[1] / "contextmesh/plugin/reasonrendercoding.ts"


def _run_hook(tmp_path: Path, *, exit_code: int) -> tuple[dict[str, object], Path]:
    fake_uv = tmp_path / "fake-uv"
    resolution = {
        "branch": "hit",
        "external_ref": "ref-123",
        "planner_tokens": 0,
        "profile": "lean",
        "rendered_packet": {"specification": "Audit src/handlers/auth.js"},
    }
    fake_uv.write_text(
        "#!/bin/sh\n"
        + (
            f"printf '%s\\n' {json.dumps(json.dumps(resolution))}\n"
            if exit_code == 0
            else "echo bridge-failed >&2\n"
        )
        + f"exit {exit_code}\n"
    )
    fake_uv.chmod(0o755)
    runner = tmp_path / "hook.ts"
    runner.write_text(
        f"import {{ ReasonRenderCodingPlugin }} from {json.dumps(str(PLUGIN))}\n"
        "const hooks = await ReasonRenderCodingPlugin({ $: Bun.$ })\n"
        'const output = { args: { subagent_type: "worker", prompt: "Audit src/handlers/auth.js now." } }\n'
        'await hooks["tool.execute.before"]?.({ tool: "task", sessionID: "parent", callID: "call-1" }, output)\n'
        "console.log(JSON.stringify(output))\n"
    )
    events = tmp_path / "events.jsonl"
    env = os.environ.copy()
    env.update(
        {
            "RRC_DEMO_REPO": str(Path(__file__).parents[1]),
            "RRC_DEMO_ROUND": "rrd-test",
            "RRC_DEMO_MODE": "warm",
            "RRC_DEMO_DATABASE": str(tmp_path / "packets.sqlite"),
            "RRC_DEMO_LOCK": str(tmp_path / "packets.lock"),
            "RRC_DEMO_EVENTS": str(events),
            "RRC_DEMO_MODEL_EVENTS": str(tmp_path / "models.jsonl"),
            "RRC_EVEROS_URL": "http://127.0.0.1:9",
            "RRC_STRONG_MODEL": "fake",
            "RRC_DEMO_UV_BIN": str(fake_uv),
        }
    )
    result = subprocess.run(
        ["bun", "run", runner],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), events


def test_rrc_plugin_augments_the_real_task_prompt(tmp_path: Path) -> None:
    output, events = _run_hook(tmp_path, exit_code=0)
    args = output["args"]
    assert isinstance(args, dict)
    prompt = args["prompt"]
    assert isinstance(prompt, str)
    assert prompt.startswith("Audit src/handlers/auth.js now.")
    assert "[ReasonRenderCoding]" in prompt
    assert "MISS" not in prompt and "HIT" in prompt
    assert "ref-123" in prompt
    assert not events.exists()


def test_rrc_plugin_failure_is_visible_and_fails_open(tmp_path: Path) -> None:
    output, events = _run_hook(tmp_path, exit_code=7)
    args = output["args"]
    assert isinstance(args, dict)
    assert args["prompt"] == "Audit src/handlers/auth.js now."
    event = json.loads(events.read_text())
    assert event["event"] == "fail_open"
    assert event["source"] == "opencode_plugin"
    assert event["failure_id"] == "rrd-test-call-1"
    assert "bridge-failed" in event["error"]


@pytest.mark.skipif(not PLUGIN.exists(), reason="plugin source is required")
def test_plugin_source_exposes_no_standalone_rrc_tool() -> None:
    source = PLUGIN.read_text()
    assert "\n  tool:" not in source
    assert 'input.tool !== "task"' in source
