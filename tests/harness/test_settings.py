from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ROOT / "harness" / "settings"


def test_subagent_stop_settings_are_byte_identical_and_use_confirmed_transport() -> None:
    files = [SETTINGS / f"{arm}.json" for arm in ("raw", "contextmesh", "full")]
    raw = [path.read_bytes() for path in files]

    assert raw[0] == raw[1] == raw[2]
    setting = json.loads(raw[0])
    command = setting["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
    assert "completion_gate.py" in command
    assert "$CLAUDE_PROJECT_DIR" in command
    assert "$REASONRENDER_GATE_MANIFEST" in command
    assert "$REASONRENDER_GATE_STATE_DIR" in command
    assert "$REASONRENDER_GATE_LOG_PATH" in command

    pretool = setting["hooks"]["PreToolUse"]
    assert pretool == [
        {
            "matcher": "Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": 'python "$CLAUDE_PROJECT_DIR/harness/hooks/block_bash_reads.py"',
                }
            ],
        }
    ]
