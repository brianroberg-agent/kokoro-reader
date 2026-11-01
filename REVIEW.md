# Reviewing kokoro-reader

## What this repo is

Two things that share code:

- **The CLI** (`text_to_speech.py`, `verify_audio.py`, `convert_audio.py`, `publish_audio.py`), driven by Brian or by the `recording-article-audio` skill: Kokoro TTS on Apple Silicon (MLX), Gemini verification, MP3 conversion, publishing to Audiobookshelf.
- **The narration server** (`narration/`, `python -m narration`), an MCP server on the Mac for the unattended pipeline. A headless Claude session that has read an untrusted web article gets six tools on `/agent/mcp`; the runner gets `open`, `finalize` and `status` on `/runner/mcp`. The server, not the session, decides whether a recording may publish. `docs/narration-server.md` is the operator's and the skill's reference; the spec and plans are in `docs/superpowers/`.

## What a change here can break

- **Containment (the server's purpose).** The agent session may have been steered by the article it read. Anything that lets it make the audio say what the article doesn't, without that being declared and published in the episode notes, is the most serious class of bug. That includes loosening a rule in `narration/words.py` (the word check, substitution limits, respelling rules, override provenance) or a refusal message that suggests a way around a rule.
- **finish and the gate disagreeing.** Both run `agent_tools.validate_substitutions` and `gate.speech_matches`. A check added to only one lets finish accept what the gate holds, or the other way round.
- **The CLI's text pipeline.** `clean_markdown_text` and `prepare_for_tts` in `text_to_speech.py` are both the CLI's behaviour and what `narration.words` mirrors. A change there changes what is spoken, and what the word check thinks is spoken.
- **Install and startup on the Mac.** The engines need Metal, the spaCy model (installed outside `uv.lock`; always `uv sync --inexact`), and a source build of `curated-tokenizers` (Cython pinned below 3.1 in `[tool.uv]`).

## Interfaces (changing one needs the callers changed too)

- MCP tool names, arguments and result keys, on both endpoints. Plan 3's runner and the skill text depend on them; the substitution keys are `original` / `spoken` / `why`.
- The work item's files (`state.json`, `result.json`, `metadata.json`, `episode.mp3`) and `GET /runner/files/{item_id}/episode.mp3`.
- The `NARRATION_*` environment variables and `deploy/net.robergb.narration.plist`.
- The CLI's command-line options, which the skill uses.

## What reviewers tend to miss

- **Fakes hide the engines.** Most tests use `narration/fakes.py`. misaki's real phonemes, Kokoro's audio and Gemini's reports differ from the fakes; `tests/narration/test_engines.py` covers some of misaki for real. A change to what the engine is asked to say needs a thought about the real G2P.
- **The MLX tests need Metal.** `TestMLXAudioBackend` aborts the process inside Claude Code's sandbox; run it outside (`.venv/bin/python -m pytest tests/test_text_to_speech.py::TestMLXAudioBackend`). Everything else runs sandboxed.
- **Gemini is not deterministic** (issue #11). One report is a hint; the gate doesn't read its prose.
- **No real names in fixtures.** Test articles and names are made up (practice 5), including when a real run found the bug.
