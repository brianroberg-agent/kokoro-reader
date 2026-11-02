# Kokoro TTS Toolkit

Produce high-quality audio recordings of articles and long-form text on Apple Silicon. The toolkit provides command-line utilities for text-to-speech generation, audio verification, and MP3 conversion — each usable on its own. It also includes a [Claude Code](https://claude.ai/code) skill that allows Claude to orchestrate all of them together, managing the full pipeline from web article to verified, tagged MP3.

The TTS engine is [Kokoro-82M](https://huggingface.co/mlx-community/Kokoro-82M-bf16) running on [MLX](https://github.com/ml-explore/mlx). Audio verification uses Google's Gemini to listen to recordings and flag errors. MP3 encoding uses [ffmpeg](https://ffmpeg.org/).

**Requires Apple Silicon (M1/M2/M3/M4 Mac).**

## Quick Start

### With Claude Code (recommended)

The included skill (`.claude/skills/recording-article-audio/SKILL.md`) manages the full workflow — fetching articles via the [article-assistant](../article-assistant) project, preparing text, generating audio, verifying quality, and iteratively fixing issues:

```
> Record an audio version of this article: https://example.com/article
```

You can also invoke it explicitly with `/recording-article-audio`.

### From the Command Line

Generate audio from a text file:

```bash
uv run python text_to_speech.py article.md --voice af_heart --output recording.wav
```

Verify the recording against the source text:

```bash
uv run python verify_audio.py recording.wav article.md
```

Convert to MP3 with metadata:

```bash
uv run python convert_audio.py recording.wav "Article Title.mp3" \
    --title "Article Title" --artist "Author Name" \
    --album "The New Atlantis, No. 83 (Winter 2026)"
```

Publish to Audiobookshelf:

```bash
uv run python publish_audio.py podcast "Article Title.mp3" \
    --podcast "The New Atlantis"
```

## Installation

1. **Install [uv](https://docs.astral.sh/uv/getting-started/installation/)**:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Install dependencies**:
   ```bash
   uv sync
   uv pip install en-core-web-sm@https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
   ```
   The second line installs a spacy language model needed by the phonemizer. It's distributed from GitHub rather than PyPI, so it can't be included in `pyproject.toml`.

3. **Install [article-assistant](https://github.com/brianroberg/article-assistant)** in a sibling directory (for fetching articles from URLs):
   ```bash
   cd .. && git clone https://github.com/brianroberg/article-assistant.git && cd article-assistant && uv sync
   ```

4. **Set up Gemini API key** (for audio verification):
   ```bash
   echo "GEMINI_API_KEY=your_key_here" > .env
   ```

## Preparing Text for TTS

The toolkit reads plain text or Markdown. For best results with articles:

**Section breaks** — Use `[BREAK]` on its own line between sections. This inserts a 2-second silent pause.

**Section headings** — Include them as plain text. The markdown cleaner strips `#` but preserves the words.

**Em dashes** — Write as `--`. They're automatically converted to pauses.

**Roman numerals** — Spell out ("World War One" not "World War I").

**Pronunciation overrides** — Use `[word](/phonemes/)` for difficult proper nouns:
```
[Kevorkian](/kɛvˈɔːɹkiən/) was born in Michigan.
```

## Command Line Reference

<details>
<summary><strong>text_to_speech.py</strong> — Generate audio from text</summary>

```bash
uv run python text_to_speech.py document.txt
uv run python text_to_speech.py article.md --voice af_bella --output recording.wav
echo "Hello world" | uv run python text_to_speech.py
```

| Option | Short | Description | Default |
|--------|-------|-------------|---------|
| `input_file` | | Input text file, or `-` for stdin | (required unless using stdin) |
| `--output` | `-o` | Output audio file path | `input_filename.wav` or `output.wav` |
| `--voice` | `-v` | Voice to use for TTS | `af_heart` |
| `--speed` | `-s` | Speech speed multiplier | `1.0` |
| `--lang` | `-l` | Language code | `a` (American English) |
| `--markdown` | `-m` | Treat input as markdown | auto-detect for `.md` files |
| `--keep-temp` | | Keep temporary chunk files | `false` |
| `--list-voices` | | Show all available voices | |

Markdown files are automatically cleaned: headers, images, links, emphasis, code blocks, lists, and blockquotes are stripped. Phonetic pronunciation links (`[word](/phonemes/)`) are preserved.

</details>

<details>
<summary><strong>verify_audio.py</strong> — Verify audio quality with Gemini</summary>

```bash
uv run python verify_audio.py recording.wav source_text.md
```

Sends audio + source text to Gemini, which listens and reports mispronunciations, missing or extra content, and pacing issues with timestamps. Costs ~3 cents per 16 minutes of audio.

The report begins with a free, deterministic pre-check (duration, word count, reading pace, unexpected silence gaps) that runs before any model call. When the source text contains `[BREAK]` markers and the audio's silence gaps line up with them, each section is verified separately against its own slice of the text and the results are aggregated under headers like `## Section 2 of 5 (12:34–25:10)` — timestamps inside a section are relative to that section's start. Verifying long recordings in one call produces unreliable phantom reports, so recordings longer than ~15 minutes that can't be chunked this way are rejected with an error asking for `[BREAK]` markers.

| Option | Description | Default |
|--------|-------------|---------|
| `audio_file` | Path to the audio file (.wav) | (required) |
| `text_file` | Path to the source text file | (required) |
| `--model` | Gemini model to use | `gemini-flash-latest` |

Requires a `GEMINI_API_KEY` environment variable or `.env` file.

</details>

<details>
<summary><strong>convert_audio.py</strong> — Convert WAV to tagged MP3</summary>

```bash
uv run python convert_audio.py recording.wav output.mp3 \
    --title "Article Title" --artist "Author Name" \
    --album "The New Atlantis, No. 83 (Winter 2026)"
```

| Option | Description | Default |
|--------|-------------|---------|
| `wav_file` | Path to the input WAV file | (required) |
| `mp3_file` | Path for the output MP3 file | (required) |
| `--title` | Article title (ID3 TIT2 tag) | |
| `--artist` | Author name (ID3 TPE1 tag) | |
| `--album` | Publication name and issue (ID3 TALB tag) | |

</details>

<details>
<summary><strong>publish_audio.py</strong> — Publish audio to Audiobookshelf</summary>

Upload a podcast episode:

```bash
uv run python publish_audio.py podcast "Article Title.mp3" \
    --podcast "The New Atlantis"
```

Upload an audiobook:

```bash
uv run python publish_audio.py book "Chapter 1.mp3" \
    --title "Book Title" --author "Author Name"
```

**Podcast subcommand:**

| Option | Description | Default |
|--------|-------------|---------|
| `audio_file` | Path to the audio file | (required) |
| `--podcast` | Podcast name in Audiobookshelf | (required) |
| `--library` | Library name | first podcast library |
| `--url` | Server URL | `AUDIOBOOKSHELF_URL` env var |
| `--api-key` | API key | `AUDIOBOOKSHELF_API_KEY` env var |

**Book subcommand:**

| Option | Description | Default |
|--------|-------------|---------|
| `audio_file` | Path to the audio file | (required) |
| `--title` | Book title | (required) |
| `--author` | Author name | (required) |
| `--library` | Library name | first book library |
| `--url` | Server URL | `AUDIOBOOKSHELF_URL` env var |
| `--api-key` | API key | `AUDIOBOOKSHELF_API_KEY` env var |

Requires `AUDIOBOOKSHELF_URL` and `AUDIOBOOKSHELF_API_KEY` environment variables or `.env` file.

</details>

<details>
<summary><strong>Available voices and languages</strong></summary>

**American English (lang: `a`)**: `af_heart`, `af_bella`, `af_sarah`, `am_adam`, `am_michael`

**British English (lang: `b`)**: `bf_emma`, `bf_isabella`, `bm_george`, `bm_daniel`

**Other Languages**: Spanish (`e`), French (`f`), Hindi (`h`), Italian (`i`), Japanese (`j`), Portuguese (`p`), Chinese (`z`)

| Code | Language |
|------|----------|
| `a` | American English |
| `b` | British English |
| `e` | Spanish |
| `f` | French |
| `h` | Hindi |
| `i` | Italian |
| `j` | Japanese |
| `p` | Brazilian Portuguese |
| `z` | Mandarin Chinese |

</details>

## System Requirements

- **Hardware**: Apple Silicon Mac (M1, M2, M3, or M4)
- **Python**: 3.10+
- **OS**: macOS (MLX is Apple Silicon only)
- **ffmpeg**: Required for MP3 conversion

## License

MIT License. The Kokoro TTS model has its own Apache 2.0 license.

## Acknowledgments

- [MLX Audio](https://github.com/Blaizzy/mlx-audio) — MLX-native audio processing
- [Kokoro TTS](https://github.com/hexgrad/Kokoro) — Open-source TTS model
- [Apple MLX](https://github.com/ml-explore/mlx) — ML framework for Apple Silicon
