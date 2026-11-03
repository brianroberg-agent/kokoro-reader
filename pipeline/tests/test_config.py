from datetime import date
from pathlib import Path

import pytest

from narration_pipeline.config import Settings, load_settings, load_sources

SOURCES_TOML = Path(__file__).parent.parent / "sources.toml"


def test_loads_the_spec_entry():
    sources = load_sources(SOURCES_TOML.read_text())
    assert len(sources) == 1
    s = sources[0]
    assert s.id == "new-atlantis"
    assert s.podcast == "The New Atlantis"
    assert s.feed == "https://www.thenewatlantis.com/feed"
    assert s.listing == "new-atlantis-issues"
    assert s.start_edition == 86
    assert s.start_date == date(2026, 9, 8)
    assert s.text_rules == ()


def test_unknown_key_is_refused():
    text = """
[[source]]
id = "x"
name = "X"
podcast = "X"
feed = "https://example.com/feed"
extractr = "x"
start = { edition = 1, date = "2026-01-01" }
"""
    with pytest.raises(ValueError, match="extractr"):
        load_sources(text)


def test_settings_defaults():
    assert load_settings("[settings]\n") == Settings()
    assert load_settings("") == Settings()


def test_settings_override_parses_times():
    from datetime import time

    s = load_settings('[settings]\nstart_at = "02:30"\ncap = 2\n')
    assert s.start_at == time(2, 30)
    assert s.cap == 2
    assert s.summary_at == Settings().summary_at


def test_unknown_setting_is_refused():
    with pytest.raises(ValueError, match="capp"):
        load_settings("[settings]\ncapp = 1\n")
