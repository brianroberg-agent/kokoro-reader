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
    assert "noise" not in text


def test_prompt_holds_only_for_gross_errors(text):
    # Brian, 2026-11-10: "Let's only block publication for gross errors like a sentence
    # (or more) of missing or misplaced text, hallucinated text, or degradation of
    # intelligibility." and "There should still be a correction pass to attempt to fix
    # smaller problems, but smaller problems shouldn't block publication." This replaces
    # the spec's hold rule (R24a: any content missing, extra or garbled).
    hold = outcome_line(text, "hold")
    assert ("only for a gross error you couldn't fix: a sentence or more missing or out of "
            "place, spoken text the article doesn't have, or a stretch a listener can't "
            "follow") in hold
    minor = outcome_line(text, "minor")
    assert ("Everything smaller is `minor` once you've tried to fix it, never `hold`: "
            "mispronunciations, a few words left out, pacing, artifacts and a clipped word") in minor


def test_prompt_omission_is_less_than_a_sentence(text):
    kinds = next(line for line in text.splitlines() if line.startswith("Each remaining issue has a `kind`"))
    assert "`omission`, words left out, less than a sentence" in kinds


def test_prompt_spells_out_abbreviations_beside_roman_numerals(text):
    lines = text.splitlines()
    roman = next(n for n, line in enumerate(lines) if line.startswith("- **Roman numerals:**"))
    assert lines[roman + 1].startswith("- **Abbreviations:**")


def report_line(text, label):
    lines = [line for line in text.splitlines() if line.startswith(f"- **{label}:**")]
    assert len(lines) == 1, label
    return lines[0]


def test_prompt_has_fixable_issues_fixed_before_minor(text):
    # E2E finding 1 (2026-11-03): three real sessions each stopped after one round of four,
    # publishing as minor what the tools could fix: Tocqueville said "Tock-ville", a word
    # left out. A mispronounced name was only "Likely fix", and minor read as "anything
    # left after the rounds you used".
    assert "mispronounced names" in report_line(text, "Fix")
    assert "a word or two left out" in report_line(text, "Likely fix")
    minor = outcome_line(text, "minor")
    assert "While record rounds are left" in minor and "isn't `minor` yet" in minor
    assert "A round spent fixing what a listener would hear is a turn you need." in text


def test_prompt_says_the_first_paragraph_is_the_title_and_byline(text):
    # The runner puts the spoken title first (Brian, 2026-11-03); the agent keeps it.
    line = report_line(text, "The opening line")
    assert "title and who wrote it" in line and "Keep it as it is" in line


def test_prompt_says_recording_again_does_not_fix_a_word_misread_every_time(text):
    # E2E finding 1, second run (2026-11-03): on "Zoning Alone" the agent's second round
    # recorded the same text again, then finished minor with "St.", "v.", "1/2" and a name
    # still misread: it took "record again once" as the fix for those too.
    [principle] = [l for l in text.splitlines() if l.startswith("- **The engine is non-deterministic.**")]
    assert "won't change by recording again" in principle
    assert "abbreviations and symbols the engine reads wrong" in report_line(text, "Fix")
    assert "Recording the same text again isn't a fix" in outcome_line(text, "minor")


def test_prompt_says_finish_refuses_minor_before_a_fix_is_tried(text):
    # The narration server's finish enforces it (agent_tools.fix_not_tried), so a
    # session that reads the prompt isn't surprised by the refusal (Brian, 2026-11-03).
    finish = next(line for line in text.splitlines() if line.startswith("- **`finish` ends"))
    assert ("While two or more record rounds and checks are left, `finish` refuses `minor` "
            "that lists a "
            "`mispronunciation` until a round recorded after the first check has changed the "
            "text that check heard, or an `omission` until a round has been recorded after the "
            "first check.") in finish


def test_prompt_asks_for_each_issue_under_its_own_kind(text):
    # finish's fix rule binds mispronunciation and omission only, so another kind would
    # get a session past it; the published notes would then name the wrong kind.
    kinds = next(line for line in text.splitlines() if line.startswith("Each remaining issue has a `kind`"))
    assert ("Give each issue the kind it is, even when that means another round: the episode "
            "notes tell listeners what to expect.") in kinds


def test_prompt_says_record_refuses_a_broken_override(text):
    # narration's record refuses it before the round is spent (agent_tools.record), so a
    # session isn't surprised; "Zoning Alone" (2026-11-10) was held over one at finish.
    [rule] = [l for l in text.splitlines()
              if l.startswith("- **`record` refuses an override `finish` would refuse.**")]
    assert "glued to the text around it" in rule and "no round is used" in rule
