#!/usr/bin/env python3
"""
Unit tests for verify_audio.py script.
Run with: pytest test_verify_audio.py
"""

import pytest
from unittest.mock import patch, MagicMock

from pydub import AudioSegment
from pydub.generators import Sine

from verify_audio import (
    verify_audio,
    main,
    precheck_report,
    precheck_warnings,
    format_timestamp,
    shift_timestamps,
    find_section_gaps,
    split_audio_at_gaps,
)


def make_segment(spec):
    """Build an AudioSegment from a spec like [("tone", 1000), ("silence", 2000)].

    Durations are in milliseconds. Tones are full-scale 440 Hz sine waves,
    well above any silence-detection threshold.
    """
    segment = AudioSegment.empty()
    for kind, duration_ms in spec:
        if kind == "tone":
            piece = Sine(440).to_audio_segment(duration=duration_ms)
            piece = piece.set_frame_rate(24000).set_channels(1)
        elif kind == "silence":
            piece = AudioSegment.silent(duration=duration_ms, frame_rate=24000)
        else:
            raise ValueError(f"Unknown spec kind: {kind}")
        segment += piece
    return segment


def make_wav(path, spec=(("tone", 500),)):
    """Write a small real WAV file built from a spec. Returns the path as str."""
    make_segment(spec).export(str(path), format="wav")
    return str(path)


def mock_gemini_client(mock_genai, response_text="No issues found."):
    """Wire up a mocked genai module and return the mocked client."""
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = response_text
    mock_client.models.generate_content.return_value = mock_response
    uploaded = MagicMock()
    uploaded.state.name = "ACTIVE"
    mock_client.files.upload.return_value = uploaded
    return mock_client


class TestUploadReadiness:
    """Test that generation waits for the Files API to finish processing.

    files.upload() can return while the file is still PROCESSING; querying
    the model against such a file makes it behave as if no audio were
    attached (observed on a 28-minute upload during issue #4 calibration).
    """

    @patch("verify_audio.time.sleep")
    @patch("verify_audio.genai")
    def test_waits_for_processing_upload_to_become_active(
        self, mock_genai, mock_sleep, tmp_path
    ):
        """Test a PROCESSING upload is polled until ACTIVE before generating."""
        mock_client = mock_gemini_client(mock_genai)
        uploaded = MagicMock()
        uploaded.state.name = "PROCESSING"
        uploaded.name = "files/abc123"
        mock_client.files.upload.return_value = uploaded
        ready = MagicMock()
        ready.state.name = "ACTIVE"
        still_processing = MagicMock()
        still_processing.state.name = "PROCESSING"
        mock_client.files.get.side_effect = [still_processing, ready]
        audio_path = make_wav(tmp_path / "audio.wav")

        verify_audio(audio_path, "Some text.")

        assert mock_client.files.get.call_count == 2
        mock_client.models.generate_content.assert_called_once()

    @patch("verify_audio.time.sleep")
    @patch("verify_audio.genai")
    def test_failed_upload_raises(self, mock_genai, mock_sleep, tmp_path):
        """Test a FAILED upload raises instead of querying the model."""
        mock_client = mock_gemini_client(mock_genai)
        failed = MagicMock()
        failed.state.name = "FAILED"
        mock_client.files.upload.return_value = failed
        audio_path = make_wav(tmp_path / "audio.wav")

        with pytest.raises(RuntimeError, match="FAILED"):
            verify_audio(audio_path, "Some text.")
        mock_client.models.generate_content.assert_not_called()


