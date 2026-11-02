import sys
import threading
import time
import types

import numpy as np
import pytest
import soundfile as sf
from google.genai import errors as genai_errors
from pydub import AudioSegment

import text_to_speech
import verify_audio
from narration import words
from narration.engines import GeminiVerifier, KokoroEngine, MisakiG2P, Mp3Converter
from narration.fakes import FakeG2P, FakeLinkG2P, FakeTTS


def test_kokoro_engine_cleans_markdown_like_the_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_generate_audio(text, output_path, voice="af_heart", speed=1.0, **_):
        seen.update(text=text, voice=voice, speed=speed)
        sf.write(output_path, np.zeros(2400, dtype="float32"), 24000)  # 0.1 s of silence

    monkeypatch.setattr(text_to_speech, "generate_audio", fake_generate_audio)
    out = tmp_path / "recording.wav"
    seconds = KokoroEngine().record(
        "# Zoning Alone\n\nSee the [court](https://example.com/zoning) **today**, "
        "said [Pengelley](/pɛnʤˈɛli/).", out, "af_heart", 1.0)
    # The heading marker, the URL and the emphasis are gone; the phonetic override stays.
    assert seen["text"] == "Zoning Alone\n\nSee the court today, said [Pengelley](/pɛnʤˈɛli/)."
    assert (seen["voice"], seen["speed"]) == ("af_heart", 1.0)
    assert seconds == pytest.approx(0.1, abs=0.01)


@pytest.mark.parametrize("space", ["\xa0", "\t", "\u2009", "\u3000", " \t\xa0 "])
def test_kokoro_engine_makes_every_space_but_a_newline_plain(tmp_path, monkeypatch, space):
    # misaki misplaces an override after such a space earlier on its line; the
    # engine gets plain spaces, exactly as words.check reads the text.
    seen = {}

    def fake_generate_audio(text, output_path, voice="af_heart", speed=1.0, **_):
        seen["text"] = text
        sf.write(output_path, np.zeros(2400, dtype="float32"), 24000)

    monkeypatch.setattr(text_to_speech, "generate_audio", fake_generate_audio)
    text = (f"a{space}b\nc{space}d -- e\n\n"
            f"We saw{space}Pengelley and{space}[Kelmorian](/kɛvˈɔɹkiən/) left.")
    KokoroEngine().record(text, tmp_path / "recording.wav", "af_heart", 1.0)
    # Newlines stay, the override is intact, and "--" is left for generate_audio's
    # own prepare_for_tts.
    assert seen["text"] == ("a b\nc d -- e\n\n"
                            "We saw Pengelley and [Kelmorian](/kɛvˈɔɹkiən/) left.")
    assert text_to_speech.prepare_for_tts(seen["text"]) == words._spoken_text(text)


def test_misaki_g2p_answers_for_names_outside_its_lexicon():
    # Runs only where misaki is installed (not in the light test environment).
    pytest.importorskip("misaki")
    g2p = MisakiG2P()
    phonemes = g2p.phonemes("Pengelley")
    assert phonemes and "❓" not in phonemes and any(ch.isalpha() for ch in phonemes)
    # Without preprocessing, link syntax is not read as an override: the agent's own
    # phonemes don't come back as the lookup's answer.
    assert g2p.phonemes("[Pengelley](/kˈɪlðəmˈɔl/)") != "kˈɪlðəmˈɔl"


def _overlap_meter():
    """A function that sleeps briefly and records the most calls running at once."""
    state = {"active": 0, "most": 0}
    guard = threading.Lock()

    def run():
        with guard:
            state["active"] += 1
            state["most"] = max(state["most"], state["active"])
        time.sleep(0.05)
        with guard:
            state["active"] -= 1
    return run, state


def _in_threads(fn, n=3):
    threads = [threading.Thread(target=fn) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)


