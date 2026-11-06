#!/usr/bin/env python3
"""
Audio verification using Gemini's multimodal capabilities.
Compares a TTS audio recording against source text to identify
mispronunciations, missing content, and other issues.
"""

import argparse
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import httpx
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv
from pydub import AudioSegment
from pydub.silence import detect_silence

from text_to_speech import split_sections

load_dotenv()

DEFAULT_MODEL = "gemini-flash-latest"

# What a Gemini request other than an upload tells Google's server it may take (the SDK
# sends a request's timeout as X-Server-Timeout). On this side, StallLimit caps each
# socket operation at GEMINI_STALL_S, whatever this says: that is what keeps a request on
# a dead connection from hanging (review F3).
GEMINI_TIMEOUT_MS = 10 * 60 * 1000

# The longest a narration session may run (the pipeline's MAX_TIMEOUT_S;
# tests/test_verify_audio.py checks this covers it).
LONGEST_SESSION_S = 2 * 60 * 60

# What an upload's requests tell Google's server they may take (the SDK sends a request's
# timeout as X-Server-Timeout): each carries 8 MB of audio, which on a slow connection
# takes longer than GEMINI_TIMEOUT_MS. The stall limit below still ends a dead connection.
UPLOAD_TIMEOUT_MS = LONGEST_SESSION_S * 1000

# Longest a connection to Gemini may go without sending or receiving anything before
# it is taken as dead (review F3).
GEMINI_STALL_S = 10 * 60
# A request body goes out this many bytes at a time. Over TLS one socket write must
# finish within the write timeout, however steadily it moves, and the SDK uploads in
# 8 MB requests: in acceptance dry run 2 an upload slowed to about 30 KB/s by a backup
# failed with "The write operation timed out". At 64 KiB per write, a stall limit of
# 10 minutes ends only a connection that moves less than about 110 bytes a second.
WRITE_PIECE = 64 * 1024


class _InPieces(httpx.SyncByteStream):
    def __init__(self, stream, size: int, check_limit):
        self._stream, self._size, self._check_limit = stream, size, check_limit

    def __iter__(self):
        for part in self._stream:
            for i in range(0, len(part), self._size):
                self._check_limit()
                yield part[i:i + self._size]

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()


class StallLimit:
    """An httpx request hook for the Gemini requests of one check: no one socket
    operation of a request waits longer than stall_s, and its body is written
    WRITE_PIECE bytes at a time, so a transfer that keeps moving goes on however slow it
    is, and one that stops fails.

    The check as a whole stops once it has run longer than total_s, the longest a
    narration session may run: no request starts after that, and an upload stops between
    two pieces. By then the check's session has ended, and a check left running would go
    on using a slow link the next run needs (review R2.1).

    A hook, not a transport: an httpx client given a transport ignores the environment's
    proxies (review R1), and the SDK's client keeps its own TLS context."""

    def __init__(self, stall_s: float = GEMINI_STALL_S, total_s: float = LONGEST_SESSION_S):
        self.stall_s, self.total_s = stall_s, total_s
        self.deadline = time.monotonic() + total_s

    def __call__(self, request: httpx.Request) -> None:
        self._check_limit(request)
        asked = request.extensions.get("timeout", {})
        request.extensions["timeout"] = {
            k: self.stall_s if asked.get(k) is None else min(asked[k], self.stall_s)
            for k in ("connect", "read", "write", "pool")}
        request.stream = _InPieces(request.stream, WRITE_PIECE, lambda: self._check_limit(request))

    def _check_limit(self, request: httpx.Request) -> None:
        if time.monotonic() >= self.deadline:
            raise httpx.TimeoutException(
                f"the check has run longer than {self.total_s:g} s, the longest a narration "
                "session may run", request=request)


def gemini_http_options() -> genai_types.HttpOptions:
    """The options the verifier's Gemini client is built with."""
    return genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS,
                                   client_args={"event_hooks": {"request": [StallLimit()]}})

