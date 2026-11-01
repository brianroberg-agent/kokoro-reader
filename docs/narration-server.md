# The narration server

`python -m narration` serves two MCP endpoints behind one bearer-token gate:

- `/agent/mcp` has the six tools an agent session records an article with: `get_text`, `replace_text`, `record`, `check`, `phonemes` and `finish`. Each session has its own token, made when the work item is opened and revoked at `finish`.
- `/runner/mcp` has `open`, `finalize` and `status`, for the runner only (the runner token). `GET /runner/files/{item_id}/episode.mp3` serves a finished MP3, also for the runner token.
- `GET /health` is open and returns `{"ok":true}`.

On the Mac it runs as one LaunchAgent, `net.robergb.narration`, from the template in `deploy/net.robergb.narration.plist`.

## Install on the Mac

The plist assumes the checkout is at `~/tools/kokoro`. If it is somewhere else, change `WorkingDirectory` in the plist first.

```bash
cd ~/tools/kokoro && git pull && uv sync --inexact
uv pip install en-core-web-sm@https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
mkdir -p ~/.config/narration ~/Library/Logs/narration ~/narration-data
openssl rand -hex 32 > ~/.config/narration/runner-token; chmod 600 ~/.config/narration/runner-token
tailscale ip -4          # put this address in place of TAILNET_IP in the plist
cp deploy/net.robergb.narration.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/(id -u) ~/Library/LaunchAgents/net.robergb.narration.plist
curl -s http://(tailscale ip -4):8770/health      # {"ok":true}
```

The plist sets `HF_HUB_OFFLINE=1`, so the TTS model loads from the local Hugging Face cache, with no request to the hub at every record. Before the first start, record one sentence by hand, once for each voice the runner will use, so the model and the voice are in the cache: `echo "Hello there." | uv run python text_to_speech.py -o /tmp/hello.wav --voice af_heart`.

Always sync with `uv sync --inexact`: the spaCy model `en_core_web_sm` comes from GitHub, not PyPI, so it is not in `uv.lock`, and a plain `uv sync` removes it (the second line installs it once, as the README does). With real engines the server exits at startup if the model is missing.

(`(id -u)` is fish syntax. In bash, use `$(id -u)`, and `$(tailscale ip -4)` for the address.)

`TAILNET_IP` appears twice in the plist, in `NARRATION_HOST` and in `NARRATION_ALLOWED_HOSTS`. Replace both in the copy in `~/Library/LaunchAgents/`, after the `cp` and before the `launchctl bootstrap`, so the checkout stays clean.

The server exits at startup if `GEMINI_API_KEY` is unset. Keep it in the checkout's `.env`, which `load_dotenv` finds from the package's location, or add it to the plist's `EnvironmentVariables`.

The logs are `~/Library/Logs/narration/out.log` and `err.log`. The runner token is the file `~/.config/narration/runner-token`. It goes to the deploy of the runner (Plan 3) and never travels through chat.

To restart after a `git pull` (and a `uv sync --inexact`): `launchctl kickstart -k gui/(id -u)/net.robergb.narration` (bash: `$(id -u)`). To remove it: `launchctl bootout gui/(id -u)/net.robergb.narration`.

## Check it from the home server's container

```bash
curl -s http://brians-macbook-pro:8770/health      # {"ok":true}
```

This reaches the Mac over the tailnet. The name must be in `NARRATION_ALLOWED_HOSTS` (with the port), or the server answers 421.

## Settings