def test_kokoro_engine_runs_one_recording_at_a_time(tmp_path, monkeypatch):
    run, state = _overlap_meter()

    def fake_generate_audio(text, output_path, **_):
        run()
        sf.write(output_path, np.zeros(2400, dtype="float32"), 24000)

    monkeypatch.setattr(text_to_speech, "generate_audio", fake_generate_audio)
    counter = iter(range(100))
    _in_threads(lambda: KokoroEngine().record("Hello.", tmp_path / f"r{next(counter)}.wav",
                                              "af_heart", 1.0))
    assert state["most"] == 1


def test_kokoro_engine_failures_are_runtime_errors_without_server_paths(tmp_path, monkeypatch):
    def disk_full(text, output_path, **_):
        raise OSError(28, "No space left on device", "/srv/narration/items/x/.tmp-recording.wav")

    monkeypatch.setattr(text_to_speech, "generate_audio", disk_full)
    with pytest.raises(RuntimeError, match="No space left on device") as failed:
        KokoroEngine().record("Hello.", tmp_path / "r.wav", "af_heart", 1.0)
    assert "/srv/narration" not in str(failed.value)

    def garbage(text, output_path, **_):
        open(output_path, "wb").write(b"RIFF not really audio")

    monkeypatch.setattr(text_to_speech, "generate_audio", garbage)
    with pytest.raises(RuntimeError, match="recording failed") as failed:
        KokoroEngine().record("Hello.", tmp_path / "r.wav", "af_heart", 1.0)
    assert str(tmp_path) not in str(failed.value)


def test_gemini_verifier_decodes_the_audio_once(tmp_path, monkeypatch):
    path = tmp_path / "recording.wav"
    sf.write(path, np.zeros(24000, dtype="float32"), 24000)  # 1 s
    decodes, seen = [], {}
    real_from_wav = AudioSegment.from_wav

    def counting_from_wav(file, *args, **kwargs):
        decodes.append(file)
        return real_from_wav(file, *args, **kwargs)

    def fake_verify(audio_path, source_text, model=None, audio=None):
        seen["audio"] = audio
        return "No issues found."

    monkeypatch.setattr(AudioSegment, "from_wav", staticmethod(counting_from_wav))
    monkeypatch.setattr(verify_audio, "verify_audio", fake_verify)
    report, warnings = GeminiVerifier().check(path, "one two")
    assert (report, warnings) == ("No issues found.", [])
    assert len(decodes) == 1 and isinstance(seen["audio"], AudioSegment)


def test_gemini_verifier_reports_api_failures_as_runtime_errors(tmp_path, monkeypatch):
    path = tmp_path / "recording.wav"
    sf.write(path, np.zeros(24000, dtype="float32"), 24000)

    def overloaded(*args, **kwargs):
        raise genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE",
                                                       "message": "The model is overloaded."}})

    monkeypatch.setattr(verify_audio, "verify_audio", overloaded)
    with pytest.raises(RuntimeError, match="overloaded"):
        GeminiVerifier().check(path, "one two")
    with pytest.raises(RuntimeError, match="verification failed"):
        GeminiVerifier().check(tmp_path / "missing.wav", "one two")


def test_mp3_converter_reports_failures_as_runtime_errors(tmp_path, monkeypatch):
    def missing(wav_path, mp3_path, **_):
        raise FileNotFoundError(2, "No such file or directory", wav_path)

    monkeypatch.setitem(sys.modules, "convert_audio", types.SimpleNamespace(convert_to_mp3=missing))
    with pytest.raises(RuntimeError, match="No such file or directory") as failed:
        Mp3Converter().to_mp3(tmp_path / "recording.wav", tmp_path / "episode.mp3", "T", "A", "B")
    assert str(tmp_path) not in str(failed.value)


