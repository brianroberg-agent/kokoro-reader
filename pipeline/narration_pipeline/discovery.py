"""Find candidate articles from a source's feed and issue listing, and apply the scope rule."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime

from defusedxml import ElementTree

from narration_pipeline.config import Source


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str
    published: date | None
    edition: int | None
    season: str | None


@dataclass(frozen=True)
class FeedItem:
    url: str
    title: str
    published: date


class DiscoveryError(Exception):
    def __init__(self, source_id: str, reason: str):
        super().__init__(f"{source_id}: {reason}")
        self.source_id = source_id
        self.reason = reason


def parse_feed(xml: str) -> list[FeedItem]:
    root = ElementTree.fromstring(xml)
    items = []
    for item in root.iter("item"):
        items.append(
            FeedItem(
                url=item.findtext("link", "").strip(),
                title=item.findtext("title", "").strip(),
                published=parsedate_to_datetime(item.findtext("pubDate", "")).date(),
            )
        )
    return items


_ISSUES_RE = re.compile(r"TNA_ISSUES\s*=\s*(\[.*?\]);const TNA_INITIAL_SLUG", re.S)


def parse_issue_index(html: str) -> dict[str, tuple[int, str]]:
    match = _ISSUES_RE.search(html)
    if not match:
        raise DiscoveryError("listing", "no TNA_ISSUES array in the page")
    try:
        issues = json.loads(match.group(1))
        return {
            article["url"]: (issue["number"], issue["name"])
            for issue in issues
            for section in issue["sections"]
            for article in section["articles"]
        }
    except (ValueError, KeyError, TypeError) as e:
        raise DiscoveryError("listing", f"unreadable TNA_ISSUES: {e!r}") from e


def candidates(
    feed: list[FeedItem], index: dict[str, tuple[int, str]], source: Source
) -> list[Candidate]:
    out: dict[str, Candidate] = {}
    for item in feed:
        edition, season = index.get(item.url, (None, None))
        out[item.url] = Candidate(item.url, item.title, item.published, edition, season)
    for url, (edition, season) in index.items():
        if edition >= source.start_edition and url not in out:
            # The index carries no titles or dates; the URL slug stands in for the title.
            out[url] = Candidate(url, url.rstrip("/").rsplit("/", 1)[-1], None, edition, season)
    return list(out.values())


def in_scope(c: Candidate, source: Source) -> bool:
    if c.edition is not None:
        return c.edition >= source.start_edition
    return c.published is not None and c.published >= source.start_date


LISTINGS: dict[str, tuple[str, Callable[[str], dict]]] = {
    "new-atlantis-issues": ("https://www.thenewatlantis.com/issues", parse_issue_index),
}


def discover(source: Source, fetch_text: Callable[[str], str]) -> list[Candidate]:
    try:
        feed = parse_feed(fetch_text(source.feed))
        index: dict[str, tuple[int, str]] = {}
        if source.listing:
            url, parser = LISTINGS[source.listing]
            index = parser(fetch_text(url))
    except DiscoveryError as e:
        raise DiscoveryError(source.id, e.reason) from e
    except KeyError as e:
        raise DiscoveryError(source.id, f"unknown listing {e}") from e
    except Exception as e:
        raise DiscoveryError(source.id, f"{type(e).__name__}: {e}") from e
    return candidates(feed, index, source)
