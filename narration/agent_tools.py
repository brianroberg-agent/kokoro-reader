"""The six tools an agent session has. Plain functions over a work item;
the HTTP layer resolves the item from the caller's token (agent never names it).

Every tool holds its item's lock for the whole call. The SDK runs sync tools in
worker threads, and two parallel record calls at round 3 would otherwise both
pass the limit. A call that finds the lock held is refused, not queued. Every
read-modify-write of the item's state.json happens inside that lock.

An override's phonemes must come from the phonemes tool. words.check reads a
[word](/phonemes/) override as its displayed word and can't know what the
phonemes say, so a session could voice anything under the override's length cap.
So phonemes() takes one plain word (never link syntax, which misaki's
preprocessing would read as an override and echo back) and records every lookup
in the item's state, and finish accepts an override only if its phonemes are
what a lookup returned for its displayed word, or for a respelling of it
(words.is_override_respelling) that the call declares as a substitution, so the published
notes list it. Every declared substitution must be used, and declared once.

The claims finish accepts are about the text that was recorded: record keeps the
sha256 of the exact text it handed the engine (state["recorded_sha256"], which
the gate checks too), and check and finish refuse once prepared.md differs from
it. outcome "hold" is always accepted, whatever was recorded or checked: nothing
held is published.

validate_finish (the shapes of a finish call), recording_claims (what it claims
about the recording) and validate_substitutions (what its substitutions must
satisfy) are module functions because the gate runs them again
on the stored finish record: one implementation, so the gate can't refuse what
finish accepted, nor the other way round. For the same reason finish runs the gate's
speech comparison (gate.speech_matches) for outcome clean or minor, so a session
learns before finalize that the engine would say something the article doesn't."""
from __future__ import annotations

import difflib
import functools
import hashlib
import json
import os
import re
import unicodedata

from narration.words import (MAX_SUBSTITUTIONS, Substitution, audit, declaration_problems,
                             is_override_respelling, looks_like_link, max_prepared_length,
                             overrides, words)

MAX_RECORDS = 4
MAX_CHECKS = 8
OUTCOMES = ("clean", "minor", "hold")
# Matched with fullmatch: `$` alone would also accept a trailing newline.
VOICE_RE = re.compile(r"^[a-z]{2}_[a-z]{2,20}$")
MAX_FIELD = 200
# Characters a substitution's text may not have: HTML tags, link and image brackets,
# emphasis, code and table marks. Ordinary punctuation ("#5", "(5%)", "Yahoo!") is
# fine: with no line break and no "[", none of it can open a link, an image, a
# heading or a tag.
NOT_PLAIN = frozenset("<>[]{}*_`|\\")
# What a session writes that reaches the published episode notes (gate.notes_for):
# each issue's kind, from this list, and its timestamp, matched by WHERE_RE (its free
# note is for Brian, in result.json, never published); and each substitution's
# original and spoken text, which are bounded to one line of plain text (_plain: no
# markup or control characters, at most MAX_FIELD characters), and which the word
# check and its budgets bound to a few words of the article and what is read for them.
ISSUE_KINDS = ("mispronunciation", "pacing", "artifact", "truncation", "other")
# m:ss to mmm:ss in ASCII digits, seconds below 60; matched with fullmatch.
WHERE_RE = re.compile(r"^[0-9]{1,3}:[0-5][0-9]$")
MAX_ISSUES = 20
MAX_NOTE = 80
MAX_EDIT_TEXT = 20_000  # characters in replace_text's old, and in its new
MAX_WORD = 64           # characters in a word given to phonemes()
WORD_MARKS = "'’-."     # what a word given to phonemes() may have besides letters and marks
MAX_LOOKUPS = 200       # phonemes() lookups an item keeps, the latest
MAX_SHOWN = 5           # unproven overrides one refusal names
NEAR_MISS_RATIO = 0.5   # a lookup this like an override's word is explained as a near miss


class ToolError(Exception):
    """A refusal the agent can act on. Its message is shown to the agent."""


def _short(value, field, limit=MAX_FIELD):
    if not isinstance(value, str) or len(value) > limit:
        raise ToolError(f"{field} must be a string of at most {limit} characters")
    return value


