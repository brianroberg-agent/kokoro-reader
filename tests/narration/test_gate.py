import hashlib
import json
import re
import threading

import pytest

from narration import agent_tools, gate
from narration.agent_tools import MAX_ISSUES, MAX_NOTE, AgentTools, ToolError
from narration.fakes import FakeConverter, FakeG2P, FakeTTS, FakeVerifier
from narration.gate import evaluate, notes_for, speech_matches
from narration.runner_tools import RunnerTools
from narration.words import Substitution, check
from narration.workitems import WorkItemStore

META = {"title": "Zoning Alone", "authors": ["Mara Quillon"], "publication": "The New Atlantis",
        "issue_number": "86", "issue_season": "Fall 2026",
        "description": "Zoning made us lonely."}
ARTICLE = "Zoning made us lonely.\n\nThe Court may end it."
G2P = FakeG2P()


@pytest.fixture
def env(tmp_path):
    store = WorkItemStore(tmp_path)
    conv = FakeConverter()
    runner = RunnerTools(store, conv, FakeG2P())
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    opened = runner.open(ARTICLE, META)
    return store, runner, tools, conv, opened["item_id"], opened["agent_token"]


def test_open_returns_id_and_token(env):
    store, _, _, _, item, token = env
    assert store.item_for_token(token) == item


def test_finalize_refuses_an_open_item_unless_forced(env):
    store, runner, _, conv, item, token = env
    with pytest.raises(ToolError, match="still open"):
        runner.finalize(item)
    assert store.item_for_token(token) == item  # the session can still finish
    out = runner.finalize(item, force=True)  # the runner's timeout policy decided
    assert out["action"] == "retry" and not out["mp3"] and conv.calls == []
    assert store.item_for_token(token) is None  # abandoned: the token dies here
    assert runner.status(item)["status"] == "abandoned"
    assert runner.finalize(item)["action"] == "retry"  # terminal; no force needed now


def test_finalize_waits_for_a_running_record(env):
    store, runner, tools, _, item, _ = env
    started, release = threading.Event(), threading.Event()
    tools.tts.hold = (started, release)
    recorder = threading.Thread(target=tools.record, args=(item,))
    recorder.start()
    assert started.wait(5)  # round 1 is inside the engine
    out = []
    finalizer = threading.Thread(target=lambda: out.append(runner.finalize(item, force=True)))
    finalizer.start()
    finalizer.join(0.2)
    assert finalizer.is_alive()  # blocked behind the record, not interleaved with it
    release.set()
    recorder.join(5); finalizer.join(5)
    assert out[0]["action"] == "retry"
    assert store.load_state(item)["recorded_round"] == 1  # the record's state write landed


def _stuck_record(env, monkeypatch):
    """A record call held inside the engine, and a short finalize wait."""
    from narration import runner_tools
    monkeypatch.setattr(runner_tools, "FINALIZE_WAIT_SECONDS", 0.2)
    store, runner, tools, _, item, token = env
    started, release = threading.Event(), threading.Event()
    tools.tts.hold = (started, release)
    recorder = threading.Thread(target=tools.record, args=(item,))
    recorder.start()
    assert started.wait(5)
    return release, recorder


