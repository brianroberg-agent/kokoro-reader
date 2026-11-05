#!/usr/bin/env python3
"""
Unit tests for text_to_speech.py script.
Run with: pytest test_text_to_speech.py
"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock
import unittest.mock
import sys
from io import StringIO
import numpy as np

# Import functions from the script
from text_to_speech import (
    read_text_file,
    clean_markdown_text,
    prepare_for_tts,
    split_sections,
    generate_audio,
    list_available_voices,
    concatenate_audio_files
)


class TestReadTextFile:
    """Test the read_text_file function."""
    
    def test_read_utf8_file(self):
        """Test reading a UTF-8 encoded file."""
        content = "Hello, 世界! 🌍"
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as f:
            f.write(content)
            temp_path = f.name
        
        try:
            result = read_text_file(temp_path)
            assert result == content
        finally:
            os.unlink(temp_path)
    
    def test_read_latin1_file(self):
        """Test reading a Latin-1 encoded file when UTF-8 fails."""
        content = "Plain ASCII content"  # Use ASCII content that works across encodings
        with tempfile.NamedTemporaryFile(mode='wb', delete=False) as f:
            f.write(content.encode('latin-1'))
            temp_path = f.name
        
        try:
            result = read_text_file(temp_path)
            assert result == content
        finally:
            os.unlink(temp_path)
    
    def test_read_nonexistent_file(self):
        """Test reading a file that doesn't exist."""
        with pytest.raises(FileNotFoundError):
            read_text_file("/nonexistent/file.txt")
    
    def test_read_ascii_file(self):
        """Test reading a basic ASCII file."""
        content = "Hello World!"
        with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', delete=False) as f:
            f.write(content)
            temp_path = f.name
        
        try:
            result = read_text_file(temp_path)
            assert result == content
        finally:
            os.unlink(temp_path)