class TestVerifyAudio:
    """Test the verify_audio function."""

    @patch("verify_audio.genai")
    def test_the_gemini_client_has_a_request_timeout(self, mock_genai, tmp_path):
        """Review F3: with no HTTP timeout, a request on a dead connection never
        returned, and the narration server's item lock was held for good."""
        import verify_audio as va
        mock_gemini_client(mock_genai, "No issues found.")
        verify_audio(make_wav(tmp_path / "audio.wav"), "Some source text.")
        options = mock_genai.Client.call_args.kwargs["http_options"]
        assert options.timeout == va.GEMINI_TIMEOUT_MS
        assert 0 < va.GEMINI_TIMEOUT_MS <= 30 * 60 * 1000

    @patch("verify_audio.genai")
    def test_returns_model_response_text(self, mock_genai, tmp_path):
        """Test that verify_audio returns the model's response as a string."""
        mock_gemini_client(mock_genai, "No issues found.")
        audio_path = make_wav(tmp_path / "audio.wav")

        result = verify_audio(audio_path, "Some source text.")
        assert "No issues found." in result

    @patch("verify_audio.genai")
    def test_sends_source_text_in_prompt(self, mock_genai, tmp_path):
        """Test that the source text is included in the prompt sent to Gemini."""
        mock_client = mock_gemini_client(mock_genai, "All good.")
        audio_path = make_wav(tmp_path / "audio.wav")

        verify_audio(audio_path, "The quick brown fox.")
        call_args = mock_client.models.generate_content.call_args
        # The source text should appear somewhere in the contents sent
        contents = str(call_args)
        assert "The quick brown fox." in contents

    @patch("verify_audio.genai")
    def test_uploads_audio_file(self, mock_genai, tmp_path):
        """Test that the audio file is uploaded via the client."""
        mock_client = mock_gemini_client(mock_genai, "All good.")
        audio_path = make_wav(tmp_path / "audio.wav")

        verify_audio(audio_path, "Some text.")
        mock_client.files.upload.assert_called_once()
        upload_args = mock_client.files.upload.call_args
        assert audio_path in str(upload_args)

    @patch("verify_audio.genai")
    def test_uses_specified_model(self, mock_genai, tmp_path):
        """Test that the specified model is passed to generate_content."""
        mock_client = mock_gemini_client(mock_genai, "All good.")
        audio_path = make_wav(tmp_path / "audio.wav")

        verify_audio(audio_path, "Some text.", model="gemini-2.5-pro")
        call_args = mock_client.models.generate_content.call_args
        assert call_args.kwargs.get("model") == "gemini-2.5-pro" or "gemini-2.5-pro" in str(call_args)

    @patch("verify_audio.genai")
    def test_uses_audio_already_decoded_by_the_caller(self, mock_genai, tmp_path):
        """A caller that decoded the WAV (for its own pre-check) passes it in;
        verify_audio then reads the file only to upload it."""
        mock_gemini_client(mock_genai, "No issues found.")
        audio_path = make_wav(tmp_path / "audio.wav", [("tone", 4000)])
        decoded = AudioSegment.from_wav(audio_path)

        with patch("verify_audio.AudioSegment.from_wav", side_effect=AssertionError("decoded twice")):
            result = verify_audio(audio_path, "one two three four five six seven eight nine ten",
                                  audio=decoded)

        assert result.startswith("## Pre-check") and "0:04" in result
        assert "No issues found." in result

    @patch("verify_audio.genai")
    def test_report_starts_with_precheck(self, mock_genai, tmp_path):
        """Test the returned report leads with the deterministic pre-check."""
        mock_gemini_client(mock_genai, "No issues found.")
        audio_path = make_wav(tmp_path / "audio.wav", [("tone", 4000)])

        result = verify_audio(audio_path, "one two three four five six seven eight nine ten")

        assert result.startswith("## Pre-check")
        assert "0:04" in result
        assert "No issues found." in result

    @patch("verify_audio.genai")
    def test_default_model_is_maintained_alias(self, mock_genai, tmp_path):
        """Test that the default model is the maintained gemini-flash-latest alias."""
        mock_client = mock_gemini_client(mock_genai)
        audio_path = make_wav(tmp_path / "audio.wav")

        verify_audio(audio_path, "Some text.")
        call_args = mock_client.models.generate_content.call_args
        assert call_args.kwargs.get("model") == "gemini-flash-latest"

    def test_raises_on_missing_audio_file(self):
        """Test that a missing audio file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            verify_audio("/nonexistent/audio.wav", "Some text.")


class TestChunkedVerification:
    """Test that [BREAK]-sectioned recordings are verified chunk by chunk."""

    TWO_SECTION_TEXT = "Alpha section text.\n\n[BREAK]\n\nBeta section text."
    TWO_SECTION_SPEC = [("tone", 1000), ("silence", 3200), ("tone", 1000)]

    @patch("verify_audio.genai")
    def test_verifies_each_section_against_its_text_slice(self, mock_genai, tmp_path):
        """Test each audio chunk is sent with only its own section's text."""
        mock_client = mock_gemini_client(mock_genai)
        audio_path = make_wav(tmp_path / "audio.wav", self.TWO_SECTION_SPEC)

        verify_audio(audio_path, self.TWO_SECTION_TEXT)

        assert mock_client.files.upload.call_count == 2
        assert mock_client.models.generate_content.call_count == 2
        first_prompt = str(mock_client.models.generate_content.call_args_list[0])
        second_prompt = str(mock_client.models.generate_content.call_args_list[1])
        assert "Alpha" in first_prompt and "Beta" not in first_prompt
        assert "Beta" in second_prompt and "Alpha" not in second_prompt

    @patch("verify_audio.genai")
    def test_chunk_prompts_include_section_context(self, mock_genai, tmp_path):
        """Test each chunk's prompt says which section of how many it is."""
        mock_client = mock_gemini_client(mock_genai)
        audio_path = make_wav(tmp_path / "audio.wav", self.TWO_SECTION_SPEC)

        verify_audio(audio_path, self.TWO_SECTION_TEXT)

        first_prompt = str(mock_client.models.generate_content.call_args_list[0])
        assert "section 1 of 2" in first_prompt

    @patch("verify_audio.genai")
    def test_report_has_section_headers_with_absolute_ranges(self, mock_genai, tmp_path):
        """Test the aggregated report labels each section with its time range."""
        mock_gemini_client(mock_genai, "No issues found.")
        audio_path = make_wav(tmp_path / "audio.wav", self.TWO_SECTION_SPEC)

        result = verify_audio(audio_path, self.TWO_SECTION_TEXT)

        assert "## Section 1 of 2 (0:00–0:02)" in result
        assert "## Section 2 of 2 (0:02–0:05)" in result
        assert "whole recording" in result  # note saying where the timestamps count from

    @patch("verify_audio.genai")
    def test_section_timestamps_are_times_in_the_whole_recording(self, mock_genai, tmp_path):
        """E2E finding 2: the agent had to add each section's start itself, and once
        published 0:10 for an issue at 6:28. The report does the adding now."""
        mock_gemini_client(mock_genai, "At 0:01, a word is dropped.")
        audio_path = make_wav(tmp_path / "audio.wav", self.TWO_SECTION_SPEC)

        result = verify_audio(audio_path, self.TWO_SECTION_TEXT)

        first, second = result.split("## Section 2 of 2")
        assert "At 0:01, a word is dropped." in first
        assert "At 0:03, a word is dropped." in second  # 0:01 into a section starting at 0:02

    @patch("verify_audio.genai")
    def test_single_section_text_verifies_whole_file_without_warning(
        self, mock_genai, tmp_path
    ):
        """Test text without [BREAK] keeps the plain whole-file behavior."""
        mock_client = mock_gemini_client(mock_genai)
        audio_path = make_wav(tmp_path / "audio.wav", [("tone", 2000)])

        result = verify_audio(audio_path, "Just one plain section of text.")

        assert mock_client.models.generate_content.call_count == 1
        assert "could not pair" not in result
        assert "## Section" not in result

    @patch("verify_audio.genai")
    def test_boundary_mismatch_falls_back_to_whole_file_with_warning(
        self, mock_genai, tmp_path
    ):
        """Test gap/section count disagreement warns and verifies whole file."""
        mock_client = mock_gemini_client(mock_genai)
        # Two text sections, but audio has no detectable gap
        audio_path = make_wav(tmp_path / "audio.wav", [("tone", 2000)])

        result = verify_audio(audio_path, self.TWO_SECTION_TEXT)

        assert mock_client.models.generate_content.call_count == 1
        prompt = str(mock_client.models.generate_content.call_args)
        assert "Alpha" in prompt and "Beta" in prompt
        assert "WARNING" in result
        assert "1 audio chunk" in result and "2 sections" in result