def _finalize_in_thread(runner, item, **kw):
    out = []

    def run():
        try:
            out.append(runner.finalize(item, **kw))
        except ToolError as e:
            out.append(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(3)
    assert not t.is_alive(), "finalize is still waiting for the stuck call"
    return out[0]


def test_finalize_gives_up_waiting_for_a_stuck_call_and_decides_nothing(env, monkeypatch):
    # Review F3: a call stuck in an engine held the item lock, and finalize waited
    # for it forever. Without force, it now refuses after the wait.
    store, runner, _, _, item, token = env
    release, recorder = _stuck_record(env, monkeypatch)
    try:
        out = _finalize_in_thread(runner, item)
        assert isinstance(out, ToolError) and "still running" in str(out)
        assert runner.status(item)["status"] == "open"  # nothing was decided
        assert store.item_for_token(token) == item
    finally:
        release.set()
        recorder.join(5)


def test_finalize_forced_past_a_stuck_call_revokes_the_token_and_retries(env, monkeypatch):
    # Review round 2, G3: forced, it still raised, so a call stuck for good (a
    # record has no timeout) kept the runner from ever moving on.
    store, runner, _, _, item, token = env
    release, recorder = _stuck_record(env, monkeypatch)
    try:
        out = _finalize_in_thread(runner, item, force=True)
        assert not isinstance(out, ToolError), out
        assert out["action"] == "retry" and not out["mp3"]
        assert "still running" in out["reasons"][0]
        assert store.item_for_token(token) is None  # the session can do nothing more
        assert runner.status(item)["status"] == "open"  # its state is not touched
    finally:
        release.set()
        recorder.join(5)
    assert store.load_state(item)["recorded_round"] == 1  # the stuck call's write landed


def test_clean_publishes_with_tags_and_notes(env):
    store, runner, tools, conv, item, _ = env
    tools.record(item); tools.check(item); tools.finish(item, "clean", [], [])
    out = runner.finalize(item)
    assert out["action"] == "publish" and out["mp3"]
    assert conv.calls == [("Zoning Alone", "Mara Quillon", "The New Atlantis, No. 86 (Fall 2026)")]
    assert out["notes"].startswith("Zoning made us lonely.")
    assert store.path(item, "episode.mp3").exists()


def test_minor_lists_issues_in_the_notes_but_never_their_free_text(env):
    store, runner, tools, conv, item, _ = env
    tools.record(item); tools.check(item)
    tools.finish(item, "minor", [{"kind": "pacing", "where": "0:12",
                                  "note": "rushed; see example.com"}], [])
    out = runner.finalize(item)
    assert out["action"] == "publish"
    assert "- 0:12 pacing" in out["notes"]
    assert "example.com" not in out["notes"]  # the note is for Brian, not the feed ...
    result = json.loads(store.read_text(item, "result.json"))
    assert result["remaining_issues"][0]["note"] == "rushed; see example.com"  # ... so it's in result.json


def test_agent_hold_is_a_hold(env):
    _, runner, tools, conv, item, _ = env
    tools.record(item); tools.check(item)
    tools.finish(item, "hold", [{"kind": "artifact", "where": "3:00", "note": "paragraph 4"}], [])
    out = runner.finalize(item)
    assert out["action"] == "hold" and conv.calls == []
    assert any("paragraph 4" in r for r in out["reasons"])


def test_gate_rechecks_words_independently(env):
    store, runner, tools, conv, item, _ = env
    tools.record(item); tools.check(item); tools.finish(item, "clean", [], [])
    store.write_text(item, "prepared.md", ARTICLE + " Buy now.")  # tampering after finish
    out = runner.finalize(item)
    assert out["action"] == "hold" and any("buy" in r for r in out["reasons"])


def test_evaluate_holds_when_the_latest_round_has_no_recording():
    state = {"status": "finished", "records": 2, "recorded_round": 1,
             "checks": [{"n": 1, "record": 1, "warnings": []}],
             "finish": {"outcome": "clean", "remaining_issues": [], "substitutions": []}}
    out = evaluate(state, ARTICLE, ARTICLE, META, G2P)
    assert out.action == "hold" and any("no recording" in r for r in out.reasons)


def test_finalize_twice_does_not_convert_twice(env):
    _, runner, tools, conv, item, _ = env
    tools.record(item); tools.check(item); tools.finish(item, "clean", [], [])
    runner.finalize(item)
    runner.finalize(item)
    assert len(conv.calls) == 1


def test_evaluate_without_edition_uses_publication_as_album():
    from narration.gate import album_for
    assert album_for({"publication": "The New Atlantis"}) == "The New Atlantis"


# ---- R1: the gate repeats finish's claims, on the files as they are now ----
def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _state(**changes):
    """A finished item whose every claim holds for ARTICLE, with `changes` applied."""
    state = {"status": "finished", "records": 1, "recorded_round": 1, "checks_used": 1,
             "recorded_sha256": _sha(ARTICLE), "checks": [{"n": 1, "record": 1, "warnings": []}],
             "finish": {"outcome": "clean", "remaining_issues": [], "substitutions": []}}
    state.update(changes)
    return state


ISSUE = {"kind": "pacing", "where": "1:10", "note": "rushed"}


def test_evaluate_publishes_when_every_claim_holds():
    out = evaluate(_state(), ARTICLE, ARTICLE, META, G2P)
    assert (out.action, out.outcome, out.reasons) == ("publish", "clean", [])


def test_evaluate_holds_when_the_prepared_text_is_not_what_was_recorded():
    paced = ARTICLE.replace("lonely.", "lonely...")  # passes the word check; not what was recorded
    out = evaluate(_state(), ARTICLE, paced, META, G2P)
    assert out.action == "hold" and any("changed" in r for r in out.reasons)
    out = evaluate(_state(recorded_sha256=None), ARTICLE, ARTICLE, META, G2P)
    assert out.action == "hold" and any("changed" in r for r in out.reasons)


def test_evaluate_holds_when_nothing_was_recorded():
    out = evaluate(_state(records=0, recorded_round=None, recorded_sha256=None, checks=[]),
                   ARTICLE, ARTICLE, META, G2P)
    assert out.action == "hold" and any("nothing is recorded" in r for r in out.reasons)


@pytest.mark.parametrize("changes", [
    # round 2 was recorded but only round 1 was checked
    {"records": 2, "recorded_round": 2, "checks_used": 2},
    # a check of round 1 was attempted but failed: counted, with no report
    {"checks_used": 1, "checks": []},
    # three attempts on round 2, all failed
    {"records": 2, "recorded_round": 2, "checks_used": 4},
])
def test_evaluate_holds_without_a_successful_check_of_the_latest_round(changes):
    out = evaluate(_state(**changes), ARTICLE, ARTICLE, META, G2P)
    assert out.action == "hold" and any("check" in r and "latest recording" in r
                                        for r in out.reasons)


@pytest.mark.parametrize("outcome, issues", [("clean", []), ("minor", [ISSUE])])
def test_evaluate_holds_on_precheck_warnings_for_clean_and_minor(outcome, issues):
    warned = [{"n": 1, "record": 1, "warnings": ["0:07 of silence at 3:10"]}]
    finish = {"outcome": outcome, "remaining_issues": issues, "substitutions": []}
    out = evaluate(_state(checks=warned, finish=finish), ARTICLE, ARTICLE, META, G2P)
    assert out.action == "hold" and any("0:07 of silence at 3:10" in r for r in out.reasons)


def test_only_the_latest_rounds_latest_check_counts():
    checks = [{"n": 1, "record": 1, "warnings": ["cut short"]},
              {"n": 2, "record": 2, "warnings": []}]
    out = evaluate(_state(records=2, recorded_round=2, checks_used=2, checks=checks),
                   ARTICLE, ARTICLE, META, G2P)
    assert out.action == "publish"
    checks = [{"n": 1, "record": 1, "warnings": []}, {"n": 2, "record": 1, "warnings": ["cut"]}]
    out = evaluate(_state(checks_used=2, checks=checks), ARTICLE, ARTICLE, META, G2P)
    assert out.action == "hold"


def test_evaluate_holds_when_the_words_differ():
    edited = ARTICLE.replace("may end", "will end")
    out = evaluate(_state(recorded_sha256=_sha(edited)), ARTICLE, edited, META, G2P)
    assert out.action == "hold"
    assert any("may" in r and "will" in r for r in out.reasons)


def test_a_finalize_after_an_edit_since_the_recording_holds(env):
    store, runner, tools, conv, item, _ = env
    tools.record(item); tools.check(item); tools.finish(item, "clean", [], [])
    store.write_text(item, "prepared.md", ARTICLE.replace("lonely.", "lonely..."))
    out = runner.finalize(item)
    assert out["action"] == "hold" and conv.calls == []
    assert any("changed" in r for r in out["reasons"])


# ---- R2: one substitution validation, shared with finish ----
NAMED = "Pengelley met the family.\n\nIt went well."
RESPELLING = {"original": "Pengelley", "spoken": "Penjelly", "why": "pronunciation"}


@pytest.fixture
def named(tmp_path):
    store = WorkItemStore(tmp_path)
    conv = FakeConverter()
    runner = RunnerTools(store, conv, FakeG2P())
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    opened = runner.open(NAMED, META)
    return store, runner, tools, conv, opened["item_id"]


def test_the_gate_and_finish_share_the_substitution_validation():
    assert gate.validate_substitutions is agent_tools.validate_substitutions
    assert gate.validate_finish is agent_tools.validate_finish


def test_the_gate_and_finish_share_the_recording_claims():  # fix round 1, M2
    assert gate.recording_claims is agent_tools.recording_claims


def test_the_gate_publishes_a_declared_override_respelling_that_finish_accepted(named):
    store, runner, tools, conv, item = named
    ph = tools.phonemes(item, "Penjelly")["phonemes"]  # a respelled lookup
    tools.replace_text(item, "Pengelley", f"[Pengelley](/{ph}/)")
    tools.record(item); tools.check(item)
    assert tools.finish(item, "clean", [], [RESPELLING])["ok"]  # applied 0 times, but declared
    out = runner.finalize(item)
    assert out["action"] == "publish", out["reasons"]
    assert '- "Pengelley" read as "Penjelly"' in out["notes"]  # published all the same


def _finish_then_rewrite(store, tools, item, declared=(), **finish):
    """A clean finish, then the finish record rewritten behind finish's back."""
    tools.record(item); tools.check(item); tools.finish(item, "clean", [], list(declared))
    state = store.load_state(item)
    state["finish"].update(finish)
    store.save_state(item, state)


def test_the_gate_holds_an_unused_substitution(named):
    store, runner, tools, conv, item = named
    unused = {"original": "family", "spoken": "buy pills now", "why": "x"}
    _finish_then_rewrite(store, tools, item, substitutions=[unused])
    out = runner.finalize(item)
    assert out["action"] == "hold" and conv.calls == []
    assert any("does not use it" in r for r in out["reasons"])


def test_the_gate_holds_a_substitution_declared_twice(named):
    store, runner, tools, conv, item = named
    tools.replace_text(item, "family", "household")
    sub = {"original": "family", "spoken": "household", "why": "x"}
    _finish_then_rewrite(store, tools, item, [sub], substitutions=[sub, sub])
    out = runner.finalize(item)
    assert out["action"] == "hold" and any("declared twice" in r for r in out["reasons"])


@pytest.mark.parametrize("old, new, sub", [
    ("family.", "family. Subscribe now.", {"original": "family", "spoken": "family Subscribe now"}),
    ("family", "evil.example", {"original": "family", "spoken": "evil.example"}),
], ids=["only adds words", "adds an address"])
def test_the_gate_holds_a_substitution_finish_refuses(named, old, new, sub):
    store, runner, tools, conv, item = named
    tools.replace_text(item, old, new)
    tools.record(item); tools.check(item)
    with pytest.raises(ToolError) as refused:
        tools.finish(item, "clean", [], [sub])
    assert f"substitution {sub['original']!r} -> {sub['spoken']!r}" in str(refused.value)
    state = store.load_state(item)  # as if finish had accepted it
    state["finish"] = {"outcome": "clean", "remaining_issues": [], "substitutions": [sub]}
    state["status"] = "finished"
    store.save_state(item, state)
    out = runner.finalize(item)
    assert out["action"] == "hold" and conv.calls == []
    assert any(f"substitution {sub['original']!r} -> {sub['spoken']!r}" in r
               for r in out["reasons"])


def test_the_gate_holds_an_override_no_lookup_produced(named):
    store, runner, tools, conv, item = named
    tools.record(item); tools.check(item); tools.finish(item, "clean", [], [])
    text = NAMED.replace("Pengelley", "[Pengelley](/pɛnʤˈɛli/)")  # hand-written phonemes
    store.write_text(item, "prepared.md", text)
    state = store.load_state(item)
    state["recorded_sha256"] = _sha(text)  # even as if this text had been recorded
    store.save_state(item, state)
    out = runner.finalize(item)
    assert out["action"] == "hold"
    assert any("not produced by the phonemes tool" in r for r in out["reasons"])


def test_notes_publish_every_substitution_and_issues_as_timestamp_and_kind():
    finish = {"outcome": "minor",
              "remaining_issues": [{"kind": "pacing", "where": "12:40", "note": "see x.com"}],
              "substitutions": [RESPELLING,
                                {"original": "XIV", "spoken": "the Fourteenth", "why": "numeral"}]}
    notes = notes_for(META, finish)
    assert notes.splitlines() == [
        "Zoning made us lonely.",
        "Known issues in this recording:",
        "- 12:40 pacing",
        "Read with these substitutions:",
        '- "Pengelley" read as "Penjelly"',
        '- "XIV" read as "the Fourteenth"',
        "Recorded automatically."]


# ---- R4: finalize and status ----
def test_finalize_and_status_of_an_unknown_item(env):
    _, runner, *_ = env
    with pytest.raises(ToolError, match="no such work item"):
        runner.finalize("0123456789ab")
    with pytest.raises(ToolError, match="no such work item"):
        runner.finalize("../../etc")
    assert runner.status("0123456789ab") == {"exists": False}


def test_status_reports_the_round_counts_and_whether_the_text_is_what_was_recorded(env):
    store, runner, tools, conv, item, _ = env
    status = runner.status(item)
    assert (status["exists"], status["status"], status["records"], status["checks_used"],
            status["recorded_round"], status["prepared_matches_recording"]) == (
        True, "open", 0, 0, None, False)
    tools.record(item)
    assert runner.status(item)["prepared_matches_recording"] is True
    tools.replace_text(item, "lonely.", "lonely...")
    status = runner.status(item)
    assert status["prepared_matches_recording"] is False and status["recorded_round"] == 1
    tools.replace_text(item, "lonely...", "lonely.")
    tools.check(item)
    assert (runner.status(item)["checks_used"], runner.status(item)["checks"]) == (1, 1)
    tools.finish(item, "clean", [], [])
    assert runner.status(item)["status"] == "finished"
    runner.finalize(item)
    status = runner.status(item)
    assert status["status"] == "finalized" and status["mp3"] is True
    assert status["finish"]["outcome"] == "clean"


class BrokenConverter(FakeConverter):
    def __init__(self):
        super().__init__()
        self.fail = True

    def to_mp3(self, wav, mp3, title, artist, album):
        if self.fail:
            from pathlib import Path
            Path(mp3).write_bytes(b"ID3 half")  # a crash mid-write
            raise RuntimeError("the MP3 conversion failed: disk full")
        super().to_mp3(wav, mp3, title, artist, album)


def test_a_failed_conversion_leaves_no_mp3_and_can_be_retried(tmp_path):
    store, conv = WorkItemStore(tmp_path), BrokenConverter()
    runner = RunnerTools(store, conv, FakeG2P())
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    item = runner.open(ARTICLE, META)["item_id"]
    tools.record(item); tools.check(item); tools.finish(item, "clean", [], [])
    with pytest.raises(RuntimeError, match="disk full"):
        runner.finalize(item)
    mp3 = store.path(item, "episode.mp3")
    assert not mp3.exists() and not list(mp3.parent.glob(".tmp-*"))
    assert runner.status(item)["status"] == "finished"
    conv.fail = False
    out = runner.finalize(item)
    assert out["action"] == "publish" and out["mp3"] and mp3.read_bytes() == b"ID3 fake mp3"


def test_a_hold_makes_no_mp3(env):
    store, runner, tools, conv, item, _ = env
    tools.record(item); tools.check(item)
    tools.finish(item, "hold", [ISSUE], [])
    out = runner.finalize(item)
    assert out["action"] == "hold" and out["mp3"] is False and conv.calls == []
    assert runner.status(item)["status"] == "finalized"


# ---- R5: the gate re-validates the finish record's shapes with agent_tools' rules ----
@pytest.mark.parametrize("finish", [
    {"outcome": "great", "remaining_issues": [], "substitutions": []},
    {"outcome": "minor", "remaining_issues": [{**ISSUE, "where": "near the end"}],
     "substitutions": []},
    {"outcome": "minor", "remaining_issues": [{**ISSUE, "where": "1:10\n<b>"}], "substitutions": []},
    {"outcome": "minor", "remaining_issues": [{**ISSUE, "kind": "<script>"}], "substitutions": []},
    {"outcome": "minor", "remaining_issues": [{**ISSUE, "note": "x" * (MAX_NOTE + 1)}],
     "substitutions": []},
    {"outcome": "minor", "remaining_issues": [ISSUE] * (MAX_ISSUES + 1), "substitutions": []},
    {"outcome": "clean", "remaining_issues": [ISSUE], "substitutions": []},
    {"outcome": "minor", "remaining_issues": [], "substitutions": []},
    {"outcome": "clean", "remaining_issues": [], "substitutions": "none"},
    {"outcome": "clean", "remaining_issues": [], "substitutions": [{"original": 5}]},
    {"outcome": "clean"},
])
def test_a_malformed_finish_record_is_held_and_never_reaches_the_notes(finish):
    out = evaluate(_state(finish=finish), ARTICLE, ARTICLE, META, G2P)
    assert out.action == "hold" and out.reasons
    assert out.notes == "Zoning made us lonely.\nRecorded automatically."


# ---- R3: what the engine would say, compared through its own G2P ----
class SignG2P(FakeG2P):
    """FakeG2P that voices a minus where misaki does and the word check doesn't: a "-"
    before a digit, also after dots ("..-5") or with a quote before the digit ("-‘5")."""
    def phonemes_for_text(self, text):
        return super().phonemes_for_text(re.sub(r"-[‘']?(?=\d)", " minus ", text))


class CountingG2P(FakeG2P):
    def __init__(self):
        self.calls = 0

    def phonemes_for_text(self, text):
        self.calls += 1
        return super().phonemes_for_text(text)


class BrokenG2P(FakeG2P):
    def phonemes_for_text(self, text):
        raise RuntimeError("the speech comparison failed: espeak is missing")


COSTS = "Costs fell 5% today.\n\nThe Court may end it."
HOLE = COSTS.replace("fell 5%", "fell ..-5%")  # the word check reads "5%"; misaki says "minus"


def test_identical_speech_passes():
    assert speech_matches(G2P, ARTICLE, ARTICLE, []) == []


def test_pacing_punctuation_passes():
    paced = ("Zoning, made us... lonely!\n\n[BREAK]\n\nThe Court -- may\n\nend it;")
    assert check(ARTICLE, paced, []) == []
    assert speech_matches(G2P, ARTICLE, paced, []) == []


def test_a_declared_substitution_passes_and_an_undeclared_word_fails():
    article = "King Ivo XIV met the family."
    prepared = "King Ivo the Fourteenth met the family."
    assert speech_matches(G2P, article, prepared, [Substitution("XIV", "the Fourteenth")]) == []
    problems = speech_matches(G2P, article, prepared, [])
    assert problems == ["the prepared text does not align with the article (the word check "
                        "says where), so what the engine would say could not be compared with "
                        "the article"]
    problems = speech_matches(G2P, ARTICLE, ARTICLE + " Buy now.", [])
    assert len(problems) == 1 and "does not align" in problems[0]


class MisplacingG2P(FakeG2P):
    """FakeG2P that, as misaki does, puts an override glued after "word(" on that word."""
    def phonemes_for_text(self, text):
        return super().phonemes_for_text(
            re.sub(r"(\w+)\(\[([^\]]+)\]\(/([^)]*)/\)", r"[\1](/\3/)(\2", text))


def test_an_override_is_compared_by_its_phonemes_where_the_engine_puts_them():
    assert G2P.phonemes_for_text("The [cat](/kˈæt/) sat.") == "the kˈæt sat"
    assert speech_matches(G2P, "The cat sat.", "The [cat](/kˈæt/) sat.", []) == []
    # The article's side has the override free-standing, around its word; the engine
    # would read the prepared text's on the word before: the phonemes moved.
    article = "He said(Quillon) left."
    prepared = "He said([Quillon](/lˈɔlɚ/)) left."
    assert check(article, prepared, []) == []
    assert speech_matches(G2P, article, prepared, []) == []
    problems = speech_matches(MisplacingG2P(), article, prepared, [])
    assert len(problems) == 1 and "'he lˈɔlɚ quillon left'" in problems[0]
    assert "'he said lˈɔlɚ left'" in problems[0]


def test_an_override_that_is_part_of_a_longer_word_cannot_be_compared():
    problems = speech_matches(G2P, "Dr. Pengelley met.", "[Dr](/dˈɑktɚ/). Pengelley met.", [])
    assert check("Dr. Pengelley met.", "[Dr](/dˈɑktɚ/). Pengelley met.", []) == []
    assert len(problems) == 1 and "'[Dr](/dˈɑktɚ/)' is not one whole word" in problems[0]
    assert "word check" not in problems[0]


def test_a_sign_the_word_check_misses_fails_and_the_message_shows_where():
    assert check(COSTS, HOLE, []) == []  # the parked hole
    problems = speech_matches(SignG2P(), COSTS, HOLE, [])
    assert len(problems) == 1
    assert "in line 1 of the prepared text, near 'Costs fell ..-5% today." in problems[0]
    assert "(line 1, near 'Costs fell 5% today.')" in problems[0]
    assert "minus" in problems[0]  # the prepared side's phonemes around the difference
    assert speech_matches(SignG2P(), COSTS, COSTS.replace("today", "-- today"), []) == []


def test_the_message_quotes_the_words_nearest_the_difference_on_each_side():
    long_line = ("Then the council met for an hour, and after a long debate costs fell 5% "
                 "today, as the rest of a long line goes on and on.")
    article = "One line here.\n\n" + long_line
    prepared = "One line here.\n\n[BREAK]\n\n" + long_line.replace("5%", "..-5%")
    problems = speech_matches(SignG2P(), article, prepared, [])
    # three words each side of the word where the sounds part (a number can be several words)
    assert "in line 2 of the prepared text, near 'debate costs fell ..-5% today, as the'" in problems[0]
    assert "(line 2, near 'costs fell 5% today, as the rest')" in problems[0]


# M2 (final review): a mismatch is most often a pause the session added, so the advice
# says to make the text read as the article does there first, then the override rule
def test_the_mismatch_advice_puts_the_pause_first():
    problems = speech_matches(SignG2P(), COSTS, HOLE, [])
    advice = problems[0][problems[0].index("says '"):]
    assert "Make the prepared text read as the article does there" in advice
    pause, override = advice.index("move or remove the pause"), advice.index("phonetic override")
    assert pause < override
    assert "instead of changing case or spelling" in advice[override:]


# M3, M4 (final review): record's advisory says when it did not compare, and no
# failure of it (it only advises) stops a record
def test_speech_warnings_say_when_the_words_differ_and_nothing_was_compared():
    assert gate.speech_warnings(G2P, ARTICLE, ARTICLE) == []
    assert gate.speech_warnings(G2P, ARTICLE, ARTICLE + " Buy now.") == [gate.NOT_COMPARED]
    assert gate.NOT_COMPARED == ("not compared: the prepared text differs from the article in "
                                 "words; finish compares it with your declared substitutions")


@pytest.mark.parametrize("error, shown", [
    (RuntimeError("the speech comparison failed: espeak is missing"),
     "the speech comparison could not run: the speech comparison failed: espeak is missing"),
    (KeyError("/Users/x/secret/path"),
     "the speech comparison could not run (an internal error, logged on the server)"),
])
def test_speech_warnings_turn_any_failure_into_a_logged_warning(caplog, error, shown):
    class FailingG2P(FakeG2P):
        def phonemes_for_text(self, text):
            raise error
    with caplog.at_level("WARNING", logger="narration.gate"):
        assert gate.speech_warnings(FailingG2P(), ARTICLE, ARTICLE) == [shown]
    assert any("speech comparison" in r.getMessage() for r in caplog.records)
    if not isinstance(error, RuntimeError):
        assert any(r.exc_info for r in caplog.records)  # the traceback, for Brian


def test_evaluate_holds_when_the_engine_would_say_something_else():
    out = evaluate(_state(recorded_sha256=_sha(HOLE)), COSTS, HOLE, META, SignG2P())
    assert out.action == "hold" and any("minus" in r for r in out.reasons)
    out = evaluate(_state(recorded_sha256=_sha(HOLE)), COSTS, HOLE, META, G2P)
    assert out.action == "publish"  # FakeG2P reads no sign: the comparison decided


def test_a_g2p_failure_raises_so_the_item_is_not_finalized(tmp_path):
    with pytest.raises(RuntimeError, match="espeak is missing"):
        evaluate(_state(), ARTICLE, ARTICLE, META, BrokenG2P())
    store, conv = WorkItemStore(tmp_path), FakeConverter()
    runner = RunnerTools(store, conv, BrokenG2P())
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    item = runner.open(ARTICLE, META)["item_id"]
    tools.record(item); tools.check(item); tools.finish(item, "clean", [], [])
    with pytest.raises(RuntimeError, match="espeak is missing"):
        runner.finalize(item)  # the runner marks it failed and retries; nothing is decided
    assert runner.status(item)["status"] == "finished" and conv.calls == []
    runner.g2p = FakeG2P()
    assert runner.finalize(item)["action"] == "publish"


def test_the_comparison_runs_only_when_everything_else_holds():
    counting = CountingG2P()
    evaluate(_state(), ARTICLE, ARTICLE, META, counting)
    assert counting.calls == 2  # the prepared text, and the article with its substitutions
    held = {"outcome": "hold", "remaining_issues": [ISSUE], "substitutions": []}
    for state in (_state(finish=held), _state(records=2, recorded_round=1)):
        counting.calls = 0
        assert evaluate(state, ARTICLE, ARTICLE, META, counting).action == "hold"
        assert counting.calls == 0


def test_finalize_runs_the_comparison_with_the_runners_g2p(tmp_path):
    store, conv = WorkItemStore(tmp_path), FakeConverter()
    runner = RunnerTools(store, conv, SignG2P())
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())  # reads no sign
    item = runner.open(COSTS, META)["item_id"]
    tools.replace_text(item, "fell 5%", "fell ..-5%")
    tools.record(item); tools.check(item); tools.finish(item, "clean", [], [])
    out = runner.finalize(item)
    assert out["action"] == "hold" and conv.calls == []