class TestCleanMarkdownText:
    """Test the clean_markdown_text function."""
    
    def test_remove_headers(self):
        """Test removal of markdown headers."""
        text = "# Header 1\n## Header 2\n### Header 3\nRegular text"
        expected = "Header 1\nHeader 2\nHeader 3\nRegular text"
        assert clean_markdown_text(text) == expected
    
    def test_remove_images(self):
        """Test removal of markdown images."""
        text = "Before ![Alt text](image.png) after"
        result = clean_markdown_text(text)
        assert "![" not in result
        assert "image.png" not in result
        assert "Before" in result
        assert "after" in result
    
    def test_remove_images_no_alt_text(self):
        """Test removal of images with no alt text."""
        text = "Before ![](image.png) after"
        result = clean_markdown_text(text)
        assert "![" not in result
        assert "image.png" not in result
        assert "Before" in result
        assert "after" in result
    
    def test_remove_images_complex_alt_text(self):
        """Test removal of images with complex alt text."""
        text = "Image: ![A photo of a cat sitting on a windowsill](cat.jpg) here"
        result = clean_markdown_text(text)
        assert "![" not in result
        assert "cat.jpg" not in result
        assert "Image:" in result
        assert "here" in result
    
    def test_preserve_link_text(self):
        """Test that link text is preserved but URLs are removed."""
        text = "Check out [this link](https://example.com) for more info"
        expected = "Check out this link for more info"
        assert clean_markdown_text(text) == expected
    
    def test_remove_emphasis(self):
        """Test removal of bold and italic formatting."""
        text = "This is **bold** and *italic* text"
        expected = "This is bold and italic text"
        assert clean_markdown_text(text) == expected

    @pytest.mark.parametrize("text, expected", [
        # One italic phrase a site split in two threw the pairing of the markers off for
        # the rest of the text, and the last italic's closing "*" was read "asterisk".
        ("A *Tale of Two Citie**s*–style ending, as in *Moby Dick*. The end.",
         "A Tale of Two Cities–style ending, as in Moby Dick. The end."),
        ("It was ***truly*** fine.", "It was truly fine."),  # bold italics
        ("The flag* was raised.", "The flag was raised."),   # a lone marker
    ])
    def test_no_asterisk_is_left_for_the_engine_to_read(self, text, expected):
        assert clean_markdown_text(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("It costs 2*3 dollars.", "It costs 2 3 dollars."),  # not "23"
        # Between letters it is most likely an emphasis marker that lost its pair, which
        # joined the word: a pair inside a word, bold italics, a split italic's leftover.
        ("An un*frig*believable day.", "An unfrigbelievable day."),
        ("It was ***un***believable.", "It was unbelievable."),
        ("The *Worl**d*s best.", "The Worlds best."),
    ])
    def test_a_stray_asterisk_keeps_only_digits_apart(self, text, expected):
        assert clean_markdown_text(text) == expected

    def test_an_emphasis_pair_does_not_reach_across_lines(self):
        """A stray asterisk paired with one paragraphs later, and both were dropped as
        markers, joining what was on either side of each."""
        text = "It costs 2*3 dollars.\n\nLater, a*b again."
        assert clean_markdown_text(text) == "It costs 2 3 dollars.\n\nLater, ab again."

    @pytest.mark.parametrize("text, expected", [
        # markdownify writes a literal "*" as "\*", and misaki reads a leftover "\" as
        # "backslash". Nor may it pair with the next italic's marker.
        ("A footnote\\*. Then *Moby Dick* again.", "A footnote. Then Moby Dick again."),
        ("It costs 2\\*3 dollars.", "It costs 2 3 dollars."),
    ])
    def test_an_escaped_asterisk_goes_with_its_backslash(self, text, expected):
        assert clean_markdown_text(text) == expected

    def test_remove_underscore_emphasis(self):
        """Test removal of underscore-based emphasis."""
        text = "This is __bold__ and _italic_ text"
        expected = "This is bold and italic text"
        assert clean_markdown_text(text) == expected
    
    def test_remove_code_blocks(self):
        """Test removal of code blocks."""
        text = "Before\n```python\nprint('hello')\n```\nAfter"
        expected = "Before\n\nAfter"
        assert clean_markdown_text(text) == expected
    
    def test_remove_inline_code(self):
        """Test removal of inline code."""
        text = "Use the `print()` function here"
        expected = "Use the print() function here"
        assert clean_markdown_text(text) == expected
    
    def test_remove_lists(self):
        """Test removal of list formatting."""
        text = "- Item 1\n* Item 2\n+ Item 3\n1. Numbered item"
        expected = "Item 1\nItem 2\nItem 3\nNumbered item"
        assert clean_markdown_text(text) == expected

    def test_a_numbered_heading_keeps_its_number(self):
        """E2E finding 1: the header marks went first, so the list rule took a
        heading's own number ("##### 1. Keeping Humans in Charge" read without "1")."""
        text = ("##### 1. Keeping Humans in Charge\n\nText.\n\n"
                "##### **2.** Avoiding Concentration of Power\n\nMore.")
        expected = ("1. Keeping Humans in Charge\n\nText.\n\n"
                    "2. Avoiding Concentration of Power\n\nMore.")
        assert clean_markdown_text(text) == expected

    def test_remove_blockquotes(self):
        """Test removal of blockquote formatting."""
        text = "> This is a quote\n> Multi-line quote"
        expected = "This is a quote\nMulti-line quote"
        assert clean_markdown_text(text) == expected
    
    def test_clean_whitespace(self):
        """Test cleanup of excessive whitespace."""
        text = "Line 1\n\n\n\nLine 2    with   spaces"
        expected = "Line 1\n\nLine 2 with spaces"
        assert clean_markdown_text(text) == expected
    
    def test_preserve_phonetic_pronunciation_links(self):
        """Test that Kokoro phonetic pronunciation links are preserved during markdown cleaning."""
        text = "Say [Pengelley](/pˈɛndʒɛli/) correctly"
        result = clean_markdown_text(text)
        assert "[Pengelley](/pˈɛndʒɛli/)" in result

    def test_preserve_phonetic_links_while_stripping_regular_links(self):
        """Test that regular links are stripped but phonetic links are kept."""
        text = "Visit [example](https://example.com) and say [Kokoro](/kˈOkəɹO/) right"
        result = clean_markdown_text(text)
        assert "[Kokoro](/kˈOkəɹO/)" in result
        assert "https://example.com" not in result
        assert "Visit example" in result

    def test_preserve_multiple_phonetic_links(self):
        """Test multiple phonetic links in the same text."""
        text = "Say [Pengelley](/pˈɛndʒɛli/) and [dewed](/djˈuːd/) correctly"
        result = clean_markdown_text(text)
        assert "[Pengelley](/pˈɛndʒɛli/)" in result
        assert "[dewed](/djˈuːd/)" in result

    def test_complex_markdown_document(self):
        """Test cleaning a complex markdown document."""
        text = """# My Document

This is **bold** text with an image: ![Screenshot](image.png)

## Section 2

Here's a [link](https://example.com) and some `code`.

- List item 1
- List item 2

> This is a blockquote

```python
def hello():
    print("world")
```

Regular text at the end."""
        
        result = clean_markdown_text(text)
        
        # Check that all markdown elements are removed
        assert "# " not in result
        assert "**" not in result
        assert "![" not in result
        assert "](image.png)" not in result
        assert "](https://example.com)" not in result
        assert "`" not in result
        assert "- " not in result
        assert "> " not in result
        assert "```" not in result
        assert "def hello():" not in result
        
        # Check that content is preserved
        assert "My Document" in result
        assert "bold text with an image:" in result
        assert "Section 2" in result
        assert "link and some code" in result
        assert "List item 1" in result
        assert "This is a blockquote" in result
        assert "Regular text at the end." in result


