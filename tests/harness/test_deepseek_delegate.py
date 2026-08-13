from __future__ import annotations

from pathlib import Path

from harness.deepseek_delegate import AUTO_COMPACT_TOKEN_LIMIT, CONTEXT_WINDOW, MODEL, command, load_profile

ROOT = Path(__file__).resolve().parents[2]


def test_shipped_deepseek_profile_is_a_230k_one_million_token_contract() -> None:
    profile = load_profile(ROOT)

    assert profile.model == MODEL
    assert profile.model_context_window == CONTEXT_WINDOW
    assert profile.model_auto_compact_token_limit == AUTO_COMPACT_TOKEN_LIMIT == 230_000
    assert profile.model_catalog.is_file()


def test_initial_and_resume_commands_preserve_deepseek_contract_without_session_collision(tmp_path: Path) -> None:
    initial_one = command(ROOT, tmp_path / "worker-one-final.md", extra_config=("mcp_servers.contextmesh.required=true",))
    initial_two = command(ROOT, tmp_path / "worker-two-final.md")
    resumed = command(ROOT, tmp_path / "worker-one-final.md", resume_thread_id="worker-one-thread")

    assert initial_one[1:3] == ("-m", "harness.qwen_delegate")
    assert initial_one[initial_one.index("--qwen-bin") + 1] == "qwen"
    assert "--contextmesh" in initial_one
    assert str(tmp_path / "worker-one-final.md") in initial_one
    assert str(tmp_path / "worker-two-final.md") in initial_two
    assert "--contextmesh" not in initial_two
    assert resumed[1:3] == ("-m", "harness.qwen_delegate")
    assert resumed[-2:] == ("--resume-session-id", "worker-one-thread")
