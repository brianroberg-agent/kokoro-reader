"""Fetching: plain GETs, the article_assistant checkout, and the article's spoken opening."""

import json
import subprocess
from pathlib import Path

import requests


class FetchError(Exception):
    pass


def fetch_text(url: str) -> str:
    resp = requests.get(url, timeout=30)
    if resp.status_code != 200:
        raise FetchError(f"GET {url} returned {resp.status_code}")
    return resp.text


def _run(args: list[str], checkout: Path) -> str:
    cmd = ["uv", "run", "--frozen", "python", "article_assistant.py", *args]
    try:
        proc = subprocess.run(cmd, cwd=checkout, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as e:
        raise FetchError("uv not found") from e
    except subprocess.TimeoutExpired as e:
        raise FetchError(f"article_assistant timed out after {e.timeout}s") from e
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        raise FetchError(lines[-1] if lines else f"article_assistant exited {proc.returncode}")
    return proc.stdout


def parse_metadata(raw: str) -> dict:
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FetchError(f"metadata is not JSON: {e}") from e
    if not isinstance(meta, dict):
        raise FetchError("metadata is not a JSON object")
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        raise FetchError("metadata has an empty title")
    return meta


def run_article_assistant(url: str, checkout: Path) -> tuple[str, dict]:
    markdown = _run(["content", url, "--no-images"], checkout)
    meta = parse_metadata(_run(["metadata", "--json", url], checkout))
    return markdown, meta

def _names(meta: dict) -> list[str]:
    """Who wrote the piece, for the spoken opening: the byline article-assistant read
    under the title (`[]` when the page shows no one, as for an unsigned statement),
    or, with no byline key (a site without its own extractor, or a page in markup it
    doesn't read), the authors. A lone name is one author, as gate._meta reads it."""
    names = meta["byline"] if "byline" in meta else meta.get("authors")
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        return []
    return [" ".join(n.split()) for n in names if isinstance(n, str) and n.strip()]


def spoken_title(meta: dict, fallback_title: str = "") -> str:
    """The line the reading opens with (Brian, 2026-11-03): the title and who wrote it,
    "Where Did the Commons Go? by Ada Quill", or the title alone when the page names
    no one. The metadata's title, else fallback_title (the record's, as the night
    names the episode); "" when there is neither.

    It is a Markdown heading with no final period. narration.words reads a line that
    doesn't end as a sentence does as a heading, so the title's capitalised words don't
    count as the article's names (which loosen its respelling rule); and a heading's
    leading number ("10. Things We Lost") isn't taken for a list marker. A title alone
    that ends in "?" or "!" still ends as a sentence."""
    raw = meta.get("title")
    title = " ".join((raw if isinstance(raw, str) and raw.strip() else fallback_title).split())
    if not title:
        return ""
    if title.endswith(".") and "." not in title.rsplit(" ", 1)[-1][:-1]:
        title = title[:-1]  # "Notes and Briefs." loses its period; "the U.S." keeps it
    names = _names(meta)
    if not names:
        return f"# {title}"
    return f"# {title} by {_joined(names)}"


def _joined(names: list[str]) -> str:
    """"Ada Quill", "Ada Quill and Tom Brandvold", "Ada Quill, Tom Brandvold, and Mara Odell"."""
    if len(names) <= 2:
        return " and ".join(names)
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def with_spoken_title(markdown: str, meta: dict, fallback_title: str = "") -> str:
    """The article as the narration server gets it: the spoken title, then the text."""
    line = spoken_title(meta, fallback_title)
    return f"{line}\n\n{markdown}" if line else markdown


def _text(value) -> str:
    """A metadata value as one line of text: a string with its spaces collapsed, a number
    as written, else ""."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return " ".join(value.split()) if isinstance(value, str) else ""


def episode_details(meta: dict) -> str:
    """The lines an episode's description opens with (Brian, 2026-11-10): who wrote it
    ("By Ada Quill", the same names the reading opens with), then the edition and, when
    the page gives them, its pages in print ("No. 86 (Fall 2026), pp. 24–41"). A line with
    nothing to show is left out; "" when there is none. Pages come only with an edition."""
    lines = []
    names = _names(meta)
    if names:
        lines.append(f"By {_joined(names)}")
    number, season = _text(meta.get("issue_number")), _text(meta.get("issue_season"))
    if number and season:
        pages = _text(meta.get("pages"))
        lines.append(f"No. {number} ({season})" + (f", {pages}" if pages else ""))
    return "\n".join(lines)


def podcast_metadata(meta: dict, podcast: str) -> dict:
    """The metadata a work item is opened with, which the Mac also writes into the MP3's
    tags (title; artist from authors; album from publication and edition). Audiobookshelf
    builds a podcast's author from its episodes' artist tag, refilling it at each scan
    when empty, and an empty podcast's title from its first episode's album tag (both
    seen on 2.37.2, 2026-11-03). Brian: "There shouldn't be an author byline on the
    entire podcast." So no authors (no artist tag), and the podcast's name as the
    publication with no edition (the album is the podcast's name).

    The description, which no tag carries and the Mac puts first in the published notes,
    opens with episode_details: the authors, the edition and the pages reach the episode
    there."""
    out = {**meta, "authors": [], "publication": podcast, "issue_number": "", "issue_season": ""}
    details = episode_details(meta)
    if details:
        summary = meta.get("description")
        summary = summary.strip() if isinstance(summary, str) else ""
        out["description"] = f"{details}\n{summary}" if summary else details
    return out
