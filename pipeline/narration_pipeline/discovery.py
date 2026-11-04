"""Find candidate articles from a source's feed and issue listing, and apply the scope rule."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

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


def canonical_url(url: str) -> str:
    """One spelling per article: https, the host lower-cased, no default port, no
    trailing slash, no query or fragment (R35). The feed and the index can spell one
    article two ways, and the record keys rows by URL, so two spellings would be two
    rows, each recorded and published. ValueError for an unreadable port."""
    parts = urlsplit(url.strip())
    port = parts.port
    host = (parts.hostname or "") + ("" if port in (None, 80, 443) else f":{port}")
    return urlunsplit(("https", host, parts.path.rstrip("/"), "", ""))


def parse_feed(xml: str) -> list[FeedItem]:
    root = ElementTree.fromstring(xml)
    items = []
    for item in root.iter("item"):
        url = item.findtext("link", "").strip()
        if not url:
            raise DiscoveryError("feed", "an item has no link")
        items.append(
            FeedItem(
                url=url,
                title=item.findtext("title", "").strip(),
                published=parsedate_to_datetime(item.findtext("pubDate", "")).date(),
            )
        )
    if not items:
        raise DiscoveryError("feed", "no items in the feed")
    return items


_ISSUES_RE = re.compile(r"TNA_ISSUES\s*=\s*(\[.*?\]);const TNA_INITIAL_SLUG", re.S)


def parse_issue_index(html: str) -> dict[str, tuple[int, str, str]]:
    match = _ISSUES_RE.search(html)
    if not match:
        raise DiscoveryError("listing", "no TNA_ISSUES array in the page")
    try:
        index = {}
        for issue in json.loads(match.group(1)):
            number, name = issue["number"], issue["name"]
            if type(number) is not int or not isinstance(name, str):
                raise TypeError(f"issue number/name has the wrong type: {number!r}, {name!r}")
            for section in issue["sections"]:
                for article in section["articles"]:
                    title = article["title"]
                    if not isinstance(title, str):
                        raise TypeError(f"article title is not a string: {title!r}")
                    index[article["url"]] = (number, name, title)
    except (ValueError, KeyError, TypeError) as e:
        raise DiscoveryError("listing", f"unreadable TNA_ISSUES: {e!r}") from e
    if not index:
        raise DiscoveryError("listing", "TNA_ISSUES holds no articles")
    return index


def candidates(
    feed: list[FeedItem], index: dict[str, tuple[int, str, str]], source: Source
) -> list[Candidate]:
    out: dict[str, Candidate] = {}
    index = {canonical_url(url): entry for url, entry in index.items()}
    for item in feed:
        url = canonical_url(item.url)
        edition, season = index.get(url, (None, None, None))[:2]
        out[url] = Candidate(url, item.title, item.published, edition, season)
    for url, (edition, season, title) in index.items():
        if edition >= source.start_edition and url not in out:
            # The index carries no publication dates.
            out[url] = Candidate(url, title, None, edition, season)
    return list(out.values())


def in_scope(c: Candidate, source: Source) -> bool:
    if c.edition is not None:
        return c.edition >= source.start_edition
    return c.published is not None and c.published >= source.start_date


LISTINGS: dict[str, tuple[str, Callable[[str], dict[str, tuple[int, str, str]]]]] = {
    "new-atlantis-issues": ("https://www.thenewatlantis.com/issues", parse_issue_index),
}


def discover(source: Source, fetch_text: Callable[[str], str]) -> list[Candidate]:
    try:
        feed = parse_feed(fetch_text(source.feed))
        index: dict[str, tuple[int, str, str]] = {}
        if source.listing:
            url, parser = LISTINGS[source.listing]
            index = parser(fetch_text(url))
        return candidates(feed, index, source)
    except DiscoveryError as e:
        raise DiscoveryError(source.id, e.reason) from e
    except KeyError as e:
        raise DiscoveryError(source.id, f"unknown listing {e}") from e
    except Exception as e:
        raise DiscoveryError(source.id, f"{type(e).__name__}: {e}") from e