def test_misaki_hears_a_sign_the_word_check_misses():
    # Runs only where misaki is installed (not in the light test environment).
    pytest.importorskip("misaki")
    from narration.engines import MisakiG2P
    g2p = MisakiG2P()
    source = "Costs fell 5% today."
    assert speech_matches(g2p, source, "Costs fell(-5%) today.", [])
    assert speech_matches(g2p, source, "Costs fell 5% -- today.", []) == []  # a pause
    for hole in ("Costs fell ..-5% today.", "Costs fell -‘5% today."):
        assert check(source, hole, []) == []  # the word check reads "5%" ...
        problems = speech_matches(g2p, source, hole, [])
        assert problems and "mˈInəs" in problems[0]  # ... the engine says "minus"
    # overrides read in place pass, free-standing on the article's side or not ...
    named = "We met Pengelley, then Obrano-Corvan and Jean-Luc."
    prepared = "We met [Pengelley](/pɛnʤˈɛli/), then Obrano-[Corvan](/kɔɹtˈɛz/) and Jean-[Luc](/lˈuk/)."
    assert speech_matches(g2p, named, prepared, []) == []
    # ... and one misaki puts on the wrong word fails: here it loses "left"
    article = "He said(Quillon) that Pengelley left."
    prepared = "He said([Quillon](/lˈɔlɚ/)) that [Pengelley](/pɛnʤˈɛli/) left."
    assert check(article, prepared, []) == []
    assert speech_matches(g2p, article, prepared, [])
    # markup that a second cleaning would change, unedited, matches
    for article in ("# # Hash tags", "1. 2. Two items"):
        assert speech_matches(g2p, article, article, []) == []


