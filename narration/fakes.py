"""Fake engines for tests and for `python -m narration --fake-engines`."""
from __future__ import annotations

import re
from pathlib import Path


class FakeTTS:
    def __init__(self):
        self.calls = 0
        self.fail_next = False
        self.hold = None  # (started, release) Events: when set, a run blocks between them
        self.hold_timeout = 10.0  # seconds a held run waits for release before it fails

    def record(self, text, out_path, voice, speed):
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            Path(out_path).write_bytes(b"RIFF partial")  # a crash mid-write
            raise RuntimeError("fake engine failure")
        if self.hold:
            started, release = self.hold
            started.set()
            if not release.wait(self.hold_timeout):
                raise RuntimeError("fake engine held, and never released")
        Path(out_path).write_bytes(b"RIFF fake wav")
        return 60.0


class FakeVerifier:
    def __init__(self):
        self.report = "No issues found."
        self.warnings: list[str] = []

    def check(self, audio_path, text):
        return self.report, list(self.warnings)


class FakeG2P:
    """Deterministic phonemes. A lookup always answers "fəˈnɛt". A whole text is read
    line by line, as the engine's pipeline splits it: each line's words lowercased and
    joined by spaces, an override "[word](/p/)" read as p, punctuation dropped."""
    _SPEECH = re.compile(r"\[[^\[\]]+\]\(/([^)\[]*)/\)|[^\W_]+(?:'[^\W_]+)*")

    def phonemes(self, word):
        return "fəˈnɛt"

    def phonemes_for_text(self, text):
        lines = [line for line in re.split(r"\n+", text.strip()) if line.strip()]
        return "\n".join(" ".join(m.group(1).strip("/") if m.group(1) is not None
                                  else m.group().lower() for m in self._SPEECH.finditer(line))
                         for line in lines)


class FakeLinkG2P(FakeG2P):
    """Like misaki with its preprocessing on: "[word](/phonemes/)" link syntax in the
    input comes back as the phonemes inside it. For tests that such input never
    reaches the G2P."""
    _LINK = re.compile(r"\[[^\]]+\]\(/([^)]*)/\)")

    def phonemes(self, word):
        link = self._LINK.search(word)
        return link.group(1) if link else super().phonemes(word)


class FakeConverter:
    def __init__(self):
        self.calls = []

    def to_mp3(self, wav, mp3, title, artist, album):
        self.calls.append((title, artist, album))
        Path(mp3).write_bytes(b"ID3 fake mp3")