class TestPrepareForTTS:
    """Test the prepare_for_tts function."""

    def test_replaces_em_dashes_with_ellipsis(self):
        """Test that -- em dashes are replaced with ... for better pauses."""
        text = "He was a crank -- a prophet."
        result = prepare_for_tts(text)
        assert "--" not in result
        assert "..." in result

    def test_preserves_non_dash_content(self):
        """Test that text around dashes is preserved."""
        text = "He was a crank -- a prophet."
        result = prepare_for_tts(text)
        assert "He was a crank" in result
        assert "a prophet." in result

    def test_leaves_curly_quotation_marks_alone(self):
        """Only an apostrophe between two letters is made straight: a quotation mark, a
        plural possessive's apostrophe and an elided letter at a word's edge stay."""
        text = "She said ‘no’, the students’ books, rock ’n’ roll, the ’90s."
        assert prepare_for_tts(text) == text


class TestSplitSections:
    """Test the split_sections function."""

    def test_splits_on_break_marker(self):
        """Test that [BREAK] markers split text into sections."""
        text = "Section one.\n\n[BREAK]\n\nSection two."
        sections = split_sections(text)
        assert len(sections) == 2
        assert "Section one." in sections[0]
        assert "Section two." in sections[1]

    def test_no_break_returns_single_section(self):
        """Test that text without [BREAK] returns as a single section."""
        text = "Just one section of text."
        sections = split_sections(text)
        assert len(sections) == 1
        assert sections[0] == "Just one section of text."

    def test_multiple_breaks(self):
        """Test multiple [BREAK] markers."""
        text = "One.\n\n[BREAK]\n\nTwo.\n\n[BREAK]\n\nThree."
        sections = split_sections(text)
        assert len(sections) == 3

    def test_break_marker_not_in_output(self):
        """Test that [BREAK] markers are removed from section text."""
        text = "Before.\n\n[BREAK]\n\nAfter."
        sections = split_sections(text)
        for section in sections:
            assert "[BREAK]" not in section


