"""python -m narration [--fake-engines]: serve the narration app with uvicorn."""
from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

import uvicorn

from narration.agent_tools import AgentTools
from narration.app import build_app
from narration.auth import _check_runner_token
from narration.runner_tools import RunnerTools
from narration.workitems import WorkItemStore

EPILOG = """\
environment:
  NARRATION_RUNNER_TOKEN_FILE  file holding the runner's bearer token (required): one
                               line, ASCII, no spaces or control characters
  NARRATION_DATA_DIR           where work items are kept (default ~/narration-data)
  NARRATION_HOST               address to listen on (default 127.0.0.1)
  NARRATION_PORT               port to listen on (default 8770)
  NARRATION_ALLOWED_HOSTS      comma-separated host:port values the Host header may
                               have (default 127.0.0.1:PORT,localhost:PORT)
  GEMINI_API_KEY               the verifier's key, from the environment or .env;
                               not needed with --fake-engines
"""


def _runner_token() -> str:
    """The runner token from NARRATION_RUNNER_TOKEN_FILE, its trailing newline (and
    any other trailing whitespace) dropped; a one-line exit if it can't be used.
    The check is BearerAuth's own, run before any engine loads."""
    name = os.environ.get("NARRATION_RUNNER_TOKEN_FILE")
    if not name:
        raise SystemExit("NARRATION_RUNNER_TOKEN_FILE is not set: point it at a file holding "
                         "the runner's bearer token")
    path = Path(name)
    try:
        token = path.read_text(encoding="utf-8").rstrip()
    except OSError as e:
        raise SystemExit(f"cannot read the runner token file {path}: {e.strerror or e}") from None
    except UnicodeDecodeError:
        raise SystemExit(f"the runner token file {path} is not UTF-8 text") from None
    try:
        _check_runner_token(token)
    except ValueError as e:
        raise SystemExit(f"the runner token file {path} is not usable: {e}") from None
    return token


# What the real engines need from MLX: an import and one computation on the GPU.
GPU_PROBE = "import mlx.core as mx; mx.array([1]).item()"
GPU_PROBE_SECONDS = 120  # a cold first import of the library can take a while


def _check_gpu() -> None:
    """Exit in one readable line unless MLX can use the GPU here. Without a Metal
    device (a LaunchAgent session without GPU access, a sandbox) importing MLX aborts
    the process (libc++abi, exit status 134) rather than raising, which launchd would
    restart forever; so it is tried in a child process, before any engine loads."""
    problem = ("MLX cannot use the GPU in this session (is this a LaunchAgent without "
               "Metal access?): ")
    try:
        probe = subprocess.run([sys.executable, "-c", GPU_PROBE], capture_output=True,
                               text=True, timeout=GPU_PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"{problem}the probe did not answer within {GPU_PROBE_SECONDS} "
                         "seconds") from None
    except OSError as e:
        raise SystemExit(f"{problem}the probe could not start: {e.strerror or e}") from None
    if probe.returncode != 0:
        said = [line.strip() for line in (probe.stderr or "").splitlines() if line.strip()]
        detail = f": {said[-1][:300]}" if said else ""
        raise SystemExit(f"{problem}the probe ({GPU_PROBE}) exited with status "
                         f"{probe.returncode}{detail}")


SPACY_MODEL = "en_core_web_sm"  # what misaki's English G2P loads


def _check_spacy_model() -> None:
    """Exit in one readable line unless the spaCy model the pronunciation lookup needs
    imports. It comes from GitHub, not PyPI, so it is not in uv.lock, and a plain
    `uv sync` removes it; misaki would then download it at every start."""
    try:
        importlib.import_module(SPACY_MODEL)
    except Exception as e:  # missing, or a broken install
        said = " ".join(str(e).split())  # one line
        raise SystemExit(f"the spaCy model {SPACY_MODEL} does not import ({type(e).__name__}: "
                         f"{said}); the pronunciation lookup needs it. Install it as the README "
                         "says, and update with `uv sync --inexact`, which keeps it (a plain "
                         "`uv sync` removes it)") from None


def _port() -> int:
    value = os.environ.get("NARRATION_PORT", "8770")
    try:
        port = int(value)
    except ValueError:
        port = -1
    if not 0 < port < 65536:
        raise SystemExit(f"NARRATION_PORT must be a port number; got {value!r}")
    return port


def _allowed_hosts(port: int) -> list[str]:
    value = os.environ.get("NARRATION_ALLOWED_HOSTS", f"127.0.0.1:{port},localhost:{port}")
    hosts = [h.strip() for h in value.split(",") if h.strip()]
    if not hosts:
        raise SystemExit("NARRATION_ALLOWED_HOSTS is empty: list the host:port values clients "
                         "use, comma-separated, or unset it for the local defaults")
    return hosts


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m narration", epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="The narration MCP server: an agent server at "
                                            "/agent/mcp and a runner server at /runner/mcp.")
    p.add_argument("--fake-engines", action="store_true",
                   help="no TTS, Gemini or G2P: fake engines, for smoke tests")
    args = p.parse_args(argv)
    runner_token = _runner_token()
    data = Path(os.environ.get("NARRATION_DATA_DIR", str(Path.home() / "narration-data")))
    host = os.environ.get("NARRATION_HOST", "127.0.0.1")
    port = _port()
    allowed = _allowed_hosts(port)
    if args.fake_engines:
        from narration.fakes import FakeConverter, FakeG2P, FakeTTS, FakeVerifier
        tts, ver, g2p, conv = FakeTTS(), FakeVerifier(), FakeG2P(), FakeConverter()
    else:
        import dotenv
        dotenv.load_dotenv()  # the same .env verify_audio.py reads
        if not os.environ.get("GEMINI_API_KEY"):
            raise SystemExit("GEMINI_API_KEY is not set (environment or .env); the Gemini "
                             "verifier needs it. Pass --fake-engines to run without it.")
        _check_gpu()
        _check_spacy_model()
        from narration.engines import GeminiVerifier, KokoroEngine, MisakiG2P, Mp3Converter
        tts, ver, g2p, conv = KokoroEngine(), GeminiVerifier(), MisakiG2P(), Mp3Converter()
    store = WorkItemStore(data)
    app = build_app(store, AgentTools(store, tts, ver, g2p), RunnerTools(store, conv, g2p),
                    runner_token, allowed)
    # No root_path, ever: BearerAuth matches absolute request paths (/health, /agent/...,
    # /runner/...), and a root path would move them out from under its gate.
    # lifespan "on": the lifespan starts both MCP session managers; if it fails, the
    # server must not start (under "auto" uvicorn would serve without them).
    uvicorn.run(app, host=host, port=port, lifespan="on")


if __name__ == "__main__":
    main()