# Longest stretch of audio one generate_content call can be trusted with.
# Calibrated empirically on known audio (issue #4): at 11.4 minutes,
# repeated runs were consistent and caught a real mispronunciation every
# time; at 17.5 minutes, 1 in 4 runs reported phantom missing content
# and all 4 missed that same real issue; at 28 minutes phantoms were
# reproducible. 15 sits above every real [BREAK] section (max 13.5 min)
# and below the shortest length where comparison degraded.
MAX_SINGLE_CALL_MINUTES = 15

VERIFICATION_PROMPT = """Here is the source text of an article, and an audio recording of it being read aloud. Compare them carefully. Report:

- Words that are mispronounced or garbled
- Words or phrases that are missing from the audio
- Words or phrases in the audio that aren't in the source
- Pauses that are unnaturally long or short
- Any section where audio quality degrades

For each issue, give the approximate timestamp and quote the relevant source text.

If the recording is faithful to the source text with no issues, say "No issues found."

SOURCE TEXT:
{source_text}"""


def format_timestamp(ms: int) -> str:
    """Format a millisecond offset as M:SS (e.g. 754000 -> "12:34")."""
    total_seconds = int(ms // 1000)
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


# A time in a report: m:ss, mm:ss or h:mm:ss, not part of a longer number or word
# (v1:20b, 2.5:1) and not a clock time (10:30 a.m., 9:45pm).
_TIMESTAMP = re.compile(
    r"(?<![\w.:])(\d{1,3}):([0-5]\d)(?::([0-5]\d))?(?!\w|:\d|\.\d|\s?[AaPp]\.?[Mm]\b)")


# What the report quotes from the article, where a time is the article's own ("John
# 3:16"): text in straight or curly double quotes, *italics* (not **bold**, which the
# report uses for its own labels such as "**1:26 - Missing word:**"), and `code`.
_QUOTED = re.compile(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d|(?<!\*)\*(?!\*)[^*\n]+(?<!\*)\*(?!\*)|`[^`\n]*`')


def shift_timestamps(text: str, offset_ms: int) -> str:
    """Each time in a section's report moved by the section's start, written as
    format_timestamp writes it, so the report's times are times in the whole recording.
    A time inside text the report quotes from the article is left as it is."""
    quoted = [m.span() for m in _QUOTED.finditer(text)]

    def shift(m: re.Match) -> str:
        if any(a <= m.start() < b for a, b in quoted):
            return m.group()
        a, b, c = m.group(1), m.group(2), m.group(3)
        seconds = int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)
        return format_timestamp(seconds * 1000 + offset_ms)
    return _TIMESTAMP.sub(shift, text)


def _pace_stats(audio: AudioSegment, source_text: str) -> tuple[int, int, float]:
    """Return (duration_ms, word_count, wpm); [BREAK] markers are not words."""
    duration_ms = len(audio)
    word_count = len(re.sub(r"\[BREAK\]", " ", source_text).split())
    wpm = word_count / (duration_ms / 60000) if duration_ms else 0
    return duration_ms, word_count, wpm


def precheck_warnings(audio: AudioSegment, source_text: str) -> list[str]:
    """Deterministic warnings: pace outside 100-250 WPM, or silences over 5 s."""
    _, _, wpm = _pace_stats(audio, source_text)
    warnings = []
    if not 100 <= wpm <= 250:
        warnings.append(
            f"pace of {wpm:.0f} WPM is outside the normal speech "
            "range (100-250) — audio may be truncated or have extra content."
        )
    for start_ms, end_ms in detect_silence(
        audio, min_silence_len=5000, silence_thresh=-40, seek_step=100
    ):
        warnings.append(
            f"{format_timestamp(end_ms - start_ms)} of silence at "
            f"{format_timestamp(start_ms)} — far longer than a [BREAK] pause."
        )
    return warnings


def precheck_report(audio: AudioSegment, source_text: str) -> str:
    """Deterministic sanity checks that run before any model call.

    Reports audio duration, source word count, and effective words per
    minute, followed by any precheck_warnings. Costs nothing — no API
    call involved.
    """
    duration_ms, word_count, wpm = _pace_stats(audio, source_text)
    lines = [
        "## Pre-check (deterministic)",
        f"Duration: {format_timestamp(duration_ms)} | "
        f"{word_count} words | {wpm:.0f} WPM",
    ]
    lines += [f"WARNING: {w}" for w in precheck_warnings(audio, source_text)]
    return "\n".join(lines)


def find_section_gaps(
    audio: AudioSegment,
    min_silence_len: int = 2500,
    silence_thresh: int = -40,
) -> list:
    """Find the silence gaps inserted at [BREAK] section boundaries.

    text_to_speech.py inserts 2000ms of silence at each [BREAK] and only
    300ms between ordinary chunks — but Kokoro's own trailing quiet at
    paragraph ends stretches both: in real recordings ordinary joins
    measure ~1.5-2.0s of silence and [BREAK] joins ~3.2-3.4s. A 2500ms
    minimum separates the two populations. Returns a list of
    (start_ms, end_ms) tuples.
    """
    return [
        (start_ms, end_ms)
        for start_ms, end_ms in detect_silence(
            audio,
            min_silence_len=min_silence_len,
            silence_thresh=silence_thresh,
            seek_step=50,
        )
    ]


def split_audio_at_gaps(audio: AudioSegment, gaps: list) -> list:
    """Cut audio into section chunks at the midpoint of each gap.

    Returns a list of (start_ms, segment) tuples where start_ms is the
    chunk's absolute offset in the full recording. Cutting mid-gap keeps
    ~1s of silence padding on each side of the cut.
    """
    cut_points = [(start_ms + end_ms) // 2 for start_ms, end_ms in gaps]
    chunks = []
    previous_cut = 0
    for cut in cut_points + [len(audio)]:
        chunks.append((previous_cut, audio[previous_cut:cut]))
        previous_cut = cut
    return chunks


def check_single_call_limit(duration_ms: int, description: str) -> None:
    """Refuse to send more audio in one model call than is reliable.

    Raises ValueError with remediation guidance when duration_ms exceeds
    MAX_SINGLE_CALL_MINUTES.
    """
    if duration_ms > MAX_SINGLE_CALL_MINUTES * 60000:
        raise ValueError(
            f"{description} is {format_timestamp(duration_ms)} long, but "
            "reliable single-call verification tops out at "
            f"~{MAX_SINGLE_CALL_MINUTES:g} minutes — beyond that Gemini "
            "reports phantom discrepancies. Add [BREAK] markers to the "
            "text and regenerate the audio so verification can be chunked."
        )


def verify_audio(audio_path: str, source_text: str, model: str = DEFAULT_MODEL,
                 audio: AudioSegment | None = None) -> str:
    """Verify a TTS audio recording against its source text using Gemini.

    Runs a deterministic pre-check, then — when the audio's [BREAK]
    silence gaps pair up with the text's [BREAK] sections — verifies each
    section separately and aggregates the results. Otherwise verifies the
    whole file in one call, provided it's short enough to be reliable.

    Args:
        audio_path: Path to the audio file (.wav)
        source_text: The original text that was read aloud
        model: Gemini model to use for verification
        audio: The file already decoded, if the caller has it (the file is
            then read only to upload it)

    Returns:
        The pre-check stats followed by the model's assessment

    Raises:
        FileNotFoundError: If the audio file doesn't exist
        ValueError: If a single call would need more audio than
            MAX_SINGLE_CALL_MINUTES allows
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if audio is None:
        audio = AudioSegment.from_wav(audio_path)
    precheck = precheck_report(audio, source_text)

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options=gemini_http_options())

    sections = split_sections(source_text)
    gaps = find_section_gaps(audio)

    if len(sections) > 1 and len(gaps) + 1 == len(sections):
        report = _verify_chunked(client, audio, sections, gaps, model)
    else:
        check_single_call_limit(len(audio), "This audio")
        report = _verify_single(client, audio_path, source_text, model)
        if len(sections) > 1:
            report = (
                f"WARNING: audio has {len(gaps) + 1} audio chunk(s) at "
                f"[BREAK]-style silence gaps but the text has {len(sections)} "
                "sections — could not pair them, so the whole file was "
                f"verified in one call.\n\n{report}"
            )

    return f"{precheck}\n\n{report}"


def _wait_until_active(client, audio_file, poll_seconds: int = 2,
                       timeout_seconds: int = 300):
    """Block until an uploaded file finishes processing.

    files.upload() can return while the file is still PROCESSING; querying
    the model against it then behaves as if no audio were attached. Gives
    up after timeout_seconds so a file wedged in PROCESSING can't hang the
    tool indefinitely.
    """
    waited = 0
    while audio_file.state.name == "PROCESSING":
        if waited >= timeout_seconds:
            raise RuntimeError(
                f"Uploaded audio file still PROCESSING after "
                f"{timeout_seconds}s — giving up."
            )
        time.sleep(poll_seconds)
        waited += poll_seconds
        audio_file = client.files.get(name=audio_file.name)
    if audio_file.state.name != "ACTIVE":
        raise RuntimeError(
            f"Uploaded audio file ended in state {audio_file.state.name}"
        )
    return audio_file


def _verify_single(client, audio_path: str, source_text: str, model: str,
                   context_note: str = "") -> str:
    """Verify one audio file against its text in a single model call."""
    uploaded = client.files.upload(file=audio_path,
                                   config={"http_options": {"timeout": UPLOAD_TIMEOUT_MS}})
    audio_file = _wait_until_active(client, uploaded)

    prompt = VERIFICATION_PROMPT.format(source_text=source_text)
    if context_note:
        prompt = f"{context_note}\n\n{prompt}"

    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt, audio_file],
        )
    finally:
        # Best-effort cleanup so chunked runs don't pile uploads up against
        # the Files API quota (uploads otherwise linger ~48h).
        try:
            client.files.delete(name=audio_file.name)
        except Exception:
            pass

    return response.text


def _verify_chunked(client, audio: AudioSegment, sections: list,
                    gaps: list, model: str) -> str:
    """Verify each [BREAK] section separately and aggregate the results."""
    chunks = split_audio_at_gaps(audio, gaps)
    total = len(chunks)
    for i, (_, segment) in enumerate(chunks, start=1):
        check_single_call_limit(len(segment), f"Section {i} of {total}")
    parts = [
        "Note: the timestamps below are times in the whole recording; each "
        "section's header gives its range."
    ]

    with tempfile.TemporaryDirectory(prefix="verify_audio_") as temp_dir:
        for i, ((start_ms, segment), section_text) in enumerate(
            zip(chunks, sections), start=1
        ):
            chunk_path = os.path.join(temp_dir, f"section_{i}.wav")
            segment.export(chunk_path, format="wav")

            context_note = (
                f"This audio is section {i} of {total} of a longer recording; "
                "it may begin and end mid-article."
            )
            result = _verify_single(
                client, chunk_path, section_text, model, context_note
            )

            end_ms = start_ms + len(segment)
            header = (
                f"## Section {i} of {total} "
                f"({format_timestamp(start_ms)}–{format_timestamp(end_ms)})"
            )
            parts.append(f"{header}\n{shift_timestamps(result, start_ms)}")

    return "\n\n".join(parts)


def main(argv=None):
    """CLI entry point for audio verification."""
    parser = argparse.ArgumentParser(
        description="Verify a TTS audio recording against source text using Gemini"
    )
    parser.add_argument("audio_file", help="Path to the audio file (.wav)")
    parser.add_argument("text_file", help="Path to the source text file")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Gemini model to use (default: {DEFAULT_MODEL})"
    )

    args = parser.parse_args(argv)

    if not os.path.exists(args.audio_file):
        print(f"Error: Audio file '{args.audio_file}' not found.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.text_file):
        print(f"Error: Text file '{args.text_file}' not found.", file=sys.stderr)
        sys.exit(1)

    source_text = Path(args.text_file).read_text()
    try:
        result = verify_audio(args.audio_file, source_text, model=args.model)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        if "404" in str(e) or "NOT_FOUND" in str(e):
            print(
                f"Error: model '{args.model}' is not available — "
                f"try --model {DEFAULT_MODEL}. ({e})",
                file=sys.stderr,
            )
            sys.exit(1)
        raise
    print(result)


if __name__ == "__main__":
    main()