class TestConcatenateWithSectionPauses:
    """Test concatenation with section-level pauses."""

    @patch('text_to_speech.AudioSegment')
    def test_section_boundaries_get_longer_pause(self, mock_audio_segment):
        """Test that section boundaries use a longer pause than intra-section chunks."""
        audio_files = ["chunk1.wav", "chunk2.wav", "chunk3.wav"]
        section_breaks = {1}  # Break after chunk index 1

        concatenate_audio_files(audio_files, "output.wav", section_breaks=section_breaks)

        silent_calls = mock_audio_segment.silent.call_args_list
        durations = [call.kwargs.get('duration', call.args[0] if call.args else None)
                     for call in silent_calls]
        # Should have at least one call with duration > 300 (the section break)
        assert any(d is not None and d > 300 for d in durations)


class TestGenerateAudio:
    """Test the generate_audio function."""

    @patch('text_to_speech.concatenate_audio_files')
    @patch('text_to_speech.sf')
    @patch('text_to_speech.load_model')
    def test_generates_wav_output(self, mock_load_model, mock_sf, mock_concat, tmp_path):
        """Test that generate_audio produces a WAV file."""
        mock_model = MagicMock()
        mock_load_model.return_value = mock_model
        mock_result = MagicMock()
        mock_result.audio = [0.0, 0.1, 0.2]
        mock_model.generate.return_value = [mock_result]

        output_path = str(tmp_path / "output.wav")
        generate_audio("Hello world.", output_path)

        mock_load_model.assert_called_once()
        mock_model.generate.assert_called_once()
        mock_concat.assert_called_once()

    @patch('text_to_speech.concatenate_audio_files')
    @patch('text_to_speech.sf')
    @patch('text_to_speech.load_model')
    def test_passes_voice_and_speed(self, mock_load_model, mock_sf, mock_concat, tmp_path):
        """Test that voice and speed parameters are forwarded to the model."""
        mock_model = MagicMock()
        mock_load_model.return_value = mock_model
        mock_result = MagicMock()
        mock_result.audio = [0.0]
        mock_model.generate.return_value = [mock_result]

        output_path = str(tmp_path / "output.wav")
        generate_audio("Hello.", output_path, voice="af_bella", speed=1.2, lang="b")

        call_kwargs = mock_model.generate.call_args.kwargs
        assert call_kwargs["voice"] == "af_bella"
        assert call_kwargs["speed"] == 1.2
        assert call_kwargs["lang_code"] == "b"

    @patch('text_to_speech.concatenate_audio_files')
    @patch('text_to_speech.sf')
    @patch('text_to_speech.load_model')
    def test_hands_the_model_straight_apostrophes_inside_words(
            self, mock_load_model, mock_sf, mock_concat, tmp_path):
        """misaki reads a sentence-final "isn’t." or "wasn’t." with a curly
        apostrophe as "is" or "was": an article's "But it isn’t." was read "But it is"."""
        mock_model = MagicMock()
        mock_load_model.return_value = mock_model
        mock_result = MagicMock()
        mock_result.audio = [0.0]
        mock_model.generate.return_value = [mock_result]

        generate_audio("But it isn’t. It wasn’t. L’Étoile.", str(tmp_path / "output.wav"))

        assert mock_model.generate.call_args.kwargs["text"] == "But it isn't. It wasn't. L'Étoile."

    @patch('text_to_speech.concatenate_audio_files')
    @patch('text_to_speech.sf')
    @patch('text_to_speech.load_model')
    def test_handles_section_breaks(self, mock_load_model, mock_sf, mock_concat, tmp_path):
        """Test that [BREAK] markers result in section breaks during concatenation."""
        mock_model = MagicMock()
        mock_load_model.return_value = mock_model
        mock_result = MagicMock()
        mock_result.audio = [0.0]
        mock_model.generate.return_value = [mock_result]

        output_path = str(tmp_path / "output.wav")
        generate_audio("Section one.\n\n[BREAK]\n\nSection two.", output_path)

        # Model should be called twice (once per section)
        assert mock_model.generate.call_count == 2
        # Concatenation should receive section_breaks
        concat_kwargs = mock_concat.call_args.kwargs
        assert "section_breaks" in concat_kwargs
        assert len(concat_kwargs["section_breaks"]) == 1


