#!/usr/bin/env python3
"""
Text-to-Speech script using MLX Audio library.
Converts text files (including Markdown) to audio using chunking for long texts.
Optimized for Apple Silicon (M1/M2/M3/M4 Macs).
"""

import argparse
import sys
import os
import re
import tempfile
from pathlib import Path
from typing import List
import numpy as np
import soundfile as sf
from pydub import AudioSegment


def load_model(model_path: str):
    """Load a TTS model with mlx_audio.

    mlx_audio is imported here, when audio is made, never at module level: it pulls
    in libmlx, which imports only on Apple Silicon, and which without a Metal device
    (a headless or sandboxed session, a LaunchAgent without GPU access) aborts the
    whole process instead of raising. So the dependency-free helpers here (e.g.
    split_sections, clean_markdown_text) stay importable by verify_audio.py and the
    narration server anywhere. Tests patch text_to_speech.load_model.
    """
    try:
        from mlx_audio.tts.utils import load_model as load_mlx_model
    except ImportError:  # pragma: no cover - exercised only off Apple Silicon
        raise RuntimeError(
            "mlx_audio is unavailable (audio generation requires Apple Silicon)."
        ) from None
    return load_mlx_model(model_path)


def read_text_file(file_path: str) -> str:
    """Read text from file, handling various encodings."""
    encodings = ['utf-8', 'utf-16', 'latin-1', 'ascii']
    
    for encoding in encodings:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    
    raise ValueError(f"Could not decode file {file_path} with any supported encoding")


def _line_start_marker(marker: str) -> "re.Pattern[str]":
    r"""A pattern that does what re.sub(r'^\s*' + marker, '', text, flags=re.M)
    does when its matches are replaced with group 1, in linear time.

    The ^\s* form retries at every line start inside a run of blank lines and
    rescans the run each time, which is quadratic. Its match always starts at
    the first line start of a whitespace run, so this pattern starts at the run
    itself and keeps what comes before that line start (group 1); it also
    starts at a line start right after a previous match, which consumed the run.
    """
    return re.compile(r'(?:(?<!\s)(\A|[^\S\n]*\n)\s*|(?<=\n))' + marker)


_LIST_ITEM = _line_start_marker(r'[-*+]\s+')
_NUMBERED_ITEM = _line_start_marker(r'\d+\.\s+')
_BLOCKQUOTE = _line_start_marker(r'>\s+')


def _remove_images(text: str) -> str:
    r"""re.sub(r'!\[([^\]]*)\]\([^\)]+\)', '', text), in linear time.

    The regex retries at every "![" before the same "]" and rescans to it (and
    to the next ")") each time. Every such attempt succeeds or fails alike, so
    only the first one is made; the next ")" is found once for all of them.
    """
    out, pos, search, close = [], 0, 0, -1
    while True:
        start = text.find('![', search)
        if start < 0:
            break
        bracket = text.find(']', start + 2)
        if bracket < 0:
            break
        if text.startswith('(', bracket + 1):
            if close < bracket + 2:
                close = text.find(')', bracket + 2)
                if close < 0:
                    break
            if close > bracket + 2:
                out.append(text[pos:start])
                pos = search = close + 1
                continue
        search = bracket + 1
    out.append(text[pos:])
    return ''.join(out)


def _unwrap_links(text: str) -> str:
    r"""re.sub(r'\[([^\]]+)\]\((?!/[^)]*?/\))[^\)]+\)', r'\1', text), in linear
    time, the same way as _remove_images. A link whose target is /.../ is a
    Kokoro phonetic override and stays.
    """
    out, pos, search, close = [], 0, 0, -1
    while True:
        start = text.find('[', search)
        if start < 0:
            break
        bracket = text.find(']', start + 1)
        if bracket < 0:
            break
        if bracket > start + 1 and text.startswith('(', bracket + 1):
            if close < bracket + 2:
                close = text.find(')', bracket + 2)
                if close < 0:
                    break
            phonetic = (text.startswith('/', bracket + 2) and close - 1 >= bracket + 3
                        and text[close - 1] == '/')
            if close > bracket + 2 and not phonetic:
                out += [text[pos:start], text[start + 1:bracket]]
                pos = search = close + 1
                continue
        search = bracket + 1
    out.append(text[pos:])
    return ''.join(out)


