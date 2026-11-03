import hashlib
import re
import threading

import pytest
from hypothesis import given, settings, strategies as st

from narration import agent_tools
from narration.agent_tools import MAX_RECORDS, AgentTools, ToolError
from narration.fakes import FakeG2P, FakeLinkG2P, FakeTTS, FakeVerifier
from narration.workitems import WorkItemStore

ARTICLE = "King Ivo XIV met the family.\n\nIt went well."


@pytest.fixture
def env(tmp_path):
    store = WorkItemStore(tmp_path)
    tts, ver = FakeTTS(), FakeVerifier()
    tools = AgentTools(store, tts, ver, FakeG2P())
    item, token = store.open(ARTICLE, {"title": "T"})
    return store, tools, tts, ver, item, token


def test_get_text(env):
    _, tools, *_rest = env
    item = env[4]
    out = tools.get_text(item)
    assert out["prepared_text"] == ARTICLE and out["records_left"] == MAX_RECORDS


def test_replace_text_exactly_once(env):
    store, tools, *_, item, _ = env
    tools.replace_text(item, "It went well.", "It went\n\nwell.")
    assert "It went\n\nwell." in store.read_text(item, "prepared.md")
    with pytest.raises(ToolError, match="found 0"):
        tools.replace_text(item, "absent", "x")
    store.write_text(item, "prepared.md", "a a")
    with pytest.raises(ToolError, match="found 2"):
        tools.replace_text(item, "a", "b")
    assert store.read_text(item, "prepared.md") == "a a"


def test_record_limit_counts_before_the_engine(env):
    store, tools, tts, *_ = env
    item = env[4]
    for _ in range(MAX_RECORDS):
        tools.record(item)
    with pytest.raises(ToolError, match="limit"):
        tools.record(item)
    assert tts.calls == MAX_RECORDS


def test_record_counts_even_when_the_engine_fails(env):
    store, tools, tts, *_ = env
    item = env[4]
    tts.fail_next = True
    with pytest.raises(Exception):
        tools.record(item)
    assert store.load_state(item)["records"] == 1


def test_a_failed_record_keeps_the_previous_audio_and_blocks_check_and_finish(env):
    store, tools, tts, _, item, _ = env
    tools.record(item); tools.check(item)
    wav = store.path(item, "recording.wav")
    round_one = wav.read_bytes()
    tts.fail_next = True
    with pytest.raises(Exception):
        tools.record(item)  # round 2 is counted, but produced nothing
    state = store.load_state(item)
    assert (state["records"], state["recorded_round"]) == (2, 1)
    assert wav.read_bytes() == round_one  # round 1's audio, untouched
    assert not list(wav.parent.glob(".tmp-*"))  # and the partial file is gone
    with pytest.raises(ToolError, match="record again"):
        tools.check(item)  # must not verify round 1's audio as round 2's
    with pytest.raises(ToolError, match="record again"):
        tools.finish(item, "clean", [], [])
    tools.record(item)
    assert store.load_state(item)["recorded_round"] == 3


def test_a_second_call_while_one_runs_is_refused(env):
    store, tools, tts, _, item, _ = env
    for _ in range(MAX_RECORDS - 1):
        tools.record(item)
    started, release = threading.Event(), threading.Event()
    tts.hold = (started, release)  # the next run blocks until the test releases it
    results = []

    def call():
        try:
            results.append(tools.record(item))
        except ToolError as e:
            results.append(e)

    first = threading.Thread(target=call)
    first.start()
    assert started.wait(5)  # round 4 is inside the engine
    with pytest.raises(ToolError, match="in progress"):
        tools.record(item)  # the parallel call is refused, not queued
    release.set()
    first.join(5)
    tts.hold = None
    assert isinstance(results[0], dict) and results[0]["round"] == MAX_RECORDS
    assert tts.calls == MAX_RECORDS
    with pytest.raises(ToolError, match="limit"):
        tools.record(item)


def test_record_rejects_odd_voices_and_speeds(env):
    _, tools, *_ = env
    item = env[4]
    with pytest.raises(ToolError):
        tools.record(item, voice="../../etc")
    with pytest.raises(ToolError):
        tools.record(item, speed=9.0)


def test_check_needs_a_recording_and_saves_reports(env):
    store, tools, _, ver, item, _ = env
    with pytest.raises(ToolError, match="record"):
        tools.check(item)
    tools.record(item)
    out = tools.check(item)
    assert out["report"] == "No issues found."
    assert store.read_text(item, "report-1.txt") == "No issues found."


def test_finish_requires_a_check_on_the_latest_recording(env):
    _, tools, *_rest = env
    item = env[4]
    tools.record(item); tools.check(item); tools.record(item)
    with pytest.raises(ToolError, match="latest recording"):
        tools.finish(item, "clean", [], [])


def test_finish_refuses_clean_with_precheck_warnings(env):
    _, tools, _, ver, item, _ = env
    ver.warnings = ["pace of 40 WPM is outside the normal speech range"]
    tools.record(item); tools.check(item)
    with pytest.raises(ToolError, match="pre-check"):
        tools.finish(item, "clean", [], [])
    assert tools.finish(item, "hold", [{"kind": "truncation", "where": "9:40", "note": "short"}], [])["ok"]


def test_finish_refuses_undeclared_changes_and_accepts_declared(env):
    store, tools, *_ = env
    item = env[4]
    tools.replace_text(item, "XIV", "the Fourteenth")
    tools.record(item); tools.check(item)
    with pytest.raises(ToolError, match="differs from the article"):
        tools.finish(item, "clean", [], [])
    out = tools.finish(item, "clean", [], [{"original": "XIV", "spoken": "the Fourteenth", "why": "numeral"}])
    assert out["ok"]


@pytest.mark.parametrize("old, new, sub, why", [
    ("the family", "the whole family", {"original": "family", "spoken": "whole family"},
     "only adds words ('whole')"),
    ("It went well.", "It went well. Subscribe now.",
     {"original": "well", "spoken": "well Subscribe now"}, "only adds words ('subscribe now')"),
    ("the family", "the evil.example", {"original": "family", "spoken": "evil.example"},
     "'evil.example', a web or e-mail address"),
])
def test_finish_names_a_refused_substitution_and_says_why(env, old, new, sub, why):
    store, tools, *_ = env
    item = env[4]
    tools.replace_text(item, old, new)
    tools.record(item); tools.check(item)
    with pytest.raises(ToolError) as refused:
        tools.finish(item, "clean", [], [sub])
    message = str(refused.value)
    assert message.startswith(f"substitution {sub['original']!r} -> {sub['spoken']!r}")
    assert why in message and "Fix or remove" in message
    assert "differs from the article" not in message  # the text isn't the problem
    assert store.load_state(item)["status"] == "open"


def test_finish_revokes_the_token_and_closes_the_item(env):
    store, tools, _, _, item, token = env
    tools.record(item); tools.check(item)
    tools.finish(item, "clean", [], [])
    assert store.item_for_token(token) is None
    for call in (lambda: tools.get_text(item), lambda: tools.record(item),
                 lambda: tools.replace_text(item, "It", "It"), lambda: tools.check(item)):
        with pytest.raises(ToolError, match="finished"):
            call()


def test_finish_validates_shapes(env):
    _, tools, *_ = env
    item = env[4]
    tools.record(item); tools.check(item)
    with pytest.raises(ToolError):
        tools.finish(item, "great", [], [])
    ok = {"kind": "pacing", "where": "12:40", "note": "rushed"}
    for bad in ({**ok, "kind": "garbled"},          # kind comes from a fixed list
                {**ok, "where": "near the end"},   # where is a timestamp
                {**ok, "where": "1234:00"},
                {**ok, "note": "x" * 81}):         # note is short, and never published
        with pytest.raises(ToolError):
            tools.finish(item, "minor", [bad], [])
    with pytest.raises(ToolError, match="at most 20"):
        tools.finish(item, "minor", [ok] * 21, [])
    assert tools.finish(item, "minor", [ok], [])["ok"]