class TestSingleCallLimit:
    """Test that no single model call carries more audio than the threshold.

    Whole-file comparison is only reliable below MAX_SINGLE_CALL_MINUTES
    (issue #4); beyond it the tool must refuse rather than degrade. Tests
    shrink the threshold so fixtures stay tiny.
    """

    TWO_SECTION_TEXT = "Alpha section text.\n\n[BREAK]\n\nBeta section text."

    @patch("verify_audio.genai")
    def test_long_audio_without_breaks_is_rejected(
        self, mock_genai, tmp_path, monkeypatch
    ):
        """Test single-section text with over-threshold audio raises."""
        mock_client = mock_gemini_client(mock_genai)
        monkeypatch.setattr("verify_audio.MAX_SINGLE_CALL_MINUTES", 3 / 60)
        audio_path = make_wav(tmp_path / "audio.wav", [("tone", 4000)])

        with pytest.raises(ValueError, match="[BREAK]"):
            verify_audio(audio_path, "No break markers here.")
        mock_client.models.generate_content.assert_not_called()

    @patch("verify_audio.genai")
    def test_long_audio_with_mismatched_breaks_is_rejected(
        self, mock_genai, tmp_path, monkeypatch
    ):
        """Test the mismatch fallback also refuses over-threshold audio."""
        mock_client = mock_gemini_client(mock_genai)
        monkeypatch.setattr("verify_audio.MAX_SINGLE_CALL_MINUTES", 3 / 60)
        # Two text sections but no detectable gap: fallback path, too long
        audio_path = make_wav(tmp_path / "audio.wav", [("tone", 4000)])

        with pytest.raises(ValueError):
            verify_audio(audio_path, self.TWO_SECTION_TEXT)
        mock_client.models.generate_content.assert_not_called()

    @patch("verify_audio.genai")
    def test_oversized_single_chunk_is_rejected(
        self, mock_genai, tmp_path, monkeypatch
    ):
        """Test chunking doesn't help if one section alone exceeds the limit."""
        mock_client = mock_gemini_client(mock_genai)
        monkeypatch.setattr("verify_audio.MAX_SINGLE_CALL_MINUTES", 3 / 60)
        audio_path = make_wav(
            tmp_path / "audio.wav",
            [("tone", 4000), ("silence", 3200), ("tone", 1000)],
        )

        with pytest.raises(ValueError):
            verify_audio(audio_path, self.TWO_SECTION_TEXT)
        mock_client.models.generate_content.assert_not_called()

    @patch("verify_audio.genai")
    def test_short_audio_is_not_rejected(self, mock_genai, tmp_path, monkeypatch):
        """Test under-threshold whole-file verification still works."""
        mock_gemini_client(mock_genai)
        monkeypatch.setattr("verify_audio.MAX_SINGLE_CALL_MINUTES", 3 / 60)
        audio_path = make_wav(tmp_path / "audio.wav", [("tone", 2000)])

        result = verify_audio(audio_path, "Short enough.")
        assert "No issues found." in result


