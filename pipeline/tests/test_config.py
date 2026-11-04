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
    assert s.cover == Path("covers/new-atlantis.png")


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


# The session service refuses a request outside its limits (session_service.validate_request),
# so a setting outside them would fail every article: it stops the start instead.

@pytest.mark.parametrize("value", ["59", "7201", "600.0", "true", '"600"'])
def test_session_timeout_outside_the_services_limits_is_refused(value):
    with pytest.raises(ValueError, match="session_timeout_s"):
        load_settings(f"[settings]\nsession_timeout_s = {value}\n")


@pytest.mark.parametrize("value", ["60", "7200", "5400"])
def test_session_timeout_inside_the_limits_loads(value):
    assert load_settings(f"[settings]\nsession_timeout_s = {value}\n").session_timeout_s == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "5.01", "nan", "inf", "true", '"2"'])
def test_session_budget_outside_the_services_limits_is_refused(value):
    with pytest.raises(ValueError, match="session_budget_usd"):
        load_settings(f"[settings]\nsession_budget_usd = {value}\n")


@pytest.mark.parametrize("value", ["5.00", "5", "0.01"])
def test_session_budget_inside_the_limits_loads(value):
    assert load_settings(f"[settings]\nsession_budget_usd = {value}\n").session_budget_usd == float(value)


# Finding 5 (end-to-end test, 2026-11-03): cap 0 stops every new article without a word,
# and a negative cap slices the picked list from the end (picked[:-1] takes all but one).
@pytest.mark.parametrize("value", ["0", "-1", "21", "5.0", "true", '"5"'])
def test_cap_outside_1_to_20_is_refused(value):
    with pytest.raises(ValueError, match="cap"):
        load_settings(f"[settings]\ncap = {value}\n")


@pytest.mark.parametrize("value", ["1", "5", "20"])
def test_cap_inside_1_to_20_loads(value):
    assert load_settings(f"[settings]\ncap = {value}\n").cap == int(value)


def source_toml(rules: str) -> str:
    return f"""
[[source]]
id = "x"
name = "X"
podcast = "X"
feed = "https://example.com/feed"
extractor = "new-atlantis"
cover = "covers/x.png"
start = {{ edition = 1, date = "2026-01-01" }}
text_rules = {rules}
"""


@pytest.mark.parametrize("rules", [
    "[" + ", ".join(['"a rule"'] * 21) + "]",  # more than 20
    '["' + "x" * 301 + '"]',                    # one longer than 300 characters
    "[5]", '"a rule"',
])
def test_text_rules_outside_the_services_limits_are_refused(rules):
    with pytest.raises(ValueError, match="text_rules"):
        load_sources(source_toml(rules))


def test_text_rules_at_the_limits_load():
    rules = "[" + ", ".join(['"' + "x" * 300 + '"'] * 20) + "]"
    [s] = load_sources(source_toml(rules))
    assert len(s.text_rules) == 20


def test_the_limits_are_the_services():
    from narration_pipeline import session_service as ss
    for name, limit in (("session_timeout_s", ss.MAX_TIMEOUT_S + 1), ("session_timeout_s", ss.MIN_TIMEOUT_S - 1),
                        ("session_budget_usd", ss.MAX_BUDGET_USD + 0.01)):
        with pytest.raises(ValueError, match=name):
            load_settings(f"[settings]\n{name} = {limit}\n")


def test_an_unknown_extractor_is_refused():
    # article-assistant picks its extractor from the URL; the key documents the source,
    # so a name nothing knows is a mistake in the entry
    with pytest.raises(ValueError, match="extractor"):
        load_sources(source_toml("[]").replace('extractor = "new-atlantis"', 'extractor = "generic"'))


def test_the_known_extractors():
    from narration_pipeline.config import EXTRACTORS
    assert EXTRACTORS == frozenset({"new-atlantis"})
    assert {s.extractor for s in load_sources(SOURCES_TOML.read_text())} <= EXTRACTORS



# A cover for every podcast the pipeline publishes to (Brian, 2026-11-03: "When a new
# podcast is created in Audiobookshelf, a graphic should be created for it as well").
def test_a_source_without_a_cover_is_refused():
    with pytest.raises(ValueError, match="missing source key.*cover"):
        load_sources(source_toml("[]").replace('cover = "covers/x.png"\n', ""))


def test_a_cover_is_found_beside_the_config_and_must_exist(tmp_path):
    (tmp_path / "covers").mkdir()
    (tmp_path / "covers" / "x.png").write_bytes(b"png")
    [s] = load_sources(source_toml("[]"), tmp_path)
    assert s.cover == tmp_path / "covers" / "x.png"
    with pytest.raises(ValueError, match="x: cover .*nope.png not found"):
        load_sources(source_toml("[]").replace("covers/x.png", "covers/nope.png"), tmp_path)


def test_an_absolute_cover_path_is_kept(tmp_path):
    cover = tmp_path / "elsewhere.png"
    cover.write_bytes(b"png")
    [s] = load_sources(source_toml("[]").replace("covers/x.png", str(cover)), Path("/no/such/dir"))
    assert s.cover == cover


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR", path
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def test_every_shipped_cover_is_a_square_png_of_at_least_1400_px():
    # 1400 to 3000 px square is what podcast apps ask for.
    for s in load_sources(SOURCES_TOML.read_text(), SOURCES_TOML.parent):
        width, height = png_size(s.cover)
        assert width == height and 1400 <= width <= 3000, (s.id, width, height)


# The podcast is created at <folder>/<podcast>, and the upload goes to a folder named
# from the same name after Audiobookshelf drops what a file name can't hold: a name it
# would change would split the podcast in two.
@pytest.mark.parametrize("name", ["A: B", "A/B", "A?", 'A \\"B\\"', "A*", "A|B", "A<B>", "A\\\\B", " A", "A ", "A.", ""])
def test_a_podcast_name_a_folder_name_would_change_is_refused(name):
    with pytest.raises(ValueError, match="podcast"):
        load_sources(source_toml("[]").replace('podcast = "X"', f'podcast = "{name}"'))


@pytest.mark.parametrize("name", ["The New Atlantis", "Notes & Briefs", "St. Mary's Review", "Vol. 2, No. 3"])
def test_a_podcast_name_a_folder_can_hold_loads(name):
    [s] = load_sources(source_toml("[]").replace('podcast = "X"', f'podcast = "{name}"'))
    assert s.podcast == name


@pytest.mark.parametrize("name", ["The  New Atlantis", "A\\u00a0B", "A\\tB", "A\\u0085B", "CON", "nul", "Com1",
                                  "LPT9", "A" * 201])
def test_a_podcast_name_a_folder_name_would_change_in_other_ways_is_refused(name):
    # Review finding 3: Audiobookshelf's folder names also collapse runs of spaces, drop
    # C1 control characters and Windows' reserved names, and cut long names.
    with pytest.raises(ValueError, match="podcast"):
        load_sources(source_toml("[]").replace('podcast = "X"', f'podcast = "{name}"'))