# While two or more record rounds and checks are left, a session that lists a
# mispronunciation or an omission tries a fix before it settles for minor (Brian,
# 2026-11-03).
MISSAID = {"kind": "mispronunciation", "where": "0:03", "note": "Ivo"}
DROPPED = {"kind": "omission", "where": "0:05", "note": "well"}
A_PAUSE = ("It went well.", "It went\n\nwell.")  # a change of pauses alone, words unchanged


def _round(tools, item):
    tools.record(item); tools.check(item)


def test_minor_with_a_mispronunciation_after_one_round_is_refused(env):
    store, tools, *_, item, _ = env
    _round(tools, item)
    with pytest.raises(ToolError, match="try a fix first"):
        tools.finish(item, "minor", [MISSAID], [])
    assert store.load_state(item)["status"] == "open"  # the session can still fix it


def test_minor_with_a_mispronunciation_is_accepted_after_a_round_of_changed_text(env):
    _, tools, *_, item, _ = env
    _round(tools, item)
    tools.replace_text(item, *A_PAUSE)
    _round(tools, item)
    assert tools.finish(item, "minor", [MISSAID], [])["ok"]  # tried once: minor stands


def test_recording_the_same_text_again_is_not_a_fix_for_a_mispronunciation(env):
    _, tools, *_, item, _ = env
    _round(tools, item); _round(tools, item)
    with pytest.raises(ToolError, match="same text again"):
        tools.finish(item, "minor", [MISSAID], [])


def test_a_change_undone_later_still_counts_as_a_fix_tried(env, monkeypatch):
    # At MAX_RECORDS = 4 the rule is off from round 3 on (two rounds must be left), so
    # this runs with more rounds: it guards the comparison should the limit be raised.
    monkeypatch.setattr(agent_tools, "MAX_RECORDS", 6)
    _, tools, *_, item, _ = env
    _round(tools, item)
    tools.replace_text(item, *A_PAUSE)
    _round(tools, item)
    tools.replace_text(item, *reversed(A_PAUSE))  # the fix made it worse
    _round(tools, item)
    assert tools.finish(item, "minor", [MISSAID], [])["ok"]


def test_minor_with_an_omission_needs_a_recording_of_any_text_after_the_first_check(env):
    _, tools, *_, item, _ = env
    _round(tools, item)
    with pytest.raises(ToolError, match="no round has been recorded since the first check"):
        tools.finish(item, "minor", [DROPPED], [])
    _round(tools, item)  # the engine doesn't always drop the same words
    assert tools.finish(item, "minor", [DROPPED], [])["ok"]


def test_a_round_after_a_failed_one_counts(env):
    _, tools, tts, _, item, _ = env
    _round(tools, item)
    tts.fail_next = True
    with pytest.raises(Exception):
        tools.record(item)
    tools.record(item); tools.check(item)  # round 3, recorded after the first check
    assert tools.finish(item, "minor", [DROPPED], [])["ok"]


def test_the_first_check_is_what_a_fix_must_follow_not_round_one(env):
    # Round 1 failed and left nothing; round 2 is the first recording a check heard.
    _, tools, tts, _, item, _ = env
    tts.fail_next = True
    with pytest.raises(Exception):
        tools.record(item)
    _round(tools, item)
    with pytest.raises(ToolError, match="no round has been recorded since the first check"):
        tools.finish(item, "minor", [DROPPED], [])


def test_a_fix_recorded_again_unchanged_still_counts_as_tried(env, monkeypatch):
    # Fixed in round 2, then the fixed text recorded again for a dropped word: the latest
    # two rounds have the same text, but round 2 tried a fix after the first check. With
    # more rounds than MAX_RECORDS = 4, for the reason in the test above.
    monkeypatch.setattr(agent_tools, "MAX_RECORDS", 6)
    _, tools, *_, item, _ = env
    _round(tools, item)
    tools.replace_text(item, *A_PAUSE)
    _round(tools, item); _round(tools, item)
    assert tools.finish(item, "minor", [MISSAID], [])["ok"]

def test_an_item_from_before_recorded_texts_is_not_refused_on_a_guess(env):
    # An item opened before the server kept recorded_texts has no record of its rounds'
    # texts, so the rule can't say truly whether a fix was tried; it isn't applied.
    store, tools, *_, item, _ = env
    _round(tools, item)
    tools.replace_text(item, *A_PAUSE)
    _round(tools, item)  # a fix tried after the first check
    state = store.load_state(item)
    del state["recorded_texts"]
    store.save_state(item, state)
    assert tools.finish(item, "minor", [MISSAID], [])["ok"]


def test_a_change_made_before_the_first_check_is_not_a_fix_tried(env):
    # The prompt has speech_warnings fixed and recorded again before a check is spent,
    # so two texts are often recorded before any check has heard a word.
    _, tools, *_, item, _ = env
    tools.record(item)
    tools.replace_text(item, *A_PAUSE)
    _round(tools, item)
    with pytest.raises(ToolError, match="try a fix first"):
        tools.finish(item, "minor", [MISSAID], [])
    with pytest.raises(ToolError, match="no round has been recorded since the first check"):
        tools.finish(item, "minor", [DROPPED], [])


def test_minor_is_accepted_once_no_record_round_is_left(env):
    _, tools, *_, item, _ = env
    for _ in range(MAX_RECORDS - 1):
        tools.record(item)
    tools.record(item); tools.check(item)  # the last round, of the same text
    assert tools.finish(item, "minor", [MISSAID, DROPPED], [])["ok"]


def test_minor_is_accepted_after_one_round_once_no_check_is_left(env):
    _, tools, *_, item, _ = env
    tools.record(item)
    for _ in range(agent_tools.MAX_CHECKS):
        tools.check(item)
    assert tools.finish(item, "minor", [MISSAID, DROPPED], [])["ok"]


def test_minor_is_accepted_when_a_forced_fix_could_not_be_undone_for_want_of_a_round(env):
    # The first check came at round 3: a fix in round 4 that went wrong (say a speech
    # mismatch) couldn't be undone, and would leave only hold.
    _, tools, *_, item, _ = env
    for _ in range(MAX_RECORDS - 2):
        tools.record(item)
    _round(tools, item)
    assert tools.finish(item, "minor", [MISSAID, DROPPED], [])["ok"]


def test_minor_is_accepted_when_a_forced_fix_could_not_be_undone_for_want_of_a_check(env):
    _, tools, *_, item, _ = env
    tools.record(item)
    for _ in range(agent_tools.MAX_CHECKS - 1):
        tools.check(item)
    assert tools.finish(item, "minor", [MISSAID, DROPPED], [])["ok"]


@pytest.mark.parametrize("outcome, issues", [
    ("minor", [{"kind": "pacing", "where": "0:04", "note": "rushed"}]),
    ("minor", [{"kind": "artifact", "where": "0:04", "note": "a click"}]),
    ("hold", [MISSAID]),  # nothing held is published
    ("hold", [DROPPED]),
])
def test_other_kinds_and_hold_need_no_fix_round(env, outcome, issues):
    _, tools, *_, item, _ = env
    _round(tools, item)
    assert tools.finish(item, outcome, issues, [])["ok"]

@settings(max_examples=100, deadline=None)
@given(old=st.text(min_size=1, max_size=40), new=st.text(max_size=40))
def test_property_replace_text_never_touches_another_item(tmp_path_factory, old, new):
    store = WorkItemStore(tmp_path_factory.mktemp("s"))
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    a, _ = store.open("alpha beta gamma", {})
    b, _ = store.open("alpha beta gamma", {})
    before = {n: store.read_text(b, n) for n in ("source.md", "prepared.md", "metadata.json", "state.json")}
    try:
        tools.replace_text(a, old, new)
    except ToolError:
        pass
    assert {n: store.read_text(b, n) for n in before} == before
    assert store.read_text(a, "source.md") == "alpha beta gamma"