class TestFormatTimestamp:
    """Test millisecond-to-MM:SS formatting."""

    def test_formats_minutes_and_seconds(self):
        assert format_timestamp(754000) == "12:34"

    def test_pads_seconds(self):
        assert format_timestamp(4000) == "0:04"

    def test_formats_over_an_hour(self):
        assert format_timestamp(3723000) == "62:03"

    def test_shift_timestamps_adds_the_offset_to_each_time_in_its_own_form(self):
        """The forms Gemini wrote in the end-to-end test's report, shifted by 5:02."""
        text = ("At ~00:12, the number is omitted. **1:26 - Missing word:** dot. "
                "0:01 - 0:03: the heading. At 1:02:03 too.")
        assert shift_timestamps(text, 302000) == (
            "At ~5:14, the number is omitted. **6:28 - Missing word:** dot. "
            "5:03 - 5:05: the heading. At 67:05 too.")

    def test_shift_timestamps_leaves_text_quoted_from_the_article(self):
        """Review finding 1: the report quotes the article, and a time there is the
        article's, not the recording's ("John 3:16" became "John 13:16")."""
        text = ('At 1:26 the word "as John 3:16 puts it" is cut; \u201cthe 4:30 train\u201d '
                "and *a 0:03 pause* and `2:15` stay; **1:26 - Missing word:** shifts.")
        assert shift_timestamps(text, 600000) == (
            'At 11:26 the word "as John 3:16 puts it" is cut; \u201cthe 4:30 train\u201d '
            "and *a 0:03 pause* and `2:15` stay; **11:26 - Missing word:** shifts.")

    def test_shift_timestamps_leaves_clock_times_and_other_numbers(self):
        text = "Read as 10:30 a.m. and 9:45pm, a 3:2 ratio, 2.5:1, and v1:20b."
        assert shift_timestamps(text, 302000) == text