# ---- fix round 1 ----
# C1: a substitution's strings are one line of plain text, so none can carry markup into the feed
INJECTIONS = ["[Penjelly](https://evil.example/buy)", "Penjelly\n\n![x](https://evil.example/p.png)",
              "```<script>alert(1)</script>```", "Penjelly\n## Buy at evil.example",
              "</p><h1>Hacked</h1>"]


@pytest.mark.parametrize("field", ["original", "spoken", "why"])
@pytest.mark.parametrize("text", INJECTIONS)
def test_finish_refuses_a_substitution_that_is_not_one_line_of_plain_text(named, field, text):
    store, runner, tools, conv, item = named
    tools.record(item); tools.check(item)
    sub = {**RESPELLING, field: text}
    for outcome, issues in (("clean", []), ("hold", [ISSUE])):
        with pytest.raises(ToolError, match=f"substitution {field} must be one line of plain text"):
            tools.finish(item, outcome, issues, [sub])
    assert store.load_state(item)["status"] == "open"


@pytest.mark.parametrize("text", ["Pen\tjelly", "Pen\rjelly", "Pen jelly", "Pen​jelly",
                                  "Pen‮jelly", "a|b", "a\\b", "a{b}", "a_b", "a*", "a<b",
                                  "a[1]", "a`", "...", "",
                                  "x" * 201])