@settings(max_examples=100, deadline=None)
@given(calls=st.integers(0, 12))
def test_property_engine_runs_at_most_max_records(tmp_path_factory, calls):
    store = WorkItemStore(tmp_path_factory.mktemp("s"))
    tts = FakeTTS()
    tools = AgentTools(store, tts, FakeVerifier(), FakeG2P())
    item, _ = store.open("x", {})
    for _ in range(calls):
        try:
            tools.record(item)
        except ToolError:
            pass
    assert tts.calls == min(calls, MAX_RECORDS)


# ---- every tool holds the item's lock, and a finished item takes no calls ----
def _every_tool(tools, item):
    return {"get_text": lambda: tools.get_text(item),
            "replace_text": lambda: tools.replace_text(item, "It", "It"),
            "record": lambda: tools.record(item),
            "check": lambda: tools.check(item),
            "phonemes": lambda: tools.phonemes(item, "Ivo"),
            "finish": lambda: tools.finish(item, "hold", [], [])}


def test_every_tool_is_refused_while_another_call_runs(env):
    store, tools, tts, _, item, _ = env
    started, release = threading.Event(), threading.Event()
    tts.hold = (started, release)
    running = threading.Thread(target=tools.record, args=(item,))
    running.start()
    try:
        assert started.wait(5)
        for name, call in _every_tool(tools, item).items():
            with pytest.raises(ToolError, match="in progress"):
                call()
    finally:
        release.set()
        running.join(5)
    state = store.load_state(item)
    assert (state["records"], state["recorded_round"], state["status"]) == (1, 1, "open")


def test_every_tool_refuses_a_finished_item(env):
    store, tools, _, _, item, _ = env
    tools.record(item); tools.check(item)
    tools.finish(item, "clean", [], [])
    for name, call in _every_tool(tools, item).items():
        with pytest.raises(ToolError, match="finished"):
            call()
    assert store.load_state(item)["status"] == "finished"


# ---- R1: an override's phonemes must come from a phonemes() lookup of that word ----
NAMED = "Pengelley met the family.\n\nIt went well."
FAKE = FakeG2P().phonemes("anything")


@pytest.fixture
def named(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    item, _ = store.open(NAMED, {})
    return store, tools, item


def _record_and_check(tools, item):
    tools.record(item)
    tools.check(item)


def test_an_override_from_a_lookup_of_its_own_word_passes(named):
    store, tools, item = named
    out = tools.phonemes(item, "Pengelley")
    assert out == {"word": "Pengelley", "phonemes": FAKE}
    tools.replace_text(item, "Pengelley", f"[Pengelley](/{out['phonemes']}/)")
    _record_and_check(tools, item)
    assert tools.finish(item, "clean", [], [])["ok"]


def test_an_override_from_a_respelled_lookup_needs_the_respelling_declared(named):
    store, tools, item = named
    ph = tools.phonemes(item, "Penjelly")["phonemes"]  # respelled to steer the engine
    tools.replace_text(item, "Pengelley", f"[Pengelley](/{ph}/)")
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match=re.escape(
            f"override [Pengelley](/{ph}/) uses the phonemes the phonemes tool gave for "
            "'Penjelly', a respelling of 'Pengelley': declare the respelling in substitutions "
            "(original 'Pengelley', spoken 'Penjelly')")):
        tools.finish(item, "clean", [], [])
    respelling = {"original": "Pengelley", "spoken": "Penjelly", "why": "pronunciation"}
    assert tools.finish(item, "clean", [], [respelling])["ok"]
    assert store.load_state(item)["finish"]["substitutions"] == [respelling]  # published


def test_hand_written_phonemes_are_refused(named):
    store, tools, item = named
    tools.phonemes(item, "Pengelley")  # looked up, but the override doesn't use the result
    tools.replace_text(item, "Pengelley", "[Pengelley](/pɛnʤˈɛli/)")
    _record_and_check(tools, item)
    message = ("override [Pengelley](/pɛnʤˈɛli/) was not produced by the phonemes tool for "
               "'Pengelley'; call phonemes('Pengelley') and use its result")
    with pytest.raises(ToolError, match=re.escape(message)):
        tools.finish(item, "clean", [], [])
    assert store.load_state(item)["status"] == "open"


def test_a_lookup_of_an_unrelated_word_is_refused(named):
    store, tools, item = named
    ph = tools.phonemes(item, "badly")["phonemes"]
    tools.phonemes(item, "Pengelley")
    tools.replace_text(item, "Pengelley", f"[Pengelley](/{ph}/)")  # its own lookup: fine
    tools.replace_text(item, "well.", f"[well](/{ph}/).")          # phonemes of "badly"
    _record_and_check(tools, item)
    with pytest.raises(ToolError) as refused:
        tools.finish(item, "clean", [], [])
    assert f"override [well](/{ph}/) was not produced" in str(refused.value)
    assert "Pengelley" not in str(refused.value)  # only the bad override is named


def test_a_lookup_made_for_another_item_does_not_count(named):
    store, tools, item = named
    other, _ = store.open("Pengelley spoke.", {})
    ph = tools.phonemes(other, "Pengelley")["phonemes"]
    tools.replace_text(item, "Pengelley", f"[Pengelley](/{ph}/)")
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match="not produced by the phonemes tool"):
        tools.finish(item, "clean", [], [])


def test_an_override_already_in_the_article_still_needs_a_lookup(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    item, _ = store.open("See [Home](/home/) today.", {})  # a relative link reads as an override
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match=re.escape(
            "override [Home](/home/) was not produced by the phonemes tool for 'Home'; if it is "
            "a link, not an override, unwrap it: replace [Home](/home/) with Home; otherwise "
            "call phonemes('Home') and use its result")):
        tools.finish(item, "clean", [], [])
    tools.replace_text(item, "[Home](/home/)", "Home")
    _record_and_check(tools, item)
    assert tools.finish(item, "clean", [], [])["ok"]


def test_a_multi_word_link_in_the_prepared_text_gets_the_unwrap_hint_too(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    item, _ = store.open("As [the report](/publications/x/) said.", {})  # not through open()
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match=re.escape(
            "unwrap it: replace [the report](/publications/x/) with the report")):
        tools.finish(item, "clean", [], [])
    tools.replace_text(item, "[the report](/publications/x/)", "the report")
    _record_and_check(tools, item)
    assert tools.finish(item, "clean", [], [])["ok"]


def test_lookups_are_kept_in_state_across_tool_calls(named):
    store, tools, item = named
    tools.phonemes(item, "Pengelly")
    tools.get_text(item)
    tools.replace_text(item, "It went", "It went")
    _record_and_check(tools, item)
    with pytest.raises(ToolError):
        tools.finish(item, "great", [], [])
    assert store.load_state(item)["phoneme_lookups"] == [{"word": "Pengelly", "phonemes": FAKE}]


def test_only_the_last_200_lookups_are_kept(named):
    store, tools, item = named
    spelled = str.maketrans("0123456789", "abcdefghij")  # phonemes() takes letters only
    for k in range(205):
        tools.phonemes(item, "word" + str(k).translate(spelled))
    lookups = store.load_state(item)["phoneme_lookups"]
    assert len(lookups) == 200
    assert (lookups[0]["word"], lookups[-1]["word"]) == ("wordf", "wordcae")  # 5 and 204