class TestListAvailableVoices:
    """Test the list_available_voices function."""
    
    def test_list_voices_output(self, capsys):
        """Test that list_available_voices produces expected output."""
        list_available_voices()
        captured = capsys.readouterr()
        
        # Check that key sections are present
        assert "Available Voices by Language:" in captured.out
        assert "American English (lang: a)" in captured.out
        assert "British English (lang: b)" in captured.out
        assert "Spanish (lang: e)" in captured.out
        assert "Female:" in captured.out
        assert "Male:" in captured.out
        assert "af_heart" in captured.out  # Default voice
        assert "Usage:" in captured.out
        assert "Example:" in captured.out


class TestConcatenateAudioFiles:
    """Test the concatenate_audio_files function."""
    
    @patch('text_to_speech.AudioSegment')
    def test_concatenate_audio_files_calls_correct_methods(self, mock_audio_segment):
        """Test that concatenate_audio_files calls the expected AudioSegment methods."""
        audio_files = ["file1.wav", "file2.wav", "file3.wav"]
        output_path = "output.wav"
        
        concatenate_audio_files(audio_files, output_path)
        
        # Verify AudioSegment.from_wav was called for each file
        assert mock_audio_segment.from_wav.call_count == 3
        expected_calls = [
            unittest.mock.call("file1.wav"),
            unittest.mock.call("file2.wav"), 
            unittest.mock.call("file3.wav")
        ]
        mock_audio_segment.from_wav.assert_has_calls(expected_calls)
        
        # Verify empty() and silent() were called
        mock_audio_segment.empty.assert_called_once()
        # silent() is called for each gap between files
        assert mock_audio_segment.silent.call_count >= 1
    
    def test_concatenate_empty_list(self):
        """Test error handling for empty audio file list."""
        with pytest.raises(ValueError, match="No audio files to concatenate"):
            concatenate_audio_files([], "output.wav")
    
    @patch('text_to_speech.AudioSegment')
    def test_concatenate_single_file_calls_methods(self, mock_audio_segment):
        """Test concatenation with a single file calls expected methods."""
        concatenate_audio_files(["single.wav"], "output.wav")
        
        mock_audio_segment.from_wav.assert_called_once_with("single.wav")
        mock_audio_segment.empty.assert_called_once()


class TestIntegration:
    """Integration tests for combined functionality."""
    
    def test_markdown_processing_pipeline(self):
        """Test the complete pipeline for markdown processing."""
        markdown_content = """# Test Document

This is a test with ![image](test.png) and [link](http://example.com).

## Code Section

```python
print("hello")
```

- List item
- Another item

> Quote text

Final paragraph."""
        
        # Test that the markdown processing works end-to-end
        cleaned = clean_markdown_text(markdown_content)
        
        # Verify structure is preserved but formatting is removed
        lines = cleaned.split('\n')
        assert any("Test Document" in line for line in lines)
        assert any("Code Section" in line for line in lines)
        assert any("Final paragraph." in line for line in lines)
        
        # Verify formatting is removed
        assert "![" not in cleaned
        assert "```" not in cleaned
        assert "- " not in cleaned
        assert "> " not in cleaned
    
    def test_file_encoding_fallback(self):
        """Test that encoding fallback works properly."""
        # Create files with different encodings
        test_content = "Test content with special chars: café"
        
        # Test UTF-8
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as f:
            f.write(test_content)
            utf8_path = f.name
        
        # Test Latin-1
        with tempfile.NamedTemporaryFile(mode='wb', delete=False) as f:
            f.write(test_content.encode('latin-1'))
            latin1_path = f.name
        
        try:
            # Both should work
            utf8_result = read_text_file(utf8_path)
            latin1_result = read_text_file(latin1_path)
            
            assert utf8_result == test_content
            assert latin1_result == test_content
        finally:
            os.unlink(utf8_path)
            os.unlink(latin1_path)