class TestPrecheckReport:
    """Test the deterministic pre-check that runs before any model call."""

    def test_reports_duration_word_count_and_pace(self):
        """Test the report includes duration, word count, and words per minute."""
        audio = make_segment([("tone", 4000)])
        text = "one two three four five six seven eight nine ten"

        report = precheck_report(audio, text)

        assert "0:04" in report
        assert "10 words" in report
        assert "150" in report  # 10 words / 4s = 150 WPM

    def test_break_markers_not_counted_as_words(self):
        """Test [BREAK] markers are excluded from the word count."""
        audio = make_segment([("tone", 4000)])
        text = "one two three four five\n[BREAK]\nsix seven eight nine ten"

        report = precheck_report(audio, text)

        assert "10 words" in report

    def test_warns_when_pace_is_implausibly_slow(self):
        """Test a WPM far below speech pace (suggesting missing text) warns."""
        audio = make_segment([("tone", 60000)])
        text = " ".join(["word"] * 10)  # 10 WPM

        report = precheck_report(audio, text)

        assert "WARNING" in report

    def test_no_warning_at_normal_speech_pace(self):
        """Test a normal reading pace produces no warning."""
        audio = make_segment([("tone", 60000)])
        text = " ".join(["word"] * 150)  # 150 WPM

        report = precheck_report(audio, text)

        assert "WARNING" not in report

    def test_warns_on_unexpectedly_long_silence(self):
        """Test a silence gap far longer than a [BREAK] pause warns with a timestamp."""
        audio = make_segment([("tone", 30000), ("silence", 6000), ("tone", 30000)])
        text = " ".join(["word"] * 150)  # pace in band; only silence should warn

        report = precheck_report(audio, text)

        assert "WARNING" in report
        assert "0:30" in report  # the silence starts 30s in

    def test_no_warning_for_normal_break_silence(self):
        """Test an ordinary ~3.2s [BREAK] gap does not trigger the silence warning."""
        audio = make_segment([("tone", 30000), ("silence", 3200), ("tone", 30000)])
        text = " ".join(["word"] * 150)

        report = precheck_report(audio, text)

        assert "WARNING" not in report


class TestFindSectionGaps:
    """Test detection of the silences at [BREAK] boundaries.

    In real Kokoro output, Kokoro's own trailing quiet at paragraph ends
    stretches every join: ordinary 300ms joins measure ~1.5-2.0s of
    silence, [BREAK] joins (2000ms inserted) measure ~3.2-3.4s. Only the
    latter mark section boundaries.
    """

    def test_finds_break_gap(self):
        """Test a [BREAK]-sized (~3.2s) silence is reported as one gap."""
        audio = make_segment([("tone", 1000), ("silence", 3200), ("tone", 1000)])

        gaps = find_section_gaps(audio)

        assert len(gaps) == 1
        start_ms, end_ms = gaps[0]
        assert start_ms == pytest.approx(1000, abs=200)
        assert end_ms == pytest.approx(4200, abs=200)

    def test_ignores_paragraph_sized_pauses(self):
        """Test the ~2s silences at ordinary paragraph joins are not gaps."""
        audio = make_segment([("tone", 1000), ("silence", 2000), ("tone", 1000)])

        assert find_section_gaps(audio) == []

    def test_ignores_short_intra_section_pauses(self):
        """Test the 300ms pauses between ordinary TTS chunks are not gaps."""
        audio = make_segment([("tone", 1000), ("silence", 300), ("tone", 1000)])

        assert find_section_gaps(audio) == []


class TestSplitAudioAtGaps:
    """Test cutting audio into section chunks at detected gaps."""

    def test_splits_at_gap_midpoint(self):
        """Test one gap yields two chunks cut at its midpoint, with offsets."""
        audio = make_segment([("tone", 1000), ("silence", 2000), ("tone", 1000)])

        chunks = split_audio_at_gaps(audio, [(1000, 3000)])

        assert len(chunks) == 2
        (first_start, first_seg), (second_start, second_seg) = chunks
        assert first_start == 0
        assert second_start == 2000  # midpoint of the gap
        assert len(first_seg) == 2000
        assert len(second_seg) == 2000
        assert len(first_seg) + len(second_seg) == len(audio)

    def test_no_gaps_returns_whole_audio(self):
        """Test that with no gaps the audio comes back as a single chunk."""
        audio = make_segment([("tone", 1000)])

        chunks = split_audio_at_gaps(audio, [])

        assert len(chunks) == 1
        assert chunks[0][0] == 0
        assert len(chunks[0][1]) == len(audio)


