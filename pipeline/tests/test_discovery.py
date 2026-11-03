from datetime import date
from pathlib import Path

import pytest

from narration_pipeline.config import Source
from narration_pipeline.discovery import (
    LISTINGS,
    Candidate,
    DiscoveryError,
    candidates,
    discover,
    in_scope,
    parse_feed,
    parse_issue_index,
)

FIX = Path(__file__).parent / "fixtures"
FEED = (FIX / "new_atlantis_feed.xml").read_text()
ISSUES = (FIX / "new_atlantis_issues.html").read_text()
BASE = "https://www.thenewatlantis.com/publications/"
AGI = "Are We Really on the Path to AGI?"

SOURCE = Source(
    id="new-atlantis",
    name="The New Atlantis",
    podcast="The New Atlantis",
    feed="https://www.thenewatlantis.com/feed",
    listing="new-atlantis-issues",
    extractor="new-atlantis",
    start_edition=86,
    start_date=date(2026, 9, 8),
    text_rules=(),
)


def test_feed_items():
    items = parse_feed(FEED)
    by_title = {i.title: i for i in items}
    assert by_title[AGI].published == date(2026, 10, 7)
    assert by_title[AGI].url.startswith("https://www.thenewatlantis.com/")
    assert len(items) == 10


def test_index_maps_articles_to_issues():
    index = parse_issue_index(ISSUES)
    assert index[BASE + "zoning-alone"][:2] == (86, "Fall 2026")
    assert index[BASE + "zoning-alone"][2] == "Zoning Alone"
    assert index[BASE + "losing-liberty-in-an-age-of-access"][:2] == (43, "Summer/Fall 2014")


@pytest.mark.parametrize(
    "html", ["<html>nothing here</html>", "<script>const TNA_ISSUES = [{oops;const TNA_INITIAL_SLUG = 'x'</script>"]
)
def test_index_without_tna_issues_raises(html):
    with pytest.raises(DiscoveryError):
        parse_issue_index(html)


def test_discovery_error_str():
    assert str(DiscoveryError("new-atlantis", "feed down")) == "new-atlantis: feed down"


def test_web_only_piece_has_no_edition():
    cands = candidates(parse_feed(FEED), parse_issue_index(ISSUES), SOURCE)
    agi = next(c for c in cands if c.title == AGI)
    assert agi.edition is None
    assert agi.published == date(2026, 10, 7)


def test_feed_item_in_index_gets_edition():
    cands = candidates(parse_feed(FEED), parse_issue_index(ISSUES), SOURCE)
    zoning = next(c for c in cands if c.url == BASE + "zoning-alone")
    assert (zoning.edition, zoning.season) == (86, "Fall 2026")
    assert [c.url for c in cands].count(zoning.url) == 1


def test_index_articles_below_start_are_not_candidates():
    feed = parse_feed(FEED)
    index = parse_issue_index(ISSUES)
    cands = candidates(feed, index, SOURCE)
    feed_urls = {i.url for i in feed}
    below = {u for u, (n, *_) in index.items() if n < 86}
    assert below  # fixture has issue 85 and 43 articles
    assert not [c for c in cands if c.url in below and c.url not in feed_urls]


def test_index_only_articles_at_or_above_start_are_candidates():
    index = {BASE + "only-in-index": (86, "Fall 2026", "Real Title"), BASE + "old": (85, "Summer 2026", "Old")}
    cands = candidates([], index, SOURCE)
    assert [(c.url, c.title, c.edition, c.season, c.published) for c in cands] == [
        (BASE + "only-in-index", "Real Title", 86, "Fall 2026", None)
    ]


def _c(edition, published):
    return Candidate("u", "t", published, edition, None)


@pytest.mark.parametrize(
    "edition, published, expected",
    [
        (86, None, True),
        (85, None, False),
        (None, date(2026, 9, 8), True),
        (None, date(2026, 9, 7), False),
        (None, None, False),
    ],
)
def test_scope(edition, published, expected):
    assert in_scope(_c(edition, published), SOURCE) is expected


def test_listings_registry():
    url, parser = LISTINGS["new-atlantis-issues"]
    assert url == "https://www.thenewatlantis.com/issues"
    assert parser is parse_issue_index


def test_discover_fetches_feed_and_listing():
    pages = {SOURCE.feed: FEED, LISTINGS["new-atlantis-issues"][0]: ISSUES}
    cands = discover(SOURCE, pages.__getitem__)
    assert any(c.title == AGI for c in cands)
    assert any(c.url == BASE + "zoning-alone" for c in cands)


def test_discover_without_listing_uses_feed_only():
    src = Source(**{**SOURCE.__dict__, "listing": None})
    cands = discover(src, {src.feed: FEED}.__getitem__)
    assert len(cands) == 10


def test_discover_fetch_error_becomes_discovery_error():
    def boom(url):
        raise OSError("down")

    with pytest.raises(DiscoveryError, match="new-atlantis"):
        discover(SOURCE, boom)


def test_discover_bad_feed_becomes_discovery_error():
    with pytest.raises(DiscoveryError, match="new-atlantis"):
        discover(SOURCE, lambda url: "not xml")


def test_discover_unknown_listing_becomes_discovery_error():
    src = Source(**{**SOURCE.__dict__, "listing": "nope"})
    with pytest.raises(DiscoveryError, match="nope"):
        discover(src, {src.feed: FEED}.__getitem__)


def test_feed_title_wins_over_index_title():
    feed = parse_feed(FEED)
    index = parse_issue_index(ISSUES)
    z = next(i for i in feed if i.url == BASE + "zoning-alone")
    index[z.url] = (86, "Fall 2026", "Different")
    cands = candidates(feed, index, SOURCE)
    assert next(c for c in cands if c.url == z.url).title == z.title


EMPTY_FEED = '<rss version="2.0"><channel><title>x</title></channel></rss>'
NO_LINK_FEED = (
    '<rss version="2.0"><channel><item><title>t</title>'
    "<pubDate>Wed, 07 Oct 2026 14:11:52 +0000</pubDate></item></channel></rss>"
)


def test_feed_without_items_raises():
    with pytest.raises(DiscoveryError):
        parse_feed(EMPTY_FEED)
    with pytest.raises(DiscoveryError, match="new-atlantis"):
        discover(SOURCE, lambda url: EMPTY_FEED)


def test_feed_item_without_link_raises():
    with pytest.raises(DiscoveryError):
        parse_feed(NO_LINK_FEED)


def _page(array):
    return f"<script>const TNA_ISSUES = {array};const TNA_INITIAL_SLUG = \"x\"</script>"


def test_empty_listing_raises_when_configured():
    with pytest.raises(DiscoveryError):
        parse_issue_index(_page("[]"))
    pages = {SOURCE.feed: FEED, LISTINGS["new-atlantis-issues"][0]: _page("[]")}
    with pytest.raises(DiscoveryError, match="new-atlantis"):
        discover(SOURCE, pages.__getitem__)


@pytest.mark.parametrize("number, name", [("86", "Fall 2026"), (86, None), (True, "Fall 2026")])
def test_listing_with_wrong_types_raises(number, name):
    import json

    issue = {"number": number, "name": name, "sections": [{"articles": [{"url": "u", "title": "t"}]}]}
    with pytest.raises(DiscoveryError):
        parse_issue_index(_page(json.dumps([issue])))