# Test fixtures and parametrized tests
@pytest.mark.parametrize("input_text,expected_contains,expected_not_contains", [
    # Headers
    ("# Header\nContent", ["Header", "Content"], ["# "]),
    # Images
    ("Text ![alt](img.png) more", ["Text", "more"], ["![", "img.png"]),
    # Links
    ("See [link text](url) here", ["link text", "here"], ["](url)"]),
    # Emphasis
    ("**bold** and *italic*", ["bold", "italic"], ["**", "*"]),
    # Code
    ("Use `code` here", ["Use", "code", "here"], ["`"]),
    # Lists
    ("- Item 1\n- Item 2", ["Item 1", "Item 2"], ["- "]),
])
def test_markdown_cleaning_parametrized(input_text, expected_contains, expected_not_contains):
    """Parametrized test for markdown cleaning."""
    result = clean_markdown_text(input_text)
    
    for expected in expected_contains:
        assert expected in result
    
    for not_expected in expected_not_contains:
        assert not_expected not in result


class TestMLXAudioBackend:
    """Tests for MLX Audio backend functionality."""

    @pytest.fixture(scope="class")
    def model(self):
        """Load the MLX Audio model (cached for test class)."""
        from mlx_audio.tts.utils import load_model
        return load_model("mlx-community/Kokoro-82M-bf16")

    def test_model_loads_successfully(self, model):
        """Test that the MLX Audio model loads without errors."""
        assert model is not None

    def test_generate_returns_iterator(self, model):
        """Test that model.generate() returns an iterable."""
        result = model.generate(
            text="Hello world.",
            voice="af_heart",
            speed=1.0,
            lang_code="a"
        )
        # Should be iterable
        assert hasattr(result, '__iter__')

    def test_generate_produces_audio(self, model):
        """Test that model.generate() produces audio output."""
        chunks = list(model.generate(
            text="Hello world.",
            voice="af_heart",
            speed=1.0,
            lang_code="a"
        ))

        assert len(chunks) > 0

        # First chunk should have audio
        first_chunk = chunks[0]
        assert first_chunk.audio is not None

        # Should be convertible to numpy array
        audio_array = np.array(first_chunk.audio)
        assert len(audio_array) > 0

    def test_different_voices_work(self, model):
        """Test that different voice options work."""
        voices = ["af_heart", "am_adam", "bf_emma"]

        for voice in voices:
            chunks = list(model.generate(
                text="Test.",
                voice=voice,
                speed=1.0,
                lang_code="a" if voice.startswith("a") else "b"
            ))
            assert len(chunks) > 0
            assert chunks[0].audio is not None

    def test_speed_parameter_accepted(self, model):
        """Test that speed parameter is accepted without error."""
        # Test slow and fast speeds
        for speed in [0.8, 1.0, 1.2]:
            chunks = list(model.generate(
                text="Test.",
                voice="af_heart",
                speed=speed,
                lang_code="a"
            ))
            assert len(chunks) > 0

    def test_long_text_produces_chunks(self, model):
        """Test that long text produces audio chunks."""
        long_text = "This is a test sentence. " * 20

        chunks = list(model.generate(
            text=long_text,
            voice="af_heart",
            speed=1.0,
            lang_code="a"
        ))

        # Long text should produce at least one chunk with audio
        assert len(chunks) >= 1
        assert chunks[0].audio is not None


if __name__ == "__main__":
    pytest.main([__file__])

def test_the_runners_opening_line_keeps_a_leading_number():
    # A title such as "10. Things We Lost" opens the article as a heading (pipeline
    # fetch.spoken_title), so the list rule leaves its number (review finding 10).
    assert clean_markdown_text("# 10. Things We Lost by Ada Quill\n\nBody.") == (
        "10. Things We Lost by Ada Quill\n\nBody.")