def clean_markdown_text(text: str) -> str:
    """Clean markdown formatting from text for better TTS."""
    # Remove markdown headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    
    # Remove markdown images completely (including alt text)
    text = _remove_images(text)
    
    # Remove markdown links but keep the text (preserve Kokoro phonetic links like [word](/phonemes/))
    text = _unwrap_links(text)
    
    # Remove markdown emphasis (bold/italic)
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', text)
    
    # Remove code blocks and inline code
    text = re.sub(r'```[^`]*```', '', text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    
    # Remove markdown lists
    text = _LIST_ITEM.sub(r'\1', text)
    text = _NUMBERED_ITEM.sub(r'\1', text)
    
    # Remove blockquotes
    text = _BLOCKQUOTE.sub(r'\1', text)
    
    # Clean up excessive whitespace
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r' +', ' ', text)
    
    return text.strip()


def prepare_for_tts(text: str) -> str:
    """Prepare text for TTS by improving pause markers.

    Replaces -- em dashes with ... ellipsis for longer pauses in the TTS model.
    """
    # (?<!\s) starts the whitespace before "--" only at the start of its run:
    # \s*--\s* would rescan a long run of whitespace from each position in it.
    text = re.sub(r'(?:(?<!\s)\s+)?--\s*', '... ', text)
    return text


def split_sections(text: str) -> List[str]:
    """Split text at [BREAK] markers into separate sections.

    Each section will be generated as a separate audio segment with
    longer silence between them.
    """
    sections = re.split(r'\n*\[BREAK\]\n*', text)
    return [s.strip() for s in sections if s.strip()]


def generate_audio(
    text: str,
    output_path: str,
    voice: str = "af_heart",
    speed: float = 1.0,
    lang: str = "a",
    keep_temp: bool = False,
) -> None:
    """Generate audio from text and save to a WAV file.

    Handles markdown cleaning, TTS preparation, section splitting,
    chunk generation, and concatenation.

    Args:
        text: Input text (may contain markdown, [BREAK] markers, phonetic links)
        output_path: Path for the output WAV file
        voice: Voice to use for TTS
        speed: Speech speed multiplier
        lang: Language code
        keep_temp: Whether to keep temporary chunk files
    """
    text = prepare_for_tts(text)

    if not text.strip():
        raise ValueError("Input text is empty or contains no readable text")

    sections = split_sections(text)

    model = load_model("mlx-community/Kokoro-82M-bf16")

    temp_dir = tempfile.mkdtemp(prefix="kokoro_tts_")
    temp_files = []
    section_breaks = set()

    try:
        chunk_count = 0

        for section_idx, section in enumerate(sections):
            for result in model.generate(
                text=section,
                voice=voice,
                speed=speed,
                lang_code=lang,
            ):
                if result.audio is not None:
                    chunk_path = os.path.join(temp_dir, f"chunk_{chunk_count:04d}.wav")
                    sf.write(chunk_path, np.array(result.audio), 24000)
                    temp_files.append(chunk_path)
                    chunk_count += 1

            if section_idx < len(sections) - 1 and temp_files:
                section_breaks.add(chunk_count - 1)

        if not temp_files:
            raise RuntimeError("No audio was generated")

        concatenate_audio_files(temp_files, output_path, section_breaks=section_breaks)

    finally:
        if not keep_temp:
            for temp_file in temp_files:
                try:
                    os.unlink(temp_file)
                except OSError:
                    pass
            try:
                os.rmdir(temp_dir)
            except OSError:
                pass


def list_available_voices():
    """List all available voices organized by language."""
    voices = {
        'American English (lang: a)': {
            'Female': ['af_alloy', 'af_aoede', 'af_bella', 'af_heart', 'af_jessica', 
                      'af_kore', 'af_nicole', 'af_nova', 'af_river', 'af_sarah', 'af_sky'],
            'Male': ['am_adam', 'am_echo', 'am_eric', 'am_fenrir', 'am_liam', 
                    'am_michael', 'am_onyx', 'am_puck', 'am_santa']
        },
        'British English (lang: b)': {
            'Female': ['bf_alice', 'bf_emma', 'bf_isabella', 'bf_lily'],
            'Male': ['bm_daniel', 'bm_fable', 'bm_george', 'bm_lewis']
        },
        'Spanish (lang: e)': {
            'Female': ['ef_dora'],
            'Male': ['em_alex', 'em_santa']
        },
        'French (lang: f)': {
            'Female': ['ff_siwis'],
            'Male': []
        },
        'Hindi (lang: h)': {
            'Female': ['hf_alpha', 'hf_beta'],
            'Male': ['hm_omega', 'hm_psi']
        },
        'Italian (lang: i)': {
            'Female': ['if_sara'],
            'Male': ['im_nicola']
        },
        'Japanese (lang: j)': {
            'Female': ['jf_alpha', 'jf_gongitsune', 'jf_nezumi', 'jf_tebukuro'],
            'Male': ['jm_kumo']
        },
        'Portuguese (lang: p)': {
            'Female': ['pf_dora'],
            'Male': ['pm_alex', 'pm_santa']
        },
        'Chinese (lang: z)': {
            'Female': ['zf_xiaobei', 'zf_xiaoni', 'zf_xiaoxiao', 'zf_xiaoyi'],
            'Male': ['zm_yunjian', 'zm_yunxi', 'zm_yunxia', 'zm_yunyang']
        }
    }
    
    print("Available Voices by Language:\n")
    for language, genders in voices.items():
        print(f"🗣️  {language}")
        for gender, voice_list in genders.items():
            if voice_list:
                print(f"   {gender}: {', '.join(voice_list)}")
        print()
    
    print("Usage: python text_to_speech.py document.txt --voice VOICE_NAME --lang LANG_CODE")
    print("Example: python text_to_speech.py document.txt --voice af_bella --lang a")