class TestCLI:
    """Test the command-line interface."""

    @patch("verify_audio.verify_audio")
    def test_cli_with_text_file(self, mock_verify, tmp_path, capsys):
        """Test CLI reads source text from a file."""
        mock_verify.return_value = "No issues found."
        audio_path = make_wav(tmp_path / "audio.wav")
        text_path = tmp_path / "source.txt"
        text_path.write_text("Hello world.")

        main([audio_path, str(text_path)])
        mock_verify.assert_called_once()
        args = mock_verify.call_args
        assert args[0][1] == "Hello world."
        captured = capsys.readouterr()
        assert "No issues found." in captured.out

    @patch("verify_audio.verify_audio")
    def test_cli_with_model_flag(self, mock_verify, tmp_path):
        """Test CLI passes --model flag to verify_audio."""
        mock_verify.return_value = "All good."
        audio_path = make_wav(tmp_path / "audio.wav")
        text_path = tmp_path / "source.txt"
        text_path.write_text("Some text.")

        main([audio_path, str(text_path), "--model", "gemini-2.5-pro"])
        call_args = mock_verify.call_args
        assert call_args.kwargs.get("model") == "gemini-2.5-pro" or call_args[0][2] == "gemini-2.5-pro"

    @patch("verify_audio.verify_audio")
    def test_cli_default_model_is_maintained_alias(self, mock_verify, tmp_path):
        """Test CLI defaults to the maintained gemini-flash-latest alias."""
        mock_verify.return_value = "All good."
        audio_path = make_wav(tmp_path / "audio.wav")
        text_path = tmp_path / "source.txt"
        text_path.write_text("Some text.")

        main([audio_path, str(text_path)])
        call_args = mock_verify.call_args
        assert call_args.kwargs.get("model") == "gemini-flash-latest"

    @patch("verify_audio.verify_audio")
    def test_cli_unavailable_model_exits_with_clear_error(self, mock_verify, tmp_path, capsys):
        """Test CLI reports a clear error (not a traceback) when the model 404s."""
        mock_verify.side_effect = Exception(
            "404 NOT_FOUND: models/gemini-nonexistent is not found"
        )
        audio_path = make_wav(tmp_path / "audio.wav")
        text_path = tmp_path / "source.txt"
        text_path.write_text("Some text.")

        with pytest.raises(SystemExit) as exc_info:
            main([audio_path, str(text_path), "--model", "gemini-nonexistent"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "gemini-nonexistent" in captured.err
        assert "not available" in captured.err

    @patch("verify_audio.verify_audio")
    def test_cli_over_threshold_audio_exits_with_clear_error(
        self, mock_verify, tmp_path, capsys
    ):
        """Test the CLI turns the too-long-for-one-call error into exit 1."""
        mock_verify.side_effect = ValueError(
            "audio is 28:00 long, but reliable single-call verification "
            "tops out at ~20 minutes"
        )
        audio_path = make_wav(tmp_path / "audio.wav")
        text_path = tmp_path / "source.txt"
        text_path.write_text("Some text.")

        with pytest.raises(SystemExit) as exc_info:
            main([audio_path, str(text_path)])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "28:00" in captured.err

    @patch("verify_audio.verify_audio")
    def test_cli_reraises_unrelated_errors(self, mock_verify, tmp_path):
        """Test CLI doesn't swallow errors that aren't about a missing model."""
        mock_verify.side_effect = Exception("connection reset by peer")
        audio_path = make_wav(tmp_path / "audio.wav")
        text_path = tmp_path / "source.txt"
        text_path.write_text("Some text.")

        with pytest.raises(Exception, match="connection reset"):
            main([audio_path, str(text_path)])

    def test_cli_missing_audio_file_exits(self, tmp_path):
        """Test CLI exits with error for missing audio file."""
        text_path = tmp_path / "source.txt"
        text_path.write_text("Some text.")

        with pytest.raises(SystemExit):
            main(["/nonexistent/audio.wav", str(text_path)])

    def test_cli_missing_text_file_exits(self, tmp_path):
        """Test CLI exits with error for missing text file."""
        audio_path = make_wav(tmp_path / "audio.wav")

        with pytest.raises(SystemExit):
            main([audio_path, "/nonexistent/text.txt"])


def test_precheck_warnings_lists_pace_and_long_silence():
    audio = make_segment([("tone", 1000), ("silence", 6000), ("tone", 1000)])
    text = " ".join(["word"] * 400)  # far too many words for 8 s of audio
    warnings = precheck_warnings(audio, text)
    assert any("WPM" in w for w in warnings)
    assert any("of silence at" in w for w in warnings)


def test_precheck_warnings_empty_for_normal_audio():
    audio = make_segment([("tone", 60000)])
    text = " ".join(["word"] * 160)  # 160 WPM
    assert precheck_warnings(audio, text) == []
