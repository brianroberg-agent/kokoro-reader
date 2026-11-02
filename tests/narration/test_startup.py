"""Starting the server: importing it without the GPU stack, and the checks
python -m narration makes before it loads the real engines."""
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _python(code, tmp_path, stub=None):
    """Run `code` in a fresh interpreter from the repo root. stub: the body of an
    mlx_audio package put first on the path, standing in for the real one."""
    path = [str(ROOT)]
    if stub is not None:
        package = tmp_path / "stubs" / "mlx_audio"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(stub)
        path.insert(0, str(package.parent))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(path)}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


HELP = ("import runpy, sys; sys.argv = ['narration', '--help']; "
        "runpy.run_module('narration', run_name='__main__')")
ABSENT = "import sys; sys.modules['mlx_audio'] = None; "  # import mlx_audio raises ImportError
# Without a Metal device, importing the real library aborts the process (libc++abi,
# exit 134); a stub can't abort without killing the test, so it raises what an
# ImportError guard would not catch.
NO_METAL = "raise RuntimeError('[metal::load_device] No Metal device available.')\n"


@pytest.mark.parametrize("prefix, stub", [(ABSENT, None), ("", NO_METAL)],
                         ids=["library absent", "library import fails"])
def test_the_server_imports_without_the_audio_library(tmp_path, prefix, stub):
    words = _python(prefix + "import narration.words, narration.app, narration.engines",
                    tmp_path, stub)
    assert words.returncode == 0, words.stderr
    helped = _python(prefix + HELP, tmp_path / "help", stub)
    assert helped.returncode == 0, helped.stderr
    assert "--fake-engines" in helped.stdout


def test_the_audio_library_is_imported_only_to_make_audio(tmp_path):
    out = _python("import sys, narration.words, verify_audio, text_to_speech; "
                  "print(sorted(m for m in sys.modules if m.startswith('mlx')))",
                  tmp_path, NO_METAL)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_making_audio_without_the_library_is_a_readable_error(monkeypatch, tmp_path):
    import text_to_speech
    monkeypatch.setitem(sys.modules, "mlx_audio", None)
    monkeypatch.setitem(sys.modules, "mlx_audio.tts", None)
    monkeypatch.setitem(sys.modules, "mlx_audio.tts.utils", None)
    message = "mlx_audio is unavailable (audio generation requires Apple Silicon)."
    with pytest.raises(RuntimeError) as e:
        text_to_speech.generate_audio("Hello there.", str(tmp_path / "out.wav"))
    assert str(e.value) == message  # what the CLI printed before the import was lazy


# -- python -m narration, real engines: the GPU probe --

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A configuration that gets past the cheap checks; nothing served, no real engine."""
    for name in ("NARRATION_HOST", "NARRATION_PORT", "NARRATION_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    token_file = tmp_path / "runner-token"
    token_file.write_text("runner-secret\n")
    monkeypatch.setenv("NARRATION_RUNNER_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("NARRATION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setitem(sys.modules, "en_core_web_sm", types.ModuleType("en_core_web_sm"))
    served, built = [], []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.append(kw))

    def fake(name):
        return lambda *a, **k: built.append(name) or object()
    for name in ("KokoroEngine", "GeminiVerifier", "MisakiG2P", "Mp3Converter"):
        monkeypatch.setattr(f"narration.engines.{name}", fake(name))
    return served, built


def _probe(monkeypatch, outcome):
    """subprocess.run replaced: records each call, and returns or raises `outcome`."""
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(args, *outcome)
    monkeypatch.setattr("subprocess.run", run)
    return calls


def test_real_engines_probe_the_gpu_in_a_child_process_first(env, monkeypatch):
    from narration.__main__ import main
    served, built = env
    calls = _probe(monkeypatch, (0, "", ""))
    main([])
    (args, kwargs), = calls
    assert args == [sys.executable, "-c", "import mlx.core as mx; mx.array([1]).item()"]
    assert kwargs.get("timeout") and kwargs.get("capture_output")
    assert served and "MisakiG2P" in built


@pytest.mark.parametrize("outcome, detail", [
    ((134, "", "libc++abi: terminating due to uncaught exception of type std::runtime_error: "
               "[metal::load_device] No Metal device available.\n"), "No Metal device available"),
    ((1, "", "Traceback (most recent call last):\n  ...\nModuleNotFoundError: No module named "
             "'mlx'\n"), "No module named 'mlx'"),
    ((-6, "", ""), "-6"),
    (subprocess.TimeoutExpired(["python"], 60), "did not answer"),
], ids=["no Metal", "not installed", "killed", "hangs"])
def test_a_gpu_the_server_cannot_use_is_one_readable_line(env, monkeypatch, outcome, detail):
    from narration.__main__ import main
    served, built = env
    _probe(monkeypatch, outcome)
    with pytest.raises(SystemExit) as e:
        main([])
    message = e.value.code
    assert isinstance(message, str) and "\n" not in message  # SystemExit(str): exit status 1
    assert "MLX cannot use the GPU in this session" in message and "LaunchAgent" in message
    assert detail in message
    assert not served and not built  # no engine loads


def test_fake_engines_never_probe_the_gpu(env, monkeypatch):
    from narration.__main__ import main
    served, _ = env
    calls = _probe(monkeypatch, (134, "", "no Metal"))
    main(["--fake-engines"])
    assert calls == [] and served


# -- python -m narration, real engines: the spaCy model --

def test_real_engines_need_the_spacy_model_installed(env, monkeypatch):
    # en_core_web_sm comes from GitHub, not PyPI, so it is not in uv.lock and a plain
    # `uv sync` removes it; misaki would then download it at every start.
    from narration.__main__ import main
    served, built = env
    _probe(monkeypatch, (0, "", ""))
    monkeypatch.setitem(sys.modules, "en_core_web_sm", None)  # import en_core_web_sm fails
    with pytest.raises(SystemExit) as e:
        main([])
    message = e.value.code
    assert isinstance(message, str) and "\n" not in message
    assert "en_core_web_sm" in message and "uv sync --inexact" in message
    assert not served and not built


def test_the_gpu_probe_comes_before_the_spacy_model_check(env, monkeypatch):
    from narration.__main__ import main
    _probe(monkeypatch, (134, "", "No Metal device available"))
    monkeypatch.setitem(sys.modules, "en_core_web_sm", None)
    with pytest.raises(SystemExit, match="MLX cannot use the GPU"):
        main([])


def test_fake_engines_need_no_spacy_model(env, monkeypatch):
    from narration.__main__ import main
    served, _ = env
    monkeypatch.setitem(sys.modules, "en_core_web_sm", None)
    main(["--fake-engines"])
    assert served
