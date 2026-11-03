"""The authoritative pre-publish gate (spec: "Quality gate").

finish accepts outcome clean or minor only when its claims hold; the gate checks
them again on the files as they are at finalize time, with the code finish uses
where it can, so it can't publish what finish would have refused, nor refuse what
finish accepted:
- the finish record has the shapes finish allows (agent_tools.validate_finish:
  only an issue's kind and timestamp, and each substitution, reach the notes);
- the latest record round left a recording, of the text prepared.md holds now
  (state["recorded_sha256"]), and a check of that round returned a report (a
  failed check is counted in checks_used but has no entry) whose pre-check found
  nothing, no truncation and no long silence (agent_tools.recording_claims, the
  code finish runs);
- the prepared text says what the article says, with exactly the declared
  substitutions, and every override's phonemes came from a lookup
  (agent_tools.validate_substitutions, the code finish runs);
- the engine would say the same for the prepared text as for the article with
  those substitutions and the prepared text's overrides (speech_matches, which
  finish runs too). The word check can't fully mirror how misaki reads signs,
  symbols and abbreviations; this compares misaki's own output. A G2P that fails
  raises, as in finish: the runner retries, and nothing is decided.
An agent's outcome "hold" is a hold, whatever else is true: nothing held is
published, so its claims aren't checked, as finish doesn't check them. Pure,
apart from the G2P it is handed."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from narration.agent_tools import (ToolError, _issue, _substitution, recording_claims,
                                   text_substitutions, validate_finish, validate_substitutions)
from narration.words import _PHONETIC, Substitution, apply_with_reason, check, engine_text

# misaki's stress and length marks: kept in the comparison with the letters. Its
# punctuation, pause marks and spaces are not compared.
_MARKS = frozenset("ˈˌːˑ")
_AROUND = 20  # phoneme characters shown on each side of a difference
_NEAR = 3     # words of text quoted on each side of it
# The advice for a difference where the words agree. Most often it is a pause the
# session added (misaki reads some words differently before one), so that comes first.
# Then the override rule: the word check folds case, so a substitution can't declare
# a case or spelling change the engine reads differently.
MISMATCH_ADVICE = ("Make the prepared text read as the article does there (move or remove the "
                   "pause, if you added one); if the change is for pronunciation, use a "
                   "phonetic override from phonemes() instead of changing case or spelling")
# What record's advisory says when the words differ, so it compared nothing.
NOT_COMPARED = ("not compared: the prepared text differs from the article in words; finish "
                "compares it with your declared substitutions")
log = logging.getLogger(__name__)


@dataclass
class GateResult:
    action: str                      # "publish" | "hold" | "retry"
    outcome: str | None
    reasons: list[str] = field(default_factory=list)
    notes: str = ""


_TEXT_FIELDS = ("title", "publication", "issue_number", "issue_season", "date_published",
                "description")


def _text(value) -> str:
    """A metadata field as text: a string (stripped), a number as written, else ""."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _meta(metadata) -> dict:
    """The metadata as the notes, the MP3 tags and status read it, whatever shape
    article-assistant gave it: each text field a string ("" when missing, null or not
    text), and authors a list of non-empty strings (a lone string is one author;
    null, empty and non-text entries are dropped)."""
    if not isinstance(metadata, dict):
        metadata = {}
    authors = metadata.get("authors")
    if isinstance(authors, str):
        authors = [authors]
    authors = [a.strip() for a in authors if isinstance(a, str) and a.strip()] \
        if isinstance(authors, list) else []
    return {**{k: _text(metadata.get(k)) for k in _TEXT_FIELDS}, "authors": authors}


def album_for(metadata: dict) -> str:
    """"<publication>, No. <n> (<season>)", or just the publication when there's no
    edition."""
    meta = _meta(metadata)
    edition = (f"No. {meta['issue_number']} ({meta['issue_season']})"
               if meta["issue_number"] and meta["issue_season"] else "")
    return ", ".join(part for part in (meta["publication"], edition) if part)


def notes_for(metadata: dict, finish: dict) -> str:
    """The published episode notes. An issue appears as its timestamp and kind only,
    which agent_tools checks against a pattern and a fixed list; its free-text note
    is for Brian, in result.json. Every declared substitution is listed,
    override-respellings included, its original and spoken text as written: agent_tools
    allows one line of plain text for each. Each entry is checked again here with
    finish's own validators, and one that fails (in a record written before a rule
    existed) is left out. Issues are listed in time order."""
    issues = sorted(_valid(_issue, finish.get("remaining_issues")), key=_seconds)
    subs = _valid(_substitution, finish.get("substitutions"))
    parts = [_meta(metadata)["description"]]
    if issues:
        parts.append("Known issues in this recording:")
        parts += [f"- {i['where']} {i['kind']}" for i in issues]
    if subs:
        parts.append("Read with these substitutions:")
        parts += [f"- \"{s['original']}\" read as \"{s['spoken']}\"" for s in subs]
    parts.append("Recorded automatically.")
    return "\n".join(p for p in parts if p)


def _seconds(issue: dict) -> int:
    """An issue's place in seconds: `where` is m:ss to mmm:ss, checked by _issue first."""
    minutes, seconds = issue["where"].split(":")
    return int(minutes) * 60 + int(seconds)


def _valid(validator, entries) -> list[dict]:
    """The entries that pass `validator` (agent_tools' own), as it returns them."""
    valid = []
    for entry in entries if isinstance(entries, list) else []:
        try:
            valid.append(validator(entry))
        except ToolError:
            continue
    return valid