def _issue(i):
    if not isinstance(i, dict):
        raise ToolError("each remaining issue must be an object with kind, where and note")
    kind, where = i.get("kind"), i.get("where")
    if kind not in ISSUE_KINDS:
        raise ToolError(f"an issue's kind must be one of {ISSUE_KINDS}; got {kind!r}")
    if not isinstance(where, str) or not WHERE_RE.fullmatch(where):
        raise ToolError(f"an issue's where must be a timestamp like 12:40; got {where!r}")
    return {"kind": kind, "where": where, "note": _short(i.get("note", ""), "note", MAX_NOTE)}


def _plain(value, field, optional=False):
    """One line of plain text: at most MAX_FIELD characters, a letter or digit in it,
    no line break, control or format character (a zero-width or direction mark), and
    none of NOT_PLAIN. Substitutions are published in the episode notes as written."""
    if optional and value == "":
        return value
    if (not isinstance(value, str) or len(value) > MAX_FIELD
            or not re.search(r"[^\W_]", value)
            or any(ch in NOT_PLAIN or unicodedata.category(ch)[0] == "C"
                   or unicodedata.category(ch) in ("Zl", "Zp") for ch in value)):
        raise ToolError(f"substitution {field} must be one line of plain text: at most "
                        f"{MAX_FIELD} characters, with a letter or digit, no line breaks or "
                        f"control characters, and none of {' '.join(sorted(NOT_PLAIN))}")
    return value


def _substitution(s):
    if not isinstance(s, dict):
        raise ToolError("each substitution must be an object with original, spoken and why")
    return {"original": _plain(s.get("original"), "original"),
            "spoken": _plain(s.get("spoken"), "spoken"),
            "why": _plain(s.get("why", ""), "why", optional=True)}


def _lookup_word(word) -> str:
    """A word for phonemes(): letters (with combining marks), apostrophes, hyphens and
    periods, at least one letter. No whitespace, digits, brackets or slashes: misaki's
    preprocessing reads "[word](/phonemes/)" as an override and returns its phonemes."""
    if (not isinstance(word, str) or not 0 < len(word) <= MAX_WORD
            or not all(ch.isalpha() or ch in WORD_MARKS or unicodedata.category(ch).startswith("M")
                       for ch in word)
            or not any(ch.isalpha() for ch in word)):
        raise ToolError(f"word must be a single word of 1-{MAX_WORD} characters: letters, with "
                        "apostrophes, hyphens or periods (no spaces, digits, brackets or slashes)")
    return word


def _speed(speed) -> float:
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise ToolError(f"speed must be a number between 0.5 and 2.0; got {speed!r}")
    if not 0.5 <= speed <= 2.0:  # NaN fails this too
        raise ToolError("speed must be between 0.5 and 2.0")
    return float(speed)