def concatenate_audio_files(audio_files: List[str], output_path: str, section_breaks: set = None) -> None:
    """Concatenate multiple audio files into one.

    Args:
        audio_files: List of WAV file paths to concatenate
        output_path: Path for the output file
        section_breaks: Set of chunk indices after which to insert a longer
                       section pause (2s) instead of the normal inter-chunk pause (300ms)
    """
    if not audio_files:
        raise ValueError("No audio files to concatenate")

    if section_breaks is None:
        section_breaks = set()

    combined = AudioSegment.empty()

    for i, audio_file in enumerate(audio_files):
        audio = AudioSegment.from_wav(audio_file)
        combined += audio
        if i in section_breaks:
            combined += AudioSegment.silent(duration=2000)  # 2s section pause
        else:
            combined += AudioSegment.silent(duration=300)  # 300ms chunk pause
    
    combined.export(output_path, format="wav")


def main():
    parser = argparse.ArgumentParser(
        description="Convert text files to speech using MLX Audio (Apple Silicon optimized)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python text_to_speech.py document.txt
  python text_to_speech.py README.md --voice af_bella --output my_audio.wav
  python text_to_speech.py --help
        """
    )
    
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Input text file (supports .txt, .md, and other text formats). Use '-' or omit to read from stdin."
    )
    
    parser.add_argument(
        "--output", "-o",
        help="Output audio file path (default: input_filename.wav)"
    )
    
    parser.add_argument(
        "--voice", "-v",
        default="af_heart",
        help="Voice to use for TTS (default: af_heart)"
    )
    
    parser.add_argument(
        "--speed", "-s",
        type=float,
        default=1.0,
        help="Speech speed multiplier (default: 1.0)"
    )
    
    parser.add_argument(
        "--lang", "-l",
        default="a",
        help="Language code (a=American English, b=British English, etc.)"
    )
    
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temporary audio chunk files for debugging"
    )
    
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="List all available voices and exit"
    )
    
    parser.add_argument(
        "--markdown", "-m",
        action="store_true",
        help="Treat input as markdown and clean formatting (auto-detected for .md/.markdown files)"
    )
    
    args = parser.parse_args()
    
    # Handle --list-voices option
    if args.list_voices:
        list_available_voices()
        sys.exit(0)

    # Determine if reading from stdin
    reading_from_stdin = not args.input_file or args.input_file == '-'
    
    # Validate input
    if not reading_from_stdin and not os.path.exists(args.input_file):
        print(f"Error: Input file '{args.input_file}' not found.", file=sys.stderr)
        sys.exit(1)
    
    # Check if stdin has data when reading from stdin
    if reading_from_stdin and sys.stdin.isatty():
        print("Error: No input provided. Specify a file or pipe text to stdin.", file=sys.stderr)
        print("Examples:")
        print("  python text_to_speech.py document.txt")
        print("  echo 'Hello world' | python text_to_speech.py")
        print("  head -n 10 document.txt | python text_to_speech.py")
        sys.exit(1)
    
    # Determine output path
    if args.output:
        output_path = args.output
    elif reading_from_stdin:
        output_path = "output.wav"
    else:
        input_path = Path(args.input_file)
        output_path = str(input_path.with_suffix('.wav'))
    
    try:
        # Read and clean the text
        if reading_from_stdin:
            print("Reading from stdin...")
            text = sys.stdin.read()
            is_markdown = args.markdown
        else:
            print("Reading input file...")
            text = read_text_file(args.input_file)
            is_markdown = args.markdown or args.input_file.lower().endswith(('.md', '.markdown'))
        
        # Clean markdown if specified or auto-detected
        if is_markdown:
            print("Cleaning markdown formatting...")
            text = clean_markdown_text(text)

        generate_audio(
            text, output_path,
            voice=args.voice,
            speed=args.speed,
            lang=args.lang,
            keep_temp=args.keep_temp,
        )
        print(f"Audio saved to: {output_path}")
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()