# ---- R2: size caps ----
def test_replace_text_refuses_a_prepared_text_far_longer_than_the_article(env):
    store, tools, _, _, item, _ = env
    limit = 2 * len(ARTICLE) + 10_000
    room = limit - len(ARTICLE)  # how far the prepared text may still grow
    with pytest.raises(ToolError, match=f"the limit is {limit}"):
        tools.replace_text(item, "well.", "well." + "x" * (room + 1))
    assert store.read_text(item, "prepared.md") == ARTICLE
    tools.replace_text(item, "well.", "well." + "x" * room)  # exactly at the limit
    assert len(store.read_text(item, "prepared.md")) == limit
    with pytest.raises(ToolError, match=f"the limit is {limit}"):
        tools.replace_text(item, "met", "met!")


def test_replace_text_refuses_old_or_new_over_20000_characters(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    article = "word " * 6000 + "end."  # long enough that a 20,001-character edit fits the size cap
    item, _ = store.open(article, {})
    with pytest.raises(ToolError, match="old is longer than 20000 characters"):
        tools.replace_text(item, "word " * 4001, "x")
    with pytest.raises(ToolError, match="new is longer than 20000 characters"):
        tools.replace_text(item, "end.", "y" * 20_001)
    assert store.read_text(item, "prepared.md") == article
    tools.replace_text(item, "end.", "y" * 20_000)  # at the cap


def _counting_audit(calls):
    def audit(source, prepared, subs):
        calls.append(subs)
        return [], {s: 1 for s in subs}
    return audit


def test_finish_refuses_too_many_substitutions_before_the_word_check(env, monkeypatch):
    _, tools, _, _, item, _ = env
    _record_and_check(tools, item)
    calls = []
    monkeypatch.setattr(agent_tools, "audit", _counting_audit(calls))
    monkeypatch.setattr("narration.gate.speech_matches", lambda *args: [])  # a later stage
    sub = {"original": "XIV", "spoken": "the Fourteenth", "why": "numeral"}
    for n in (agent_tools.MAX_SUBSTITUTIONS + 1, 10_000):
        with pytest.raises(ToolError, match="at most 25 substitutions"):
            tools.finish(item, "clean", [], [sub] * n)
    assert calls == []
    distinct = [{"original": f"w{k}", "spoken": f"v{k}"} for k in range(agent_tools.MAX_SUBSTITUTIONS)]
    assert tools.finish(item, "clean", [], distinct)["ok"]
    assert len(calls) == 1  # the limit itself reaches the check


def test_finish_refuses_too_many_issues_before_the_word_check(env, monkeypatch):
    _, tools, _, _, item, _ = env
    _record_and_check(tools, item)
    calls = []
    monkeypatch.setattr(agent_tools, "audit", _counting_audit(calls))
    ok = {"kind": "pacing", "where": "1:05", "note": ""}
    for n in (agent_tools.MAX_ISSUES + 1, 10_000):
        with pytest.raises(ToolError, match="at most 20"):
            tools.finish(item, "minor", [ok] * n, [])
    assert calls == []


# ---- arguments the schema should have stopped, refused all the same ----
def test_record_refuses_malformed_voices_and_speeds_without_counting_a_round(env):
    store, tools, tts, _, item, _ = env
    for voice in ("af_heart\n", "AF_HEART", "af_", 7, 0, False, ""):
        with pytest.raises(ToolError, match="voice"):
            tools.record(item, voice=voice)
    for speed in ("fast", None, float("nan"), float("inf"), 0.4):
        with pytest.raises(ToolError, match="speed"):
            tools.record(item, speed=speed)
    assert (store.load_state(item)["records"], tts.calls) == (0, 0)


def test_replace_text_refuses_non_text(env):
    store, tools, _, _, item, _ = env
    with pytest.raises(ToolError):
        tools.replace_text(item, 5, "x")
    with pytest.raises(ToolError):
        tools.replace_text(item, "It", None)
    with pytest.raises(ToolError, match="empty"):
        tools.replace_text(item, "", "x")
    assert store.read_text(item, "prepared.md") == ARTICLE


@pytest.mark.parametrize("where", ["12:40\n", "12:60", "\u0661\u0662:\u0664\u0660", "12:4", ":40", 1240])
def test_finish_refuses_a_where_that_is_not_a_plain_timestamp(env, where):
    _, tools, _, _, item, _ = env
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match="timestamp"):
        tools.finish(item, "minor", [{"kind": "pacing", "where": where, "note": ""}], [])


def test_finish_refuses_issues_and_substitutions_that_are_not_objects(env):
    store, tools, _, _, item, _ = env
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match="object"):
        tools.finish(item, "minor", ["12:40 garbled"], [])
    with pytest.raises(ToolError, match="object"):
        tools.finish(item, "clean", [], [["XIV", "the Fourteenth"]])
    with pytest.raises(ToolError, match="list"):
        tools.finish(item, "clean", [], "XIV")
    with pytest.raises(ToolError, match="original"):
        tools.finish(item, "clean", [], [{"spoken": "the Fourteenth"}])
    assert store.load_state(item)["status"] == "open"


def test_replace_text_counts_overlapping_matches_as_more_than_one(env):
    store, tools, _, _, item, _ = env
    store.write_text(item, "prepared.md", "baaa")
    with pytest.raises(ToolError, match="found 2"):  # str.count finds "aa" once
        tools.replace_text(item, "aa", "x")
    assert store.read_text(item, "prepared.md") == "baaa"
    tools.replace_text(item, "baa", "bxa")
    assert store.read_text(item, "prepared.md") == "bxaa"