def _sha256(text: str) -> str:
    """The hash of the text as prepared.md stores it (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _latest_checks(state: dict) -> list[dict]:
    return [c for c in state["checks"] if c["record"] == state["records"]]


def _checks_used(state: dict) -> int:
    """Check calls so far, failed ones included (state["checks"] holds the ones
    that returned a report)."""
    return state.get("checks_used", len(state["checks"]))


def _changed(state: dict) -> str:
    n = state["records"]
    if n >= MAX_RECORDS:
        tail = "undo the edit (no record rounds are left), or finish with outcome 'hold'"
    elif _checks_used(state) >= MAX_CHECKS:  # a new recording could never be checked
        tail = ("undo the edit (no checks are left to verify a new recording), or finish "
                "with outcome 'hold'")
    else:
        tail = "record again, or undo the edit"
    return f"the prepared text changed since round {n} was recorded; {tail}"


def _matches(text: str, old: str) -> int:
    """How many times `old` appears in `text`; 2 or more also when two appearances
    overlap ("aa" in "aaa"), which str.count counts once."""
    first = text.find(old)
    if first < 0:
        return 0
    if text.find(old, first + 1) < 0:
        return 1
    return max(2, text.count(old))


def _unproven_overrides(state: dict, prepared: str, declared: list[tuple], limit: int):
    """Check each override in `prepared` against the recorded lookups. Returns
    messages for the ones not proven (stopping at `limit`), and the indices of the
    `declared` substitutions (as words() pairs) that declare an override's respelling.

    Proven: a lookup of the displayed word itself returned these phonemes, or a
    lookup of a respelling of it did and that respelling is declared. Each lookup
    word and each case-folded display goes through words() once, so a proven
    override costs a dictionary lookup; respellings are compared only against the
    (at most MAX_SUBSTITUTIONS) declarations, or, for a refusal's message, against
    the lookups of an unproven override."""
    looked_up: dict[str, dict[tuple, str]] = {}  # phonemes -> words() of a lookup -> its word
    for lookup in state.get("phoneme_lookups", []):
        looked_up.setdefault(lookup["phonemes"], {}).setdefault(
            tuple(words(lookup["word"])), lookup["word"])
    problems, used, seen = [], set(), set()
    for shown, phonemes in overrides(prepared):
        if (shown.casefold(), phonemes) in seen:
            continue
        seen.add((shown.casefold(), phonemes))
        found, display = looked_up.get(phonemes, {}), tuple(words(shown))
        if display and display in found:
            continue
        respelled = [k for k, (original, spoken) in enumerate(declared)
                     if original == display and spoken in found
                     and is_override_respelling(shown, found[spoken])]
        if respelled:
            used.update(respelled)
            continue
        problems.append(_unproven(shown, phonemes, found))
        if len(problems) >= limit:
            break
    return problems, used


def _near_miss(shown: str, word: str) -> bool:
    """Whether a lookup of `word` looks like an attempt to respell `shown`: the same
    first letter and at least NEAR_MISS_RATIO alike. Only such a lookup is explained
    in a refusal, with the substitution route; an unrelated one gets the plain
    refusal, so the server never suggests putting an unrelated word in the text."""
    a, b = " ".join(words(shown)), " ".join(words(word))
    return (bool(a and b) and a[0] == b[0]
            and difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= NEAR_MISS_RATIO)


def _unproven(shown: str, phonemes: str, found: dict) -> str:
    """Why an override isn't proven, and what to do about it."""
    override = f"[{shown}](/{phonemes}/)"
    respellings = sorted(word for word in found.values() if is_override_respelling(shown, word))
    if respellings:
        return (f"override {override} uses the phonemes the phonemes tool gave for "
                f"{respellings[0]!r}, a respelling of {shown!r}: declare the respelling in "
                f"substitutions (original {shown!r}, spoken {respellings[0]!r}) so the notes "
                f"list it, or call phonemes({shown!r}) and use its result")
    near = sorted(word for word in found.values() if _near_miss(shown, word))
    if near:  # a lookup that tried to respell this word, but too loosely
        near = near[0]
        return (f"override {override} uses the phonemes the phonemes tool gave for {near!r}, "
                f"which is too far from {shown!r} to count as a respelling of it; call "
                f"phonemes({shown!r}) and use its result, or write {near!r} in the text with "
                f"replace_text and declare it as a substitution (original {shown!r}, spoken "
                f"{near!r})")
    # No IPA in it (or a slash): most likely a link to a path, which clean_markdown_text
    # keeps as an override. RunnerTools.open unwraps the article's own links.
    hint = (f"if it is a link, not an override, unwrap it: replace {override} with {shown}; "
            "otherwise " if looks_like_link(phonemes) else "")
    return (f"override {override} was not produced by the phonemes tool for {shown!r}; "
            f"{hint}call phonemes({shown!r}) and use its result")


def _locked(method):
    """Run a tool method under its item's lock; refuse if another call holds it."""
    @functools.wraps(method)
    def wrapper(self, item_id, *args, **kwargs):
        if not self.store.exists(item_id):
            raise ValueError("no such work item")
        lock = self.store.item_lock(item_id)
        if not lock.acquire(blocking=False):
            raise ToolError("another call is in progress for this work item; wait for it to finish")
        try:
            return method(self, item_id, *args, **kwargs)
        finally:
            lock.release()
    return wrapper


