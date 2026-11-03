"""Fetching: plain GETs and the article_assistant checkout."""

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