def test_a_refusal_names_at_most_five_unproven_overrides(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    names = ["Abbott", "Barlow", "Carver", "Dobson", "Ellery", "Fenwick", "Garrow"]
    item, _ = store.open(" ".join(names) + " met.", {})
    for name in names:
        tools.replace_text(item, name, f"[{name}](/{FAKE}/)")
    _record_and_check(tools, item)
    with pytest.raises(ToolError) as refused:
        tools.finish(item, "clean", [], [])
    message = str(refused.value)
    assert message.count("was not produced by the phonemes tool") == 5
    assert message.endswith("; and more overrides like these")
    assert "Fenwick" not in message and "Garrow" not in message


def test_finish_refuses_minor_with_precheck_warnings_and_needs_the_latest_check_clear(env):
    store, tools, _, ver, item, _ = env
    issue = {"kind": "truncation", "where": "9:40", "note": "cut short"}
    ver.warnings = ["0:07 of silence at 3:10 — far longer than a [BREAK] pause."]
    tools.record(item); tools.check(item)
    with pytest.raises(ToolError, match="pre-check flagged: 0:07 of silence"):
        tools.finish(item, "minor", [issue], [])
    ver.warnings = []
    tools.record(item)
    with pytest.raises(ToolError, match="latest recording"):  # round 1's check doesn't count
        tools.finish(item, "minor", [issue], [])
    tools.check(item)
    assert tools.finish(item, "minor", [issue], [])["ok"]
    assert store.load_state(item)["finish"]["remaining_issues"] == [issue]



# ---- fix round 1 ----
ISSUE = {"kind": "truncation", "where": "9:40", "note": "cut short"}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# C1: the claims are about the text that was recorded, not the text as it is now
def test_record_keeps_the_hash_of_the_text_it_recorded(env):
    store, tools, tts, _, item, _ = env
    tools.record(item)
    assert store.load_state(item)["recorded_sha256"] == _sha(ARTICLE)
    tools.replace_text(item, "It went well.", "It went well...")
    tts.fail_next = True
    with pytest.raises(RuntimeError):
        tools.record(item)
    assert store.load_state(item)["recorded_sha256"] == _sha(ARTICLE)  # round 1's text
    tools.record(item)
    assert store.load_state(item)["recorded_sha256"] == _sha(ARTICLE.replace("well.", "well..."))


def test_check_refuses_text_edited_since_the_recording(env):
    store, tools, _, _, item, _ = env
    tools.replace_text(item, "It went well.", "It went well. Buy pills today.")
    tools.record(item)  # the audio says "Buy pills today"
    tools.replace_text(item, "It went well. Buy pills today.", "It went well.")  # the article again
    message = "the prepared text changed since round 1 was recorded; record again, or undo the edit"
    with pytest.raises(ToolError, match=re.escape(message)):
        tools.check(item)
    with pytest.raises(ToolError, match=re.escape(message)):
        tools.finish(item, "clean", [], [])
    assert "checks_used" not in store.load_state(item)  # the refusal used no check


def test_finish_refuses_text_edited_between_check_and_finish(env):
    store, tools, _, _, item, _ = env
    tools.replace_text(item, "It went well.", "It went well. Buy pills today.")
    tools.record(item); tools.check(item)
    tools.replace_text(item, "It went well. Buy pills today.", "It went well.")
    with pytest.raises(ToolError, match="changed since round 1 was recorded"):
        tools.finish(item, "clean", [], [])
    tools.record(item); tools.check(item)  # recording again makes the claims true
    assert tools.finish(item, "clean", [], [])["ok"]


def test_undoing_the_edit_restores_the_recording(env):
    store, tools, _, _, item, _ = env
    tools.record(item)
    tools.replace_text(item, "It went well.", "It went well...")
    with pytest.raises(ToolError, match="changed since round 1"):
        tools.check(item)
    tools.replace_text(item, "It went well...", "It went well.")
    tools.check(item)
    assert tools.finish(item, "clean", [], [])["ok"]


# I3 and M6: hold is always accepted; outcomes and issues agree
def test_hold_is_accepted_with_nothing_recorded_and_still_validates_shapes(env):
    store, tools, _, _, item, _ = env
    with pytest.raises(ToolError, match="timestamp"):
        tools.finish(item, "hold", [{**ISSUE, "where": "soon"}], [])
    with pytest.raises(ToolError, match="at most 25 substitutions"):
        tools.finish(item, "hold", [ISSUE], [{"original": "a", "spoken": "b"}] * 26)
    assert tools.finish(item, "hold", [ISSUE], [])["ok"]
    assert store.load_state(item)["finish"]["outcome"] == "hold"


def test_hold_skips_the_check_and_word_requirements(env):
    store, tools, _, _, item, _ = env
    tools.replace_text(item, "well", "badly")  # undeclared, and never checked
    tools.record(item)
    assert tools.finish(item, "hold", [ISSUE], [])["ok"]


def test_outcomes_and_remaining_issues_must_agree(env):
    store, tools, _, _, item, _ = env
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match="use 'minor'"):
        tools.finish(item, "clean", [ISSUE], [])
    for outcome in ("minor", "hold"):
        with pytest.raises(ToolError, match="at least one remaining issue"):
            tools.finish(item, outcome, [], [])
    assert store.load_state(item)["status"] == "open"


def test_when_the_last_record_fails_the_messages_offer_hold(env):
    store, tools, tts, _, item, _ = env
    for _ in range(MAX_RECORDS - 1):
        tools.record(item)
    tools.check(item)
    tts.fail_next = True
    with pytest.raises(RuntimeError):
        tools.record(item)
    for call in (lambda: tools.check(item), lambda: tools.finish(item, "clean", [], []),
                 lambda: tools.record(item)):
        with pytest.raises(ToolError) as refused:
            call()
        message = str(refused.value)
        assert "finish with outcome 'hold'" in message
        assert "record again" not in message and "clean or minor" not in message
    assert tools.finish(item, "hold", [ISSUE], [])["ok"]


def test_when_checks_run_out_before_a_new_recording_the_messages_offer_hold(env):
    store, tools, _, ver, item, _ = env
    tools.record(item)
    for _ in range(agent_tools.MAX_CHECKS - 1):
        tools.check(item)
    tools.record(item)
    real, ver.check = ver.check, lambda audio_path, text: (_ for _ in ()).throw(
        RuntimeError("verification failed: 503 UNAVAILABLE"))
    with pytest.raises(RuntimeError):
        tools.check(item)  # the last check, counted, and round 2 is never checked
    ver.check = real
    with pytest.raises(ToolError, match=r"check limit reached \(8 per article\); "
                                        r"finish with outcome 'hold'$"):
        tools.check(item)
    with pytest.raises(ToolError, match=r"no checks are left to verify a new recording "
                                        r"\(8 per article\); finish with outcome 'hold'$"):
        tools.record(item)
    with pytest.raises(ToolError, match="no checks are left; finish with outcome 'hold'"):
        tools.finish(item, "clean", [], [])
    assert tools.finish(item, "hold", [ISSUE], [])["ok"]


# M1 (final review): with no checks left, a new recording could never be checked, so
# record is refused rather than replacing a round that was checked clean
def test_record_is_refused_once_no_checks_are_left_and_the_checked_round_stands(env):
    store, tools, tts, _, item, _ = env
    tools.record(item)
    for _ in range(agent_tools.MAX_CHECKS):
        tools.check(item)
    with pytest.raises(ToolError, match=re.escape(
            "no checks are left to verify a new recording (8 per article); finish with "
            "outcome clean or minor; or finish with outcome 'hold'")):
        tools.record(item)
    assert store.load_state(item)["records"] == 1 and tts.calls == 1  # no round spent
    assert tools.finish(item, "clean", [], [])["ok"]  # round 1, checked clean


def test_an_edit_with_no_checks_left_is_not_told_to_record_again(env):
    store, tools, _, _, item, _ = env
    tools.record(item)
    for _ in range(agent_tools.MAX_CHECKS):
        tools.check(item)
    tools.replace_text(item, "It went well.", "It went well...")
    with pytest.raises(ToolError) as refused:
        tools.finish(item, "clean", [], [])
    assert str(refused.value) == (
        "the prepared text changed since round 1 was recorded; undo the edit (no checks are "
        "left to verify a new recording), or finish with outcome 'hold'")
    with pytest.raises(ToolError, match=re.escape(
            "no checks are left to verify a new recording (8 per article); undo the edits "
            "made since round 1 was recorded, then finish with outcome clean or minor; or "
            "finish with outcome 'hold'")):
        tools.record(item)
    tools.replace_text(item, "It went well...", "It went well.")
    assert tools.finish(item, "clean", [], [])["ok"]


@pytest.mark.parametrize("records, checks, advice", [
    (1, 1, "Fix and re-record, or finish with outcome 'hold'."),
    (MAX_RECORDS, 1, "No record round is left to fix it: finish with outcome 'hold'."),
    (1, agent_tools.MAX_CHECKS, "No check is left to verify a fix: finish with outcome 'hold'."),
    (MAX_RECORDS, agent_tools.MAX_CHECKS,
     "No record round or check is left to fix it: finish with outcome 'hold'."),
])
def test_the_precheck_refusal_says_which_limit_is_spent(records, checks, advice):
    state = {"records": records, "recorded_round": records, "recorded_sha256": _sha(ARTICLE),
             "checks_used": checks,
             "checks": [{"n": checks, "record": records, "warnings": ["pace of 40 WPM"]}]}
    assert agent_tools.recording_claims(state, ARTICLE) == [
        f"the latest check's pre-check flagged: pace of 40 WPM. {advice}"]


def test_the_limit_messages_say_what_can_still_be_done(env):
    store, tools, _, _, item, _ = env
    for _ in range(MAX_RECORDS):
        tools.record(item)
    with pytest.raises(ToolError, match=re.escape(
            "record limit reached (4 per article); call check on round 4, then finish with "
            "outcome clean or minor; or finish with outcome 'hold'")):
        tools.record(item)
    tools.replace_text(item, "It went well.", "It went well...")
    with pytest.raises(ToolError, match=re.escape(
            "undo the edit (no record rounds are left), or finish with outcome 'hold'")):
        tools.check(item)
    with pytest.raises(ToolError, match=re.escape(
            "record limit reached (4 per article); undo the edits made since round 4 was "
            "recorded, then call check on round 4, then finish with outcome clean or minor; "
            "or finish with outcome 'hold'")):
        tools.record(item)
    tools.replace_text(item, "It went well...", "It went well.")
    tools.check(item)
    with pytest.raises(ToolError, match=re.escape(
            "record limit reached (4 per article); finish with outcome clean or minor; "
            "or finish with outcome 'hold'")):
        tools.record(item)