class AgentTools:
    def __init__(self, store, tts, verifier, g2p, default_voice="af_heart"):
        self.store, self.tts, self.verifier, self.g2p = store, tts, verifier, g2p
        self.default_voice = default_voice

    def _open_state(self, item_id):
        state = self.store.load_state(item_id)
        if state["status"] != "open":
            raise ToolError(f"this work item is {state['status']}; no further tool calls are accepted")
        return state

    def _unchanged(self, item_id, state, text=None) -> bool:
        """Whether prepared.md is still the text the latest round recorded."""
        if text is None:
            text = self.store.read_text(item_id, "prepared.md")
        return _sha256(text) == state.get("recorded_sha256")

    def _what_next(self, item_id, state) -> str:
        """How the session can still finish, for a refusal at a limit."""
        n, latest = state["records"], _latest_checks(state)
        hold = "finish with outcome 'hold'"
        if (not n or state["recorded_round"] != n or (latest and latest[-1]["warnings"])
                or (not latest and _checks_used(state) >= MAX_CHECKS)):
            return hold
        steps = []
        if not self._unchanged(item_id, state):
            steps.append(f"undo the edits made since round {n} was recorded")
        if not latest:
            steps.append(f"call check on round {n}")
        return ", then ".join(steps + ["finish with outcome clean or minor"]) + f"; or {hold}"

    @_locked
    def get_text(self, item_id):
        state = self._open_state(item_id)
        return {"prepared_text": self.store.read_text(item_id, "prepared.md"),
                "metadata": json.loads(self.store.read_text(item_id, "metadata.json")),
                "records_used": state["records"], "records_left": MAX_RECORDS - state["records"]}

    @_locked
    def replace_text(self, item_id, old, new):
        self._open_state(item_id)
        for field, value in (("old", old), ("new", new)):
            if not isinstance(value, str):
                raise ToolError(f"{field} must be a string")
            if len(value) > MAX_EDIT_TEXT:
                raise ToolError(f"{field} is longer than {MAX_EDIT_TEXT} characters")
        if not old:
            raise ToolError("old must not be empty")
        text = self.store.read_text(item_id, "prepared.md")
        found = _matches(text, old)
        if found != 1:
            raise ToolError(f"old must appear exactly once in the prepared text; found {found}")
        result = text.replace(old, new, 1)
        limit = max_prepared_length(len(self.store.read_text(item_id, "source.md")))
        if len(result) > limit:
            raise ToolError(f"this edit would make the prepared text {len(result)} characters "
                            f"long; the limit is {limit} (twice the article's length plus 10,000)")
        self.store.write_text(item_id, "prepared.md", result)
        return {"ok": True}

    @_locked
    def record(self, item_id, voice=None, speed=1.0):
        state = self._open_state(item_id)
        voice = self.default_voice if voice is None else voice
        if not isinstance(voice, str) or not VOICE_RE.fullmatch(voice):
            raise ToolError(f"voice must look like 'af_heart'; got {voice!r}")
        speed = _speed(speed)
        if state["records"] >= MAX_RECORDS:
            raise ToolError(f"record limit reached ({MAX_RECORDS} per article); "
                            + self._what_next(item_id, state))
        if _checks_used(state) >= MAX_CHECKS:
            # A new round could never be checked, so it could only replace audio that may
            # have been checked clean.
            raise ToolError(f"no checks are left to verify a new recording ({MAX_CHECKS} per "
                            "article); " + self._what_next(item_id, state))
        text = self.store.read_text(item_id, "prepared.md")
        # finish refuses speech the article doesn't have; say so now, before the round
        # is spent, without refusing. Imported here: the gate imports this module.
        from narration.gate import speech_warnings
        warnings = speech_warnings(self.g2p, self.store.read_text(item_id, "source.md"), text)
        state["records"] += 1  # counted before the engine runs
        self.store.save_state(item_id, state)
        final = self.store.path(item_id, "recording.wav")
        tmp = final.with_name(".tmp-recording.wav")
        try:
            duration = self.tts.record(text, tmp, voice, speed)
            os.replace(tmp, final)  # the previous round's audio stays until this one succeeds
        finally:
            tmp.unlink(missing_ok=True)  # a failed run's partial file
        state["recorded_round"] = state["records"]
        state["recorded_sha256"] = _sha256(text)  # what this round's audio says
        self.store.save_state(item_id, state)
        return {"round": state["records"], "duration_seconds": round(duration, 1),
                "records_left": MAX_RECORDS - state["records"], "speech_warnings": warnings}

    @_locked
    def check(self, item_id):
        state = self._open_state(item_id)
        n = state["records"]
        if n == 0:
            raise ToolError("nothing to check yet: call record first")
        wav = self.store.path(item_id, "recording.wav")
        if state["recorded_round"] != n or not wav.exists():
            raise ToolError(f"record round {n} failed and left no recording; "
                            + ("call record again" if n < MAX_RECORDS
                               else "no record rounds are left, so finish with outcome 'hold'"))
        if _checks_used(state) >= MAX_CHECKS:
            raise ToolError(f"check limit reached ({MAX_CHECKS} per article); "
                            + self._what_next(item_id, state))
        text = self.store.read_text(item_id, "prepared.md")
        if not self._unchanged(item_id, state, text):
            raise ToolError(_changed(state))
        attempt = _checks_used(state) + 1
        state["checks_used"] = attempt  # counted before the verifier runs, as record is
        self.store.save_state(item_id, state)
        report, warnings = self.verifier.check(wav, text)
        self.store.write_text(item_id, f"report-{attempt}.txt", report)
        state["checks"].append({"n": attempt, "record": n, "warnings": list(warnings)})
        self.store.save_state(item_id, state)
        return {"report": report, "precheck_warnings": list(warnings)}

    @_locked
    def phonemes(self, item_id, word):
        state = self._open_state(item_id)
        out = str(self.g2p.phonemes(_lookup_word(word)))
        # finish accepts an override only with phonemes recorded here (module docstring).
        lookups = state.get("phoneme_lookups", []) + [{"word": word, "phonemes": out}]
        state["phoneme_lookups"] = lookups[-MAX_LOOKUPS:]
        self.store.save_state(item_id, state)
        return {"word": word, "phonemes": out}

    @_locked
    def finish(self, item_id, outcome, remaining_issues, substitutions):
        state = self._open_state(item_id)
        issues, subs_in = validate_finish(outcome, remaining_issues, substitutions)
        if outcome != "hold":  # nothing held is published, so hold needs no claims
            self._check_claims(item_id, state, subs_in)
        state["finish"] = {"outcome": outcome, "remaining_issues": issues, "substitutions": subs_in}
        state["status"] = "finished"
        self.store.save_state(item_id, state)
        self.store.write_text(item_id, "result.json", json.dumps(state["finish"]))
        self.store.revoke(item_id)
        return {"ok": True}

    def _check_claims(self, item_id, state, subs_in):
        """What outcome clean or minor claims: the latest round recorded the text as it
        is now, a check of that round found no pre-check warnings, the text says what
        the article says with exactly the declared substitutions
        (validate_substitutions), and the engine would say what it says for the
        article with them (gate.speech_matches). The gate runs both again."""
        n = state["records"]
        hold = "finish with outcome 'hold'"
        prepared = self.store.read_text(item_id, "prepared.md")
        problems = recording_claims(state, prepared)
        if problems:
            raise ToolError(problems[0])
        source = self.store.read_text(item_id, "source.md")
        validate_substitutions(state, source, prepared, subs_in)
        # Imported here: the gate imports this module.
        from narration.gate import speech_matches
        subs = [Substitution(s["original"], s["spoken"]) for s in subs_in]
        problems = speech_matches(self.g2p, source, prepared, subs)
        if problems:
            # Not "declare it": the words agree, so a substitution can't (case is folded).
            tail = (f"Then record and check again; or {hold}." if n < MAX_RECORDS
                    else f"No record rounds are left to fix it: {hold}.")
            raise ToolError(f"{problems[0]}. {tail}")