The server reads these from its environment (the plist's `EnvironmentVariables`).

| Variable | Default | Meaning |
|---|---|---|
| `NARRATION_RUNNER_TOKEN_FILE` | none: required | A file holding the runner's bearer token: one line, ASCII, no spaces or control characters. The server exits at startup if it can't use it. |
| `NARRATION_DATA_DIR` | `~/narration-data` | Where work items are kept, one folder per article. |
| `NARRATION_HOST` | `127.0.0.1` | The address to listen on. The plist sets the Mac's tailnet address. |
| `NARRATION_PORT` | `8770` | The port to listen on. |
| `NARRATION_ALLOWED_HOSTS` | `127.0.0.1:PORT,localhost:PORT` | Comma-separated `host:port` values a request's `Host` header may have (the DNS-rebinding guard). A request with any other Host gets 421. |
| `GEMINI_API_KEY` | none | The verifier's key, from the environment or `.env`. Not needed with `--fake-engines`. |
| `HF_HUB_OFFLINE` | unset (the plist sets `1`) | Hugging Face's own setting: with `1`, the TTS model and voices load from the local cache only. |

## Never run it behind a root path

Never start the server with `--root-path` (or any root path setting). The bearer-token gate matches absolute request paths (`/health`, `/agent/...`, `/runner/...`), so a root path would move the requests out from under the gate. `python -m narration` takes no such option, and the plist must not add one. If a reverse proxy is ever put in front, it has to strip any prefix before the request reaches the server.

## What an agent session must do (a summary for the skill)

The server's instructions and tool descriptions say all of this to the agent. A skill that drives a session (Plan 3) should say it too:

- **One tool call at a time.** A call made while another is still running for the same work item is refused.
- **Re-record after any edit.** The recorded text is hashed. After any `replace_text`, `check` and `finish` (outcome `clean` or `minor`) refuse until `record` has run again. There are at most 4 record rounds and 8 checks per article, and a round counts even if the engine fails. Once the checks are used up, `record` is refused (a new round could not be checked), so the last checked round stands.
- **Never change what the article says without declaring it.** Every word change in the prepared text is a substitution in `finish` (`original`, `spoken`, optional `why`), declared once. Each must replace some of the article's words: one that only adds words (`long` read as `long subscribe now`) is refused, and so is one whose spoken side adds a web or e-mail address, a path or a dotted word the article doesn't have. All of them together may add only a few words.
- **A respelled pronunciation lookup is a substitution.** If the agent looks up a respelling of a word with `phonemes` (Pengelly for Pengelley) and uses those phonemes in the word's override, it declares that in `finish` as a substitution: `original` the word, `spoken` the respelling. The respelling must stay close to the word: one word, starting with the same letter, neither inside the other, and alike. A name (a word starting with a capital letter) may be respelled more loosely, from four letters up and with a possessive on both sides (Zounk's as Zunk's). A respelling further off than that can't be used in an override: write it in the text with `replace_text` and declare it as a substitution instead.
- **Overrides only from `phonemes()`.** A pronunciation override is `[word](/phonemes/)` with exactly the phonemes `phonemes(word)` returned. Phonemes written by hand are refused at `finish`.
- **`record` returns `speech_warnings`:** places where the engine would say something the article doesn't. Read them before spending a check, fix them with `replace_text` (most often by moving or removing a pause), and record again. `finish` (outcome `clean` or `minor`) and the runner's gate refuse the same mismatches. When the prepared text's words differ from the article's, nothing is compared at `record` (substitutions are declared only at `finish`), and `speech_warnings` holds one entry saying so.
- **A pause after some words changes how the engine reads them.** The engine reads "to", "in", "the" (before a vowel) and "am" differently when a pause follows them. So a pacing edit (a comma, a dash or a pause mark) right after one of these words can change the sound, and may be flagged in `speech_warnings` and then refused at `finish`. A skill should tell the agent to put pauses elsewhere, or to take the pause out again.
- **`finish` ends the session** and revokes its token. `clean` needs the latest round recorded from the prepared text as it is now and checked with no pre-check warnings. `minor` and `hold` list at least one remaining issue. `hold` needs no claims about the recording, and nothing held is published.

## Tests

The tests run without the Apple Silicon stack (no `mlx_audio`, `torch` or `spacy`). This is the light test command:

```bash
uv run --no-project --with "mcp==2.3.0" --with uvicorn --with httpx --with hypothesis --with pytest \
  --with numpy --with soundfile --with pydub --with "audioop-lts; python_version >= '3.13'" \
  --with google-genai --with python-dotenv \
  python -m pytest tests/narration tests/test_verify_audio.py -q
```

`tests/narration/smoke_claude.sh` is a manual end-to-end check, not part of pytest: it starts the server on fake engines (port 8771, or `SMOKE_PORT`), opens a work item, and has a real `claude -p` session with only the six tools record it. The session is also asked to run a shell command, read two files and fetch two URLs (the spec's containment probes, plus canaries the script recognises), and the script fails if any got through, if `result.json` does not say outcome `clean`, or if the session's token still works after `finish`. It needs `claude` logged in and costs a few cents. Run it with `bash tests/narration/smoke_claude.sh`.