def test_a_substitution_string_needs_a_word_and_no_markup_or_control_characters(text):
    with pytest.raises(ToolError, match="substitution spoken must be one line of plain text"):
        agent_tools.validate_finish("clean", [], [{"original": "Pengelley", "spoken": text}])


@pytest.mark.parametrize("text", ["the Fourteenth", "U.S.", "Pengelley's", "Obrano-Corvan",
                                  "$3.50", "5%", "No. 5, e.g.", "Vaine-Bröck", '"quoted"',
                                  "#5", "(5%)", "Yahoo!", "a & b + c @ d / e: f; g?"])
def test_plain_substitution_strings_pass(text):
    issues, subs = agent_tools.validate_finish("clean", [], [{"original": "x", "spoken": text}])
    assert subs == [{"original": "x", "spoken": text, "why": ""}]


@pytest.mark.parametrize("text", INJECTIONS)
def test_an_old_record_with_markup_in_a_substitution_is_never_published(text):
    finish = {"outcome": "clean", "remaining_issues": [],
              "substitutions": [{"original": "Pengelley", "spoken": text, "why": ""},
                                {"original": "XIV", "spoken": "the Fourteenth", "why": "numeral"}]}
    notes = notes_for(META, finish)
    assert "evil" not in notes and "<" not in notes and "Penjelly" not in notes
    assert '- "XIV" read as "the Fourteenth"' in notes  # the valid one is still listed
    out = evaluate(_state(finish=finish), ARTICLE, ARTICLE, META, G2P)
    assert out.action == "hold" and "evil" not in out.notes and "<" not in out.notes