# M2: a check is counted before the verifier runs
def test_a_failed_check_still_counts(env):
    store, tools, _, ver, item, _ = env
    tools.record(item)

    def down(audio_path, text):
        raise RuntimeError("verification failed: 503 UNAVAILABLE")

    ver.check = down
    for _ in range(agent_tools.MAX_CHECKS):
        with pytest.raises(RuntimeError):
            tools.check(item)
    state = store.load_state(item)
    assert (state["checks_used"], state["checks"]) == (agent_tools.MAX_CHECKS, [])
    with pytest.raises(ToolError, match="check limit reached"):
        tools.check(item)
    with pytest.raises(ToolError, match="latest recording"):  # a failed check proves nothing
        tools.finish(item, "clean", [], [])


def test_check_numbers_reports_by_attempt(env):
    store, tools, _, ver, item, _ = env
    tools.record(item)
    real = ver.check
    ver.check = lambda audio_path, text: (_ for _ in ()).throw(RuntimeError("down"))
    with pytest.raises(RuntimeError):
        tools.check(item)
    ver.check = real
    tools.check(item)
    assert store.read_text(item, "report-2.txt") == "No issues found."
    assert store.load_state(item)["checks"] == [{"n": 2, "record": 1, "warnings": []}]


# M3, M4: unknown items, closed items, readable messages
def test_an_unknown_item_is_a_value_error_without_a_path(env):
    store, tools, *_ = env
    for name, call in _every_tool(tools, "0123456789ab").items():
        with pytest.raises(ValueError, match="no such work item") as refused:
            call()
        assert str(store.root) not in str(refused.value)


def test_a_closed_item_names_its_status(env):
    store, tools, _, _, item, _ = env
    state = store.load_state(item)
    state["status"] = "abandoned"
    store.save_state(item, state)
    with pytest.raises(ToolError, match="this work item is abandoned"):
        tools.get_text(item)


def test_the_precheck_refusal_reads_cleanly(env):
    store, tools, _, ver, item, _ = env
    ver.warnings = ["0:07 of silence at 3:10 — far longer than a [BREAK] pause."]
    _record_and_check(tools, item)
    with pytest.raises(ToolError) as refused:
        tools.finish(item, "minor", [ISSUE], [])
    assert "pause.." not in str(refused.value)
    assert "far longer than a [BREAK] pause. Fix and re-record" in str(refused.value)



# C2: phonemes() takes one word, never link syntax that misaki's preprocessing reads
@pytest.fixture
def linked(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeLinkG2P())
    item, _ = store.open(NAMED, {})
    return store, tools, item


@pytest.mark.parametrize("word", [
    "[Pengelley](/kˈɪlðəmˈɔl/)", "Pen gelley", "Pengelley\n", "Pen\tgelley", "x/y", "(x)",
    "[x]", "1990s", "", "...", "'-.", "a" * 65, 7, None])
def test_phonemes_refuses_anything_but_one_word(linked, word):
    store, tools, item = linked
    with pytest.raises(ToolError, match="a single word"):
        tools.phonemes(item, word)
    assert "phoneme_lookups" not in store.load_state(item)


@pytest.mark.parametrize("word", ["Pengelley", "Pengelley's", "Pengelley’s", "Obrano-Corvan",
                                  "U.S.", "nai\u0308ve", "Penjelly", "a" * 64])
def test_phonemes_accepts_a_word_with_apostrophes_hyphens_periods_and_marks(linked, word):
    store, tools, item = linked
    assert tools.phonemes(item, word) == {"word": word, "phonemes": FAKE}


def test_an_override_cannot_launder_its_own_phonemes_through_the_lookup(linked):
    store, tools, item = linked
    with pytest.raises(ToolError):
        tools.phonemes(item, "[Pengelley](/kˈɪlðəmˈɔl/)")  # FakeLinkG2P would echo them back
    tools.replace_text(item, "Pengelley", "[Pengelley](/kˈɪlðəmˈɔl/)")
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match="not produced by the phonemes tool"):
        tools.finish(item, "clean", [], [])


# I1: a respelled lookup changes the reading only when declared, and is published
class SpellingG2P(FakeG2P):
    """Phonemes that differ per word, so a test can tell lookups apart."""
    def phonemes(self, word):
        return "ˈ" + word.lower()


@pytest.fixture
def spelled(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SpellingG2P())
    item, _ = store.open("The plan was rejected today.", {})
    return store, tools, item


def test_a_respelling_that_changes_the_meaning_must_be_declared(spelled):
    store, tools, item = spelled
    ph = tools.phonemes(item, "respected")["phonemes"]
    tools.replace_text(item, "rejected", f"[rejected](/{ph}/)")
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match="declare the respelling in substitutions "
                                        r"\(original 'rejected', spoken 'respected'\)"):
        tools.finish(item, "clean", [], [])
    sub = {"original": "rejected", "spoken": "respected", "why": "pronunciation"}
    assert tools.finish(item, "clean", [], [sub])["ok"]  # allowed, but now in the notes
    assert store.load_state(item)["finish"]["substitutions"] == [sub]


def test_a_declared_respelling_must_match_the_lookup_it_names(spelled):
    store, tools, item = spelled
    ph = tools.phonemes(item, "respected")["phonemes"]
    tools.phonemes(item, "rejected")
    tools.replace_text(item, "rejected", f"[rejected](/{ph}/)")
    _record_and_check(tools, item)
    other = {"original": "rejected", "spoken": "reflected", "why": "pronunciation"}
    with pytest.raises(ToolError, match="respelling of 'rejected'"):
        tools.finish(item, "clean", [], [other])


def test_a_lookup_in_other_capitals_needs_the_change_declared(spelled):
    # Review F9: misaki reads capitals differently (US/us, IT/it, Polish/polish), so a
    # lookup in other capitals proves an override only when that change is declared.
    store, tools, item = spelled
    ph = tools.phonemes(item, "REJECTED")["phonemes"]
    tools.replace_text(item, "rejected", f"[rejected](/{ph}/)")
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match=re.escape(
            "declare the respelling in substitutions (original 'rejected', spoken 'REJECTED')")):
        tools.finish(item, "clean", [], [])
    sub = {"original": "rejected", "spoken": "REJECTED", "why": "pronunciation"}
    assert tools.finish(item, "clean", [], [sub])["ok"]
    assert store.load_state(item)["finish"]["substitutions"] == [sub]  # published


def test_a_lookup_of_the_displayed_word_needs_no_declaration(spelled):
    store, tools, item = spelled
    ph = tools.phonemes(item, "rejected")["phonemes"]
    tools.replace_text(item, "rejected", f"[rejected](/{ph}/)")
    _record_and_check(tools, item)
    assert tools.finish(item, "clean", [], [])["ok"]


def test_a_second_override_in_other_capitals_is_checked_too(tmp_path):
    # The proven [us] override must not vouch for [US] with the same phonemes.
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SpellingG2P())
    item, _ = store.open("Let us go. They told US so.", {})
    ph = tools.phonemes(item, "us")["phonemes"]
    store.write_text(item, "prepared.md", f"Let [us](/{ph}/) go. They told [US](/{ph}/) so.")
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match=re.escape("override [US]")):
        tools.finish(item, "clean", [], [])


# The first real session (2026-11-01) respelled three surnames as the instructions say
# (look up the respelling, override with its phonemes, declare it) and finish refused
# every one: provenance used the budget's strict respelling rule (6+ letters, one plain
# word), so 5-letter names and possessives could never be proven.
NAMES = "Zounk spoke to Zowen. Then LindeHei agreed with Zounk's plan, but the form was late."


