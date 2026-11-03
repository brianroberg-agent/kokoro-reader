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
    doesn't read), the authors."""
    names = meta["byline"] if "byline" in meta else meta.get("authors")
    if not isinstance(names, list):
        return []
    return [" ".join(n.split()) for n in names if isinstance(n, str) and n.strip()]


def spoken_title(meta: dict, fallback_title: str = "") -> str:
    """The line the reading opens with (Brian, 2026-11-03): the title and who wrote it,
    "Is There Life on Mars, Pennsylvania? by Vladimir Snurenco.", or the title alone
    when the page names no one. The metadata's title, else fallback_title (the
    record's, as the night names the episode); "" when there is neither."""
    raw = meta.get("title")
    title = " ".join((raw if isinstance(raw, str) and raw.strip() else fallback_title).split())
    if not title:
        return ""
    names = _names(meta)
    if not names:
        return title if title.endswith((".", "?", "!")) else f"{title}."
    if len(names) == 1:
        who = names[0]
    elif len(names) == 2:
        who = f"{names[0]} and {names[1]}"
    else:
        who = f"{', '.join(names[:-1])}, and {names[-1]}"
    return f"{title.removesuffix('.')} by {who}."


def with_spoken_title(markdown: str, meta: dict, fallback_title: str = "") -> str:
    """The article as the narration server gets it: the spoken title, then the text."""
    line = spoken_title(meta, fallback_title)
    return f"{line}\n\n{markdown}" if line else markdown
