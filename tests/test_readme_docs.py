#!/usr/bin/env python3
"""
Tests that verify all CLI commands and options are documented in the README.
Uses pytest-subtests following the pattern from til.simonwillison.net/pytest/subtests
"""

import re
import subprocess
from pathlib import Path

README = Path(__file__).parent.parent / "README.md"


def _extract_cli_options(*args):
    """Run --help on a script and extract all option flags.

    Args can be a single script name or multiple args for subcommands:
        _extract_cli_options("convert_audio.py")
        _extract_cli_options("publish_audio.py", "podcast")
    """
    result = subprocess.run(
        ["uv", "run", "python", *args, "--help"],
        capture_output=True, text=True
    )
    # Match long-form options like --output, --voice, --model
    return re.findall(r"(--[a-z][-a-z]*)", result.stdout)


def _unique(items):
    """Deduplicate while preserving order."""
    seen = set()
    return [x for x in items if not (x in seen or seen.add(x))]


def test_text_to_speech_options_documented(subtests):
    """Every text_to_speech.py CLI option should appear in the README."""
    readme_text = README.read_text()
    options = _unique(_extract_cli_options("text_to_speech.py"))
    for option in options:
        if option == "--help":
            continue
        with subtests.test(option=option):
            assert option in readme_text, f"{option} not documented in README"


def test_verify_audio_options_documented(subtests):
    """Every verify_audio.py CLI option should appear in the README."""
    readme_text = README.read_text()
    options = _unique(_extract_cli_options("verify_audio.py"))
    for option in options:
        if option == "--help":
            continue
        with subtests.test(option=option):
            assert option in readme_text, f"{option} not documented in README"


def test_convert_audio_options_documented(subtests):
    """Every convert_audio.py CLI option should appear in the README."""
    readme_text = README.read_text()
    options = _unique(_extract_cli_options("convert_audio.py"))
    for option in options:
        if option == "--help":
            continue
        with subtests.test(option=option):
            assert option in readme_text, f"{option} not documented in README"


def test_publish_audio_podcast_options_documented(subtests):
    """Every publish_audio.py podcast subcommand option should appear in the README."""
    readme_text = README.read_text()
    options = _unique(_extract_cli_options("publish_audio.py", "podcast"))
    for option in options:
        if option in ("--help",):
            continue
        with subtests.test(option=option):
            assert option in readme_text, f"publish_audio.py podcast {option} not documented in README"


def test_publish_audio_book_options_documented(subtests):
    """Every publish_audio.py book subcommand option should appear in the README."""
    readme_text = README.read_text()
    options = _unique(_extract_cli_options("publish_audio.py", "book"))
    for option in options:
        if option in ("--help",):
            continue
        with subtests.test(option=option):
            assert option in readme_text, f"publish_audio.py book {option} not documented in README"


def test_all_scripts_documented(subtests):
    """Every CLI script should be mentioned in the README."""
    readme_text = README.read_text()
    for script in ["text_to_speech.py", "verify_audio.py", "convert_audio.py", "publish_audio.py"]:
        with subtests.test(script=script):
            assert script in readme_text, f"{script} not mentioned in README"


from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_skill_lives_at_its_gerund_name():
    skill = ROOT / ".claude" / "skills" / "recording-article-audio" / "SKILL.md"
    assert skill.exists()
    assert "name: recording-article-audio" in skill.read_text()
    assert not (ROOT / ".claude" / "skills" / "article-tts-recording.md").exists()


def test_docs_name_the_skill_by_its_new_name():
    for doc in ("README.md", "docs/adr-001-no-orchestration-script.md"):
        text = (ROOT / doc).read_text()
        assert "article-tts-recording" not in text, doc
    readme = (ROOT / "README.md").read_text()
    assert ".claude/skills/recording-article-audio/SKILL.md" in readme
    assert "/recording-article-audio" in readme