@pytest.fixture
def names(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SpellingG2P())
    item, _ = store.open(NAMES, {})
    return store, tools, item


def _respell(tools, item, old, shown, spoken):
    """What the session did: look up `spoken`, override `shown` (inside `old`, which
    appears once) with its phonemes, and return the declaration finish needs."""
    ph = tools.phonemes(item, spoken)["phonemes"]
    tools.replace_text(item, old, old.replace(shown, f"[{shown}](/{ph}/)"))
    return {"original": shown, "spoken": spoken, "why": "pronunciation"}


@pytest.mark.parametrize("old, shown, spoken", [
    ("Zounk spoke", "Zounk", "Zunk"),            # a letter dropped, 5 letters
    ("to Zowen", "Zowen", "Zowan"),              # a vowel changed, 5 letters
    ("LindeHei", "LindeHei", "Lindahigh"),       # a longer name, respelled more loosely
    ("Zounk's plan", "Zounk's", "Zunk's"),       # a possessive
])
def test_a_declared_respelling_of_a_name_proves_its_override(names, old, shown, spoken):
    store, tools, item = names
    sub = _respell(tools, item, old, shown, spoken)
    _record_and_check(tools, item)
    assert tools.finish(item, "clean", [], [sub])["ok"]
    assert store.load_state(item)["finish"]["substitutions"] == [sub]  # published


def test_an_undeclared_respelling_of_a_name_is_told_to_declare_it(names):
    store, tools, item = names
    _respell(tools, item, "Zounk spoke", "Zounk", "Zunk")
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match=re.escape(
            "a respelling of 'Zounk': declare the respelling in substitutions "
            "(original 'Zounk', spoken 'Zunk')")):
        tools.finish(item, "clean", [], [])


@pytest.mark.parametrize("article, old, shown, spoken", [
    # Review F2: the session capitalises a lowercase article word to get the name rule.
    ("Three people were found dead today.", "dead", "Dead", "Deed"),
    # A word the article capitalises only where a sentence starts is not a name.
    ("Three left early. Three stayed late.", "Three left", "Three", "Tree"),
])
def test_only_the_articles_own_names_get_the_name_rule(tmp_path, article, old, shown, spoken):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SpellingG2P())
    item, _ = store.open(article, {})
    ph = tools.phonemes(item, spoken)["phonemes"]
    tools.replace_text(item, old, old.replace(old.split()[0], f"[{shown}](/{ph}/)", 1))
    _record_and_check(tools, item)
    sub = {"original": shown, "spoken": spoken, "why": "pronunciation"}
    with pytest.raises(ToolError, match=re.escape(f"override [{shown}]")):
        tools.finish(item, "clean", [], [sub])


@pytest.mark.parametrize("old, shown, spoken", [
    ("the form", "form", "from"),                # not a name: short words stay strict
    ("to Zowen", "Zowen", "Kowen"),              # another first letter
    ("to Zowen", "Zowen", "Zebra"),              # not alike
    ("Zounk's plan", "Zounk's", "Zunk"),         # the possessive dropped on one side
])
def test_the_name_rule_still_refuses_other_words(names, old, shown, spoken):
    store, tools, item = names
    sub = _respell(tools, item, old, shown, spoken)
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match=re.escape(f"override [{shown}]")):
        tools.finish(item, "clean", [], [sub])
    assert store.load_state(item)["status"] == "open"


def _respell_everywhere(n_over_cap):
    """An article of one sentence repeated, every 'positive' overridden with the
    phonemes of 'Punitive' (a respelling by the strict rule) under one declaration."""
    from narration.words import MAX_RESPELLINGS
    n = MAX_RESPELLINGS + n_over_cap
    article = "The test was positive. " * n
    return n, article


@pytest.mark.parametrize("n_over_cap, refused", [(0, False), (1, True)])
def test_override_respellings_count_toward_the_respellings_cap(tmp_path, n_over_cap, refused):
    # Review F1: applied 0 times by audit, an override respelling escaped every budget,
    # so one declaration could change a word at every place it appears.
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SpellingG2P())
    n, article = _respell_everywhere(n_over_cap)
    item, _ = store.open(article, {})
    ph = tools.phonemes(item, "Punitive")["phonemes"]
    store.write_text(item, "prepared.md", article.replace("positive", f"[positive](/{ph}/)"))
    _record_and_check(tools, item)
    sub = {"original": "positive", "spoken": "Punitive", "why": "pronunciation"}
    if not refused:
        assert tools.finish(item, "clean", [], [sub])["ok"]
        return
    with pytest.raises(ToolError, match=re.escape(
            f"respellings are applied {n} times in all, {n} of them through overrides")):
        tools.finish(item, "clean", [], [sub])


def test_a_lookup_too_far_from_the_word_is_named_in_the_refusal(names):
    # The real session was told its phonemes were "not produced by the phonemes tool"
    # when they were: the lookup was only too far from the word to count as a respelling.
    store, tools, item = names
    sub = _respell(tools, item, "the form", "form", "from")
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match=re.escape(
            "override [form](/ˈfrom/) uses the phonemes the phonemes tool gave for 'from', "
            "which is too far from 'form' to count as a respelling of it; call "
            "phonemes('form') and use its result, or write 'from' in the text with "
            "replace_text and declare it as a substitution (original 'form', spoken 'from')")):
        tools.finish(item, "clean", [], [sub])


def test_a_declared_change_of_capitals_proves_its_override(tmp_path):
    # The real session overrode an all-capitals name with the phonemes of its ordinary
    # capitalisation and declared it, which finish refused as "changes nothing". With
    # F9 that declaration is what proves the override, and it is published.
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SpellingG2P())
    item, _ = store.open("The firm ZORVA grew.", {})
    sub = _respell(tools, item, "ZORVA", "ZORVA", "Zorva")
    _record_and_check(tools, item)
    assert tools.finish(item, "clean", [], [sub])["ok"]
    assert store.load_state(item)["finish"]["substitutions"] == [sub]


def test_a_change_of_capitals_no_override_uses_is_refused(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SpellingG2P())
    item, _ = store.open("The firm ZORVA grew.", {})
    _record_and_check(tools, item)
    sub = {"original": "ZORVA", "spoken": "Zorva", "why": "pronunciation"}
    with pytest.raises(ToolError, match=re.escape(
            "substitution 'ZORVA' -> 'Zorva' is declared but the prepared text does not use it")):
        tools.finish(item, "clean", [], [sub])


@pytest.mark.parametrize("original, spoken", [
    ("Zorva", "ZORVA"),    # reversed
    ("zorva", "zOrva"),    # neither side's capitals
    ("ZORVA", "zorva"),    # the lookup's capitals wrong
])
def test_a_change_of_capitals_must_name_the_capitals_used(tmp_path, original, spoken):
    # Review round 2, G5: the declaration was matched case-folded, so the published
    # notes could name a capitalisation that was never written or looked up.
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SpellingG2P())
    item, _ = store.open("The firm ZORVA grew.", {})
    _respell(tools, item, "ZORVA", "ZORVA", "Zorva")  # [ZORVA] with the phonemes of Zorva
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match=re.escape("override [ZORVA]")):
        tools.finish(item, "clean", [], [{"original": original, "spoken": spoken, "why": "x"}])


def test_an_unrelated_lookup_gets_no_hint_to_write_it_into_the_text(names):
    # Only a near miss (a plausible respelling) is explained; an unrelated word with
    # the same first letter still gets the plain refusal, never a suggestion to use it.
    store, tools, item = names
    _respell(tools, item, "Zounk spoke", "Zounk", "Zillionaire")
    _record_and_check(tools, item)
    with pytest.raises(ToolError) as refused:
        tools.finish(item, "clean", [], [])
    assert "was not produced by the phonemes tool for 'Zounk'" in str(refused.value)
    assert "Zillionaire" not in str(refused.value)


