"""Engine interfaces and the real adapters over the existing toolkit.

Imports of the heavy toolkit modules happen inside methods, so this module
(and everything that imports it) stays importable without MLX or Gemini.

The adapters report a failure the agent should see as a RuntimeError (the HTTP
layer shows those to the agent) and keep server paths out of its message.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Protocol

# The MCP SDK runs sync tools in worker threads, and one lock each keeps two calls
# from running at once. text_to_speech.generate_audio loads the engine's model afresh
# on every record (no model is shared between calls), and two loaded at once would
# double the memory and compete for the GPU; the misaki instance is shared by the
# whole process.
_TTS_LOCK = threading.Lock()
_G2P_LOCK = threading.Lock()


class TTSEngine(Protocol):
    def record(self, text: str, out_path: Path, voice: str, speed: float) -> float: ...


class Verifier(Protocol):
    def check(self, audio_path: Path, text: str) -> tuple[str, list[str]]: ...


class G2P(Protocol):
    def phonemes(self, word: str) -> str: ...

    def phonemes_for_text(self, text: str) -> str:
        """What the engine says for a whole text: read line by line as the engine's
        pipeline splits it (the text stripped, split on runs of newlines, blank lines
        skipped), override syntax read as the engine reads it; one line of phonemes
        per line read, joined with newlines."""
        ...


class Converter(Protocol):
    def to_mp3(self, wav: Path, mp3: Path, title: str, artist: str, album: str) -> None: ...


def _failed(what: str, error: BaseException) -> RuntimeError:
    """A RuntimeError saying what failed, without a server path: an OSError's
    message names its file, and a decoding error carries ffmpeg's output."""
    from pydub.exceptions import CouldntDecodeError
    if isinstance(error, CouldntDecodeError):
        detail = "the audio could not be decoded"
    elif isinstance(error, OSError):
        detail = error.strerror or type(error).__name__
    else:
        detail = str(error)
    return RuntimeError(f"{what} failed: {detail}")


class KokoroEngine:
    def record(self, text, out_path, voice, speed):
        from pydub import AudioSegment
        from pydub.exceptions import CouldntDecodeError
        from narration.words import normalize_spaces
        from text_to_speech import clean_markdown_text, generate_audio
        # The CLI's chain for a .md file: clean_markdown_text, then generate_audio
        # (which only runs prepare_for_tts). Without the cleaning, headings,
        # emphasis markers and link URLs are read aloud, and words.check can't
        # catch it because it strips exactly that markup. Between the two, every
        # space but a newline is made plain, as words.check reads the text: misaki
        # misplaces an override after a no-break, thin or ideographic space or a
        # tab earlier on its line.
        text = normalize_spaces(clean_markdown_text(text))
        try:
            with _TTS_LOCK:
                generate_audio(text, str(out_path), voice=voice, speed=speed)
            return len(AudioSegment.from_file(out_path)) / 1000
        except (CouldntDecodeError, OSError) as e:
            raise _failed("recording", e) from e


class GeminiVerifier:
    def check(self, audio_path, text):
        import httpx
        from google.genai import errors as genai_errors
        from pydub import AudioSegment
        from pydub.exceptions import CouldntDecodeError
        from verify_audio import precheck_warnings, verify_audio
        try:
            audio = AudioSegment.from_wav(str(audio_path))  # decoded once, for both
            warnings = precheck_warnings(audio, text)
            return verify_audio(str(audio_path), text, audio=audio), warnings
        except (CouldntDecodeError, OSError, genai_errors.APIError, httpx.HTTPError) as e:
            raise _failed("verification", e) from e


class MisakiG2P:
    def __init__(self):
        from misaki import en, espeak
        # The engine's own configuration (mlx_audio's Kokoro pipeline for American
        # English). A bare en.G2P() has no fallback and answers "❓" for every word
        # outside misaki's lexicon, which is most names, the words overrides are for.
        self._g2p = en.G2P(trf=False, british=False,
                           fallback=espeak.EspeakFallback(british=False), unk="")

    def phonemes(self, word):
        # preprocess=False: misaki's preprocessing reads "[word](/phonemes/)" link
        # syntax as an override and would hand back the phonemes inside it.
        try:
            with _G2P_LOCK:
                out = self._g2p(word, preprocess=False)
        except OSError as e:
            raise _failed("the pronunciation lookup", e) from e
        return out[0] if isinstance(out, tuple) else str(out)

    def phonemes_for_text(self, text):
        # As mlx_audio's Kokoro pipeline reads its input (split_pattern r"\n+"), with
        # misaki's preprocessing on, as the engine has it: here "[word](/phonemes/)"
        # is the engine's own override syntax, read as the engine reads it. One line
        # per lock, so lookups from other sessions aren't held up for a whole article.
        out = []
        for line in re.split(r"\n+", text.strip()):
            if not line.strip():
                continue
            try:
                with _G2P_LOCK:
                    result = self._g2p(line)
            except OSError as e:
                raise _failed("the speech comparison", e) from e
            out.append(result[0] if isinstance(result, tuple) else str(result))
        return "\n".join(out)


class Mp3Converter:
    def to_mp3(self, wav, mp3, title, artist, album):
        from pydub.exceptions import CouldntDecodeError
        from convert_audio import convert_to_mp3
        try:
            convert_to_mp3(str(wav), str(mp3), title=title, artist=artist, album=album)
        except (CouldntDecodeError, OSError) as e:
            raise _failed("the MP3 conversion", e) from e