def test_notes_drop_an_issue_an_old_record_got_wrong():
    finish = {"outcome": "minor", "substitutions": [],
              "remaining_issues": [{"kind": "pacing", "where": "1:10\n<b>x</b>", "note": ""},
                                   {"kind": "<i>", "where": "1:10", "note": ""}, ISSUE]}
    notes = notes_for(META, finish)
    assert "<" not in notes and "- 1:10 pacing" in notes and notes.count("\n- ") == 1


# I1: metadata in every shape article-assistant documents
EMPTY_META = {"title": "", "authors": [], "publication": "", "issue_number": "",
              "issue_season": "", "date_published": "", "description": ""}


@pytest.mark.parametrize("metadata, expected", [
    ({}, {}),
    ([], {}),  # not even an object
    ({"description": None, "authors": None, "title": None, "issue_number": None}, {}),
    ({"authors": []}, {}),
    ({"authors": "Mara Quillon"}, {"authors": ["Mara Quillon"]}),
    ({"authors": ["A", None, "", "  ", 5, " B "]}, {"authors": ["A", "B"]}),
    ({"authors": {"name": "A"}}, {}),
    ({"issue_number": 86, "issue_season": "Fall 2026"},
     {"issue_number": "86", "issue_season": "Fall 2026"}),
    ({"issue_number": True, "title": ["x"], "date_published": {"y": 1}}, {}),
    ({"title": " Zoning Alone ", "description": 7}, {"title": "Zoning Alone", "description": "7"}),
])
def test_meta_reads_each_documented_shape(metadata, expected):
    assert gate._meta(metadata) == {**EMPTY_META, **expected}


