"""The agent prompt: the system prompt every session runs with (--append-system-prompt)."""

from pathlib import Path

import pytest

from narration_pipeline import session_service as ss

PROMPT = Path(ss.__file__).with_name("agent_prompt.md")


@pytest.fixture(scope="module")
def text():
    return PROMPT.read_text()


def test_prompt_names_every_tool_and_no_shell(text):
    for tool in ss.EXPECTED_TOOLS:  # backticked: record, check and finish are also plain words
        assert f"`{tool.removeprefix('mcp__narration__')}`" in text, tool
    for shell in ("uv run", "python ", ".py", "cd "):
        assert shell not in text, shell


def test_prompt_is_one_command_line_argument(text):
    # It goes to claude as one argv string: Linux caps a single argument at 128 KiB,
    # and an argument starting with "-" would be read as an option.
    assert len(text.encode()) < 100_000
    assert not text.startswith("-")


def test_prompt_leaves_voice_and_speed_at_the_defaults(text):
    assert "Call `record` with no arguments: the podcast's voice and speed are the defaults." in text


def outcome_line(text, outcome):
    lines = [line for line in text.splitlines() if line.startswith(f"- **`{outcome}`:**")]
    assert len(lines) == 1, outcome
    return lines[0]


def test_prompt_outcomes_follow_the_spec(text):
    # Spec: "`minor` is for issues the skill would 'likely ignore' or couldn't fix" (ruling R24).
    assert "nothing you would address or note" in outcome_line(text, "clean")
    minor = outcome_line(text, "minor")
    assert "likely ignore" in minor and "couldn't fix" in minor and "`remaining_issues`" in minor
    assert "only for missing or garbled content" in outcome_line(text, "hold")
    assert "noise" not in text