def recording_claims(state: dict, prepared: str) -> list[str]:
    """What outcome clean or minor claims about the recording that fails, in order;
    [] when it all holds: something was recorded, the latest round left a recording,
    of `prepared` as it is now (state["recorded_sha256"]), and a check of that round
    returned a report with no pre-check warnings. finish refuses with the first; the
    gate holds with all of them. Each says what the session can still do."""
    n = state.get("records", 0)
    hold = "finish with outcome 'hold'"
    if not n:
        return ["nothing is recorded yet: record and check before finishing with outcome "
                f"clean or minor, or {hold}"]
    if state.get("recorded_round") != n:
        return [f"record round {n} failed and left no recording; "
                + (f"call record again, then check, before finish; or {hold}"
                   if n < MAX_RECORDS else f"no record rounds are left, so {hold}")]
    problems = []
    if _sha256(prepared) != state.get("recorded_sha256"):
        problems.append(_changed(state))
    latest = _latest_checks(state)
    if not latest:
        problems.append(f"the latest recording (round {n}) has no check, and no checks are "
                        f"left; {hold}" if _checks_used(state) >= MAX_CHECKS
                        else f"call check on the latest recording before finish; or {hold}")
    elif latest[-1]["warnings"]:
        flagged = "; ".join(latest[-1]["warnings"]).rstrip(".")
        no_round, no_check = n >= MAX_RECORDS, _checks_used(state) >= MAX_CHECKS
        advice = ("No record round or check is left to fix it" if no_round and no_check
                  else "No record round is left to fix it" if no_round
                  else "No check is left to verify a fix" if no_check else "")
        advice = (f"{advice}: finish with outcome 'hold'." if advice
                  else "Fix and re-record, or finish with outcome 'hold'.")
        problems.append(f"the latest check's pre-check flagged: {flagged}. {advice}")
    return problems