@pytest.mark.parametrize("changes, artist, album, first_line", [
    ({"description": None, "authors": None}, "", "The New Atlantis, No. 86 (Fall 2026)",
     "Recorded automatically."),
    ({"authors": ["Mara Quillon", None]}, "Mara Quillon", "The New Atlantis, No. 86 (Fall 2026)",
     "Zoning made us lonely."),
    ({"authors": "Mara Quillon"}, "Mara Quillon", "The New Atlantis, No. 86 (Fall 2026)",
     "Zoning made us lonely."),
    ({"authors": ["A", "B"], "issue_number": 86}, "A; B", "The New Atlantis, No. 86 (Fall 2026)",
     "Zoning made us lonely."),
    ({"issue_number": None, "issue_season": None}, "Mara Quillon", "The New Atlantis",
     "Zoning made us lonely."),
    ({"publication": None, "title": None}, "Mara Quillon", "No. 86 (Fall 2026)",
     "Zoning made us lonely."),
])
def test_finalize_publishes_whatever_shape_the_metadata_has(tmp_path, changes, artist, album,
                                                            first_line):
    store, conv = WorkItemStore(tmp_path), FakeConverter()
    runner = RunnerTools(store, conv, FakeG2P())
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    item = runner.open(ARTICLE, {**META, **changes})["item_id"]
    tools.record(item); tools.check(item); tools.finish(item, "clean", [], [])
    out = runner.finalize(item)
    assert out["action"] == "publish" and out["mp3"]
    assert conv.calls[0][1:] == (artist, album)
    assert out["notes"].splitlines()[0] == first_line
    assert runner.status(item)["metadata"] == gate._meta({**META, **changes})


@pytest.mark.parametrize("metadata", [{"x": object()}, {"x": float("nan")}, {"x": {1, 2}},
                                      {"x": b"bytes"}])
def test_open_refuses_metadata_that_is_not_json_before_touching_the_store(tmp_path, metadata):
    store = WorkItemStore(tmp_path)
    runner = RunnerTools(store, FakeConverter(), FakeG2P())
    with pytest.raises(ToolError, match="metadata must be JSON"):
        runner.open(ARTICLE, metadata)
    assert list((tmp_path / "items").iterdir()) == []


# I2: the article's side is prepared for the engine once, as the engine prepares it
class VoicingG2P(FakeG2P):
    """FakeG2P that voices "*", "#" and digits, as misaki does ("asterisk", "hash", "two")."""
    SAID = {"*": " asterisk ", "#": " hash ", "1": " one ", "2": " two "}

    def phonemes_for_text(self, text):
        return super().phonemes_for_text("".join(self.SAID.get(ch, ch) for ch in text))


class RecordingG2P(FakeG2P):
    def __init__(self):
        self.texts = []

    def phonemes_for_text(self, text):
        self.texts.append(text)
        return super().phonemes_for_text(text)


MARKUP = ["# # Hash tags", "1. 2. Two items"]


@pytest.mark.parametrize("article", MARKUP)
def test_markup_that_cleaning_changes_twice_passes_unedited(article):
    from narration.words import engine_text
    assert engine_text(engine_text(article)) != engine_text(article)  # cleaning isn't idempotent
    assert speech_matches(G2P, article, article, []) == []
    assert speech_matches(VoicingG2P(), article, article, []) == []