def test_misaki_g2p_skips_preprocessing_and_runs_one_lookup_at_a_time(monkeypatch):
    run, state = _overlap_meter()
    calls = []

    class FakeMisakiG2P:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __call__(self, text, preprocess=True):
            calls.append((text, preprocess))
            run()
            return "fəˈnɛt", []

    fake_en = types.SimpleNamespace(G2P=FakeMisakiG2P)
    fake_espeak = types.SimpleNamespace(EspeakFallback=lambda british: ("fallback", british))
    monkeypatch.setitem(sys.modules, "misaki",
                        types.SimpleNamespace(en=fake_en, espeak=fake_espeak))
    g2p = MisakiG2P()
    assert g2p._g2p.kwargs == {"trf": False, "british": False,
                               "fallback": ("fallback", False), "unk": ""}
    _in_threads(lambda: g2p.phonemes("Pengelley"))
    assert state["most"] == 1
    assert calls == [("Pengelley", False)] * 3


def test_fake_tts_hold_gives_up_after_its_timeout(tmp_path):
    tts = FakeTTS()
    started, release = threading.Event(), threading.Event()
    tts.hold, tts.hold_timeout = (started, release), 0.05
    with pytest.raises(RuntimeError, match="never released"):
        tts.record("x", tmp_path / "r.wav", "af_heart", 1.0)
    assert started.is_set()


def test_fake_link_g2p_reads_link_syntax_like_misaki_preprocessing():
    assert FakeLinkG2P().phonemes("[Pengelley](/kˈɪlðəmˈɔl/)") == "kˈɪlðəmˈɔl"
    assert FakeLinkG2P().phonemes("Pengelley") == FakeG2P().phonemes("Pengelley")


def test_misaki_g2p_reads_a_whole_text_line_by_line_with_preprocessing(monkeypatch):
    run, state = _overlap_meter()
    calls = []

    class FakeMisakiG2P:
        def __init__(self, **kwargs):
            pass

        def __call__(self, text, preprocess=True):
            calls.append((text, preprocess))
            run()
            return f"<{text}>", []

    monkeypatch.setitem(sys.modules, "misaki", types.SimpleNamespace(
        en=types.SimpleNamespace(G2P=FakeMisakiG2P),
        espeak=types.SimpleNamespace(EspeakFallback=lambda british: None)))
    g2p = MisakiG2P()
    # As the engine's pipeline splits it: the text stripped, split on runs of newlines,
    # blank lines skipped; misaki's preprocessing on, so overrides are read as the engine reads them.
    assert g2p.phonemes_for_text("\n one [x](/ɛks/)\n \n\ntwo, three\n") == (
        "<one [x](/ɛks/)>\n<two, three>")
    assert calls == [("one [x](/ɛks/)", True), ("two, three", True)]
    _in_threads(lambda: g2p.phonemes_for_text("a\nb"))
    assert state["most"] == 1  # one misaki call at a time, lookups included


def test_misaki_g2p_reports_a_failed_comparison_as_a_runtime_error(monkeypatch):
    class FailingMisakiG2P:
        def __init__(self, **kwargs):
            pass

        def __call__(self, text, preprocess=True):
            raise FileNotFoundError(2, "No such file or directory", "/opt/espeak/data")

    monkeypatch.setitem(sys.modules, "misaki", types.SimpleNamespace(
        en=types.SimpleNamespace(G2P=FailingMisakiG2P),
        espeak=types.SimpleNamespace(EspeakFallback=lambda british: None)))
    with pytest.raises(RuntimeError, match="the speech comparison failed") as failed:
        MisakiG2P().phonemes_for_text("Costs fell.")
    assert "/opt/espeak" not in str(failed.value)


def test_fake_g2p_reads_words_and_overrides_line_by_line():
    text = "The Court, [Pengelley](/pɛnʤˈɛli/) -- it's 5%!\n\n[x](//ɛks//) Next  line.\n"
    assert FakeG2P().phonemes_for_text(text) == "the court pɛnʤˈɛli it's 5\nɛks next line"