def validate_finish(outcome, remaining_issues, substitutions) -> tuple[list[dict], list[dict]]:
    """The shapes a finish call may have, as (issues, substitutions) in the form
    state["finish"] keeps them; ToolError if any is wrong. finish checks its arguments
    with this, and the gate checks the stored finish record with it again, since only
    an issue's kind and timestamp, and each substitution, reach the published notes."""
    if outcome not in OUTCOMES:
        raise ToolError(f"outcome must be one of {OUTCOMES}")
    # Both counts are bounded before any entry is read, so a huge list costs nothing.
    if not isinstance(remaining_issues, list) or len(remaining_issues) > MAX_ISSUES:
        raise ToolError(f"remaining_issues must be a list of at most {MAX_ISSUES}")
    if not isinstance(substitutions, list) or len(substitutions) > MAX_SUBSTITUTIONS:
        raise ToolError(f"substitutions must be a list of at most {MAX_SUBSTITUTIONS} "
                        "substitutions")
    issues = [_issue(i) for i in remaining_issues]
    subs_in = [_substitution(s) for s in substitutions]
    if outcome == "clean" and issues:
        raise ToolError("outcome 'clean' means no remaining issues: use 'minor' for small "
                        "ones, or fix them")
    if outcome != "clean" and not issues:
        raise ToolError(f"outcome {outcome!r} needs at least one remaining issue saying what "
                        "is wrong")
    return issues, subs_in


def validate_substitutions(state: dict, source: str, prepared: str, subs_in: list[dict]) -> None:
    """The one check of a finish call's substitutions, which finish and the gate both
    run, so the gate can't refuse what finish accepted or the other way round. Raises
    ToolError at the first rule that fails:
    - each substitution is declared once (compared as words());
    - each is allowed (words.declaration_problems: short, its original in the article,
      replacing some of the article's words, adding no address the article lacks);
    - the prepared text says what the article says, with them (words.audit);
    - every override's phonemes came from a phonemes() lookup (_unproven_overrides);
    - every declaration is used: audit applies it at least once, or it is an
      override-respelling, {original: an override's display, spoken: the word whose
      lookup gave its phonemes}, which audit applies 0 times because it names an
      override's pronunciation, not a change to the text.
    `state` supplies the item's lookups (state["phoneme_lookups"])."""
    declared = [(tuple(words(s["original"])), tuple(words(s["spoken"]))) for s in subs_in]
    twice = [s for k, s in enumerate(subs_in) if declared[k] in declared[:k]]
    if twice:
        raise ToolError("; ".join(f"substitution {s['original']!r} -> {s['spoken']!r} is "
                                  "declared twice" for s in twice) + ". Declare each once.")
    subs = [Substitution(s["original"], s["spoken"]) for s in subs_in]
    problems, applied = audit(source, prepared, subs)
    if problems:
        # audit reports refused declarations alone; say so, rather than that the text
        # differs. Looked for only on this path, so a passing finish pays nothing.
        refused = declaration_problems(source, subs)
        if refused:
            raise ToolError("; ".join(refused) + ". Fix or remove these substitutions.")
        raise ToolError("the prepared text differs from the article: " + "; ".join(problems)
                        + ". Declare each word change in substitutions, or undo it.")
    unproven, respellings = _unproven_overrides(state, prepared, declared, MAX_SHOWN + 1)
    if unproven:
        raise ToolError("; ".join(unproven[:MAX_SHOWN])
                        + ("; and more overrides like these" if len(unproven) > MAX_SHOWN else ""))
    unused = [s for k, (s, sub) in enumerate(zip(subs_in, subs))
              if not applied.get(sub) and k not in respellings]
    if unused:
        raise ToolError("; ".join(f"substitution {s['original']!r} -> {s['spoken']!r} is "
                                  "declared but the prepared text does not use it"
                                  for s in unused) + ". Remove it from substitutions.")