# I2: every declared substitution is used, and declared once
def test_a_declared_substitution_the_text_does_not_use_is_refused(env):
    store, tools, _, _, item, _ = env
    _record_and_check(tools, item)
    unused = {"original": "King Ivo XIV", "spoken": "buy pills now", "why": "x"}
    with pytest.raises(ToolError, match=re.escape(
            "substitution 'King Ivo XIV' -> 'buy pills now' is declared but the prepared text "
            "does not use it")):
        tools.finish(item, "clean", [], [unused])
    assert store.load_state(item)["status"] == "open"


def test_a_substitution_declared_twice_is_refused(env):
    store, tools, _, _, item, _ = env
    tools.replace_text(item, "XIV", "the Fourteenth")
    _record_and_check(tools, item)
    sub = {"original": "XIV", "spoken": "the Fourteenth", "why": "numeral"}
    for twice in ([sub, sub], [sub, {**sub, "spoken": "The Fourteenth", "why": "again"}]):
        with pytest.raises(ToolError, match="declared twice"):
            tools.finish(item, "clean", [], twice)
    assert tools.finish(item, "clean", [], [sub])["ok"]


# M1: proven overrides cost a dictionary lookup each, not a respelling scan
def test_proven_overrides_need_no_respelling_comparisons(tmp_path, monkeypatch):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    names = [f"Name{chr(97 + k // 26)}{chr(97 + k % 26)}" for k in range(100)]
    item, _ = store.open(" ".join(names) + ".", {})
    for name in names:  # 200 lookups (each capitalisation proves only itself, F9),
        tools.phonemes(item, name)  # all with the same phonemes (the worst case)
        tools.phonemes(item, name.upper())
    prepared = " ".join(f"[{n}](/{FAKE}/) [{n.upper()}](/{FAKE}/)" for n in names) + "."
    store.write_text(item, "source.md", " ".join(f"{n} {n}" for n in names) + ".")
    store.write_text(item, "prepared.md", prepared)
    _record_and_check(tools, item)
    respellings, word_calls = [], []
    real_words = agent_tools.words
    monkeypatch.setattr(agent_tools, "is_override_respelling",
                        lambda *args: respellings.append(args))
    monkeypatch.setattr(agent_tools, "words", lambda t: word_calls.append(t) or real_words(t))
    assert tools.finish(item, "clean", [], [])["ok"]
    assert respellings == []
    assert len(word_calls) <= 4 * len(names)  # once per lookup, once per display as written



# R3 (Task 5): finish compares what the engine would say, so the agent learns before finalize
class SignG2P(FakeG2P):
    """FakeG2P that voices a minus before a digit even after dots, as misaki does."""
    def phonemes_for_text(self, text):
        return super().phonemes_for_text(re.sub(r"-(?=\d)", " minus ", text))


def test_finish_refuses_a_text_the_engine_would_read_differently(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SignG2P())
    item, _ = store.open("Costs fell 5% today.", {})
    tools.replace_text(item, "fell 5%", "fell ..-5%")  # the word check reads "5%"
    _record_and_check(tools, item)
    with pytest.raises(ToolError, match=r"would not sound like the article.*minus"):
        tools.finish(item, "clean", [], [])
    assert store.load_state(item)["status"] == "open"
    assert tools.finish(item, "hold", [ISSUE], [])["ok"]  # hold needs no claims


def test_finish_runs_the_speech_comparison_with_its_g2p(tmp_path, monkeypatch):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    item, _ = store.open("Costs fell 5% today.", {})
    _record_and_check(tools, item)
    compared = []
    monkeypatch.setattr("narration.gate.speech_matches",
                        lambda *args: compared.append(args) or [])
    assert tools.finish(item, "clean", [], [])["ok"]
    assert compared == [(tools.g2p, "Costs fell 5% today.", "Costs fell 5% today.", [])]


# I4 (Task 5 fix round 1): record warns, before the engine runs, about speech the
# article doesn't have; it refuses nothing and counts nothing extra
def test_record_warns_about_a_sign_only_change_and_still_records(tmp_path):
    store = WorkItemStore(tmp_path)
    tts = FakeTTS()
    tools = AgentTools(store, tts, FakeVerifier(), SignG2P())
    item, _ = store.open("Costs fell 5% today.", {})
    out = tools.record(item)
    assert out["speech_warnings"] == []
    tools.replace_text(item, "fell 5%", "fell ..-5%")
    out = tools.record(item)
    assert out["round"] == 2 and out["records_left"] == MAX_RECORDS - 2 and tts.calls == 2
    assert store.path(item, "recording.wav").exists()
    assert len(out["speech_warnings"]) == 1 and "minus" in out["speech_warnings"][0]
    assert "phonetic override" in out["speech_warnings"][0]


def test_record_says_it_did_not_compare_word_changes_finish_will_judge(tmp_path):
    from narration.gate import NOT_COMPARED
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SignG2P())
    item, _ = store.open("King Ivo XIV met.", {})
    tools.replace_text(item, "XIV", "the Fourteenth")  # declared only at finish
    assert tools.record(item)["speech_warnings"] == [NOT_COMPARED]


def test_record_records_whatever_the_advisory_raises(tmp_path):
    class BuggyG2P(FakeG2P):
        def phonemes_for_text(self, text):
            raise KeyError("a bug")
    store = WorkItemStore(tmp_path)
    tts = FakeTTS()
    tools = AgentTools(store, tts, FakeVerifier(), BuggyG2P())
    item, _ = store.open("Costs fell 5% today.", {})
    out = tools.record(item)
    assert tts.calls == 1 and out["round"] == 1 and len(out["speech_warnings"]) == 1
    assert "could not run" in out["speech_warnings"][0]


def test_record_warns_when_the_comparison_cannot_run_and_records_anyway(tmp_path):
    class BrokenG2P(FakeG2P):
        def phonemes_for_text(self, text):
            raise RuntimeError("the speech comparison failed: espeak is missing")
    store = WorkItemStore(tmp_path)
    tts = FakeTTS()
    tools = AgentTools(store, tts, FakeVerifier(), BrokenG2P())
    item, _ = store.open("Costs fell 5% today.", {})
    out = tools.record(item)
    assert tts.calls == 1 and out["speech_warnings"] == [
        "the speech comparison could not run: the speech comparison failed: espeak is missing"]



# M1 (Task 5 fix round 1): a G2P failure in finish is an error, not a verdict
def test_a_g2p_failure_in_finish_raises_and_the_item_stays_open(tmp_path):
    class BrokenG2P(FakeG2P):
        def phonemes_for_text(self, text):
            raise RuntimeError("the speech comparison failed: espeak is missing")
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), BrokenG2P())
    item, _ = store.open("Costs fell 5% today.", {})
    _record_and_check(tools, item)
    with pytest.raises(RuntimeError, match="espeak is missing"):
        tools.finish(item, "clean", [], [])
    assert store.load_state(item)["status"] == "open"


def test_overrides_that_differ_only_in_capitals_are_each_read_as_written(tmp_path):
    # Review round 2, G6: words() keeps "Mr." an abbreviation but not "MR.", and the
    # display cache was keyed case-folded, so [MR.] was checked as if it were [Mr.].
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), SpellingG2P())
    article = "We met Mr. Zounk there. The old sign said MR. Zounk too."
    item, _ = store.open(article, {})
    mr, upper = tools.phonemes(item, "Mr.")["phonemes"], tools.phonemes(item, "MR.")["phonemes"]
    store.write_text(item, "prepared.md", article.replace("Mr.", f"[Mr.](/{mr}/)")
                     .replace("MR.", f"[MR.](/{upper}/)"))
    _record_and_check(tools, item)
    assert tools.finish(item, "clean", [], [])["ok"]