def test_the_article_side_reaches_the_g2p_as_apply_returns_it():
    from narration.words import apply, engine_text
    recording = RecordingG2P()
    article = "# # It was fine.\n\n[BREAK]\n\nKing Ivo XIV spoke."
    prepared = "# # It was fine.\n\nKing Ivo the Fourteenth spoke."
    subs = [Substitution("XIV", "the Fourteenth")]
    assert speech_matches(recording, article, prepared, subs) == []
    assert recording.texts == [engine_text(prepared), apply(article, prepared, subs)]



# I3: a case-only edit can change what the engine says; the advice is an override
class CaseG2P(FakeG2P):
    """FakeG2P that spells a short all-capitals word letter by letter, as misaki says "AI"."""
    def phonemes_for_text(self, text):
        return super().phonemes_for_text(
            re.sub(r"\b[A-Z]{2,3}\b", lambda m: " ".join("ˈ" + c.lower() for c in m.group()), text))


def test_a_case_only_edit_the_engine_reads_differently_is_refused_with_override_advice(tmp_path):
    store = WorkItemStore(tmp_path)
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), CaseG2P())
    item, _ = store.open("Ai is here.", {})
    tools.replace_text(item, "Ai", "AI")
    assert check("Ai is here.", "AI is here.", []) == []  # case is folded
    tools.record(item); tools.check(item)
    with pytest.raises(ToolError) as refused:
        tools.finish(item, "clean", [], [])
    message = str(refused.value)
    assert ("if the change is for pronunciation, use a phonetic override from phonemes() "
            "instead of changing case or spelling.") in message
    assert gate.MISMATCH_ADVICE + "." in message
    assert "declare " not in message and "in substitutions" not in message  # no impossible advice
    assert message.endswith("Then record and check again; or finish with outcome 'hold'.")


# I3 (final review): markdownify keeps a page's hrefs, so an article's own links to
# paths between slashes read as phonetic overrides; open unwraps every link
LINKED = ("# The Report\n\nAs [the report](/publications/the-report/) said, prices fell. "
          "See [Home](/home/) and [the court](https://example.com/court?x=1).\n\n"
          "![A chart of prices](/images/chart/)\n\nIt went well.")


def test_open_unwraps_the_articles_links_and_the_item_publishes_unedited(tmp_path):
    from narration.words import overrides
    from text_to_speech import clean_markdown_text
    store = WorkItemStore(tmp_path)
    conv = FakeConverter()
    runner = RunnerTools(store, conv, FakeG2P())
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    item = runner.open(LINKED, META)["item_id"]
    stored = ("# The Report\n\nAs the report said, prices fell. See Home and the court.\n\n"
              "\n\nIt went well.")  # the image is dropped, alt text and all, as the engine drops it
    assert store.read_text(item, "source.md") == store.read_text(item, "prepared.md") == stored
    assert overrides(stored) == []
    # What the engine reads is unchanged, but for the paths it no longer takes for overrides.
    as_links = LINKED.replace("/publications/the-report/", "x").replace("/home/", "x")
    assert clean_markdown_text(stored) == clean_markdown_text(as_links)
    tools.record(item); tools.check(item)
    assert tools.finish(item, "clean", [], [])["ok"]
    out = runner.finalize(item)
    assert out["action"] == "publish" and out["mp3"], out["reasons"]


def test_open_refuses_an_article_with_no_text_once_its_images_are_dropped(tmp_path):
    store = WorkItemStore(tmp_path)
    runner = RunnerTools(store, FakeConverter(), FakeG2P())
    with pytest.raises(ToolError, match="no text"):
        runner.open("![A chart](/images/chart.png)\n", META)
    assert list((tmp_path / "items").iterdir()) == []


# N1 (re-review): reading a number range out passes the whole path
def test_a_number_range_read_out_publishes_with_its_substitution_in_the_notes(tmp_path):
    store = WorkItemStore(tmp_path)
    conv = FakeConverter()
    runner = RunnerTools(store, conv, FakeG2P())
    tools = AgentTools(store, FakeTTS(), FakeVerifier(), FakeG2P())
    item = runner.open("The work took 2-3 weeks.\n\nThe council agreed.", META)["item_id"]
    tools.replace_text(item, "2-3", "2 to 3")
    tools.record(item); tools.check(item)
    sub = {"original": "2-3", "spoken": "2 to 3", "why": "a range, read out"}
    assert tools.finish(item, "clean", [], [sub])["ok"]
    out = runner.finalize(item)
    assert out["action"] == "publish" and out["mp3"], out["reasons"]
    assert '- "2-3" read as "2 to 3"' in out["notes"]


# ---- end-to-end test findings, 2026-11-03 ----
def test_an_omission_is_a_kind_of_issue_finish_accepts():
    # F3: a word the engine left out fitted none of the kinds, so it was published as "other".
    issues, _ = agent_tools.validate_finish(
        "minor", [{"kind": "omission", "where": "2:27", "note": "a dropped"}], [])
    assert issues == [{"kind": "omission", "where": "2:27", "note": "a dropped"}]


def test_notes_list_the_issues_in_time_order():
    # F3: the notes followed the order the agent wrote them in (3:32, 3:43, then 2:27).
    finish = {"outcome": "minor", "substitutions": [],
              "remaining_issues": [{"kind": "mispronunciation", "where": "12:40", "note": ""},
                                   {"kind": "pacing", "where": "9:40", "note": ""},
                                   {"kind": "omission", "where": "2:27", "note": ""},
                                   {"kind": "other", "where": "9:40", "note": ""}]}
    lines = [l for l in notes_for(META, finish).splitlines() if l.startswith("- ")]
    assert lines == ["- 2:27 omission", "- 9:40 pacing", "- 9:40 other", "- 12:40 mispronunciation"]