def speech_matches(g2p, source: str, prepared: str, substitutions: list[Substitution]) -> list[str]:
    """Problems with what the engine would say for `prepared`, against what it would
    say for the article with the declared substitutions and the prepared text's
    overrides (words.apply); [] when the two sound the same. Each text is prepared for
    the engine exactly once, as the engine prepares it: the prepared text by
    words.engine_text, the article's side by apply itself (its result is already in
    that form, and cleaning is not idempotent: "***x***" cleaned twice loses the
    asterisks the engine says). Both go through the engine's own G2P and are compared
    as letters and stress and length marks only, so pacing punctuation, pauses and
    paragraph breaks are free. One problem at most, for the first place they differ."""
    applied, why = apply_with_reason(source, prepared, substitutions)
    if applied is None:
        if check(source, prepared, substitutions):  # it says where; this only says so
            why += " (the word check says where)"
        return [f"{why}, so what the engine would say could not be compared with the article"]
    prepared_text = engine_text(prepared)
    # apply's result is already as the engine reads it: not prepared a second time
    said, heard = g2p.phonemes_for_text(prepared_text), g2p.phonemes_for_text(applied)
    (a, a_at), (b, b_at) = _sounds(said), _sounds(heard)
    if a == b:
        return []
    d = len(os.path.commonprefix([a, b]))
    pa = a_at[d] if d < len(a_at) else len(said)
    pb = b_at[d] if d < len(b_at) else len(heard)
    la, wa = _near(prepared_text, said, pa)
    lb, wb = _near(applied, heard, pb)
    return [f"the prepared text would not sound like the article: in line {la} of the prepared "
            f"text, near {wa!r}, the engine would say '{_around(said, pa)}', where the article, "
            f"with the declared substitutions (line {lb}, near {wb!r}), says "
            f"'{_around(heard, pb)}'. {MISMATCH_ADVICE}"]


def speech_warnings(g2p, source: str, prepared: str) -> list[str]:
    """What record warns about before the engine runs: speech_matches, with no
    substitutions, since a session declares them only at finish. Where the words
    differ nothing is compared, and the warning says so (NOT_COMPARED): finish's word
    check judges that, with the declared substitutions. It only advises, so nothing
    it raises stops a record: a comparison that can't run is a warning too, logged."""
    try:
        if check(source, prepared, []):
            return [NOT_COMPARED]
        return speech_matches(g2p, source, prepared, [])
    except RuntimeError as e:  # the G2P adapter's failures, which keep server paths out
        log.warning("record's speech comparison could not run: %s", e)
        return [f"the speech comparison could not run: {e}"]
    except Exception:  # a bug: its message is not for the agent
        log.exception("record's speech comparison could not run")
        return ["the speech comparison could not run (an internal error, logged on the server)"]


def _near(text: str, phonemes: str, at: int) -> tuple[int, str]:
    """The line (from 1) of `text` that the phonemes at `at` were read from, and its
    words around the word they belong to, overrides shown as their words. The word is
    found by counting the phoneme words before it, so it is an estimate: a number or
    a symbol can be read as several words, punctuation as none."""
    line = phonemes.count("\n", 0, at)
    lines = [part for part in re.split(r"\n+", text.strip()) if part.strip()]
    if line >= len(lines):
        return line + 1, ""
    before = phonemes[phonemes.rfind("\n", 0, at) + 1:at]
    index = len(before.split()) - (1 if before and not before[-1].isspace() else 0)
    shown = _PHONETIC.sub(r"\1", lines[line]).split()
    index = min(index, len(shown) - 1)
    return line + 1, " ".join(shown[max(0, index - _NEAR):index + _NEAR + 1])


def _sounds(phonemes: str) -> tuple[str, list[int]]:
    """The phonemes as compared (letters and misaki's stress and length marks), and
    where each kept character is in `phonemes`."""
    at = [k for k, ch in enumerate(phonemes) if ch.isalpha() or ch in _MARKS]
    return "".join(phonemes[k] for k in at), at


def _around(phonemes: str, at: int) -> str:
    start, end = max(0, at - _AROUND), at + _AROUND
    shown = phonemes[start:end].replace("\n", " / ")
    return ("…" if start else "") + shown + ("…" if end < len(phonemes) else "")


def evaluate(state: dict, source: str, prepared: str, metadata: dict, g2p) -> GateResult:
    finish = state.get("finish")
    if not finish:
        return GateResult("retry", None, ["the session ended without calling finish"])
    try:
        issues, subs = validate_finish(finish.get("outcome"), finish.get("remaining_issues"),
                                       finish.get("substitutions"))
    except ToolError as e:
        # Nothing from a malformed record reaches the notes.
        return GateResult("hold", None, [f"the finish record is malformed: {e}"],
                          notes_for(metadata, {}))
    outcome = finish["outcome"]
    notes = notes_for(metadata, {"remaining_issues": issues, "substitutions": subs})
    if outcome == "hold":
        held = "; ".join(f"{i['where']} {i['kind']}: {i['note']}" for i in issues)
        return GateResult("hold", outcome, ["the agent held it" + (f" ({held})" if held else "")],
                          notes)
    reasons = recording_claims(state, prepared)
    try:
        validate_substitutions(state, source, prepared, subs)
    except ToolError as e:
        reasons.append(str(e))
    if not reasons:  # the slow check last: its answer can't change a hold
        # A G2P failure (RuntimeError) propagates: finalize then leaves the item
        # finished, for the runner to retry, rather than holding a good recording.
        reasons += speech_matches(g2p, source, prepared, text_substitutions(subs))
    return GateResult("hold" if reasons else "publish", outcome, reasons, notes)
