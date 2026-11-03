"""Publish finished MP3s as episodes in an Audiobookshelf podcast.

The endpoint paths and JSON field names below were written from the Audiobookshelf
API docs and verified against Audiobookshelf 2.37.2 (2026-11-02). Each one is a
module-level constant or a one-line function, so a correction is a one-line change.
"""
from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path

import requests

# --- endpoints and field names (verified against Audiobookshelf 2.37.2, 2026-11-02) ---------------------------------
LIBRARIES_PATH = "/api/libraries"
UPLOAD_PATH = "/api/upload"


def items_path(library_id: str) -> str:
    return f"/api/libraries/{library_id}/items"


def item_path(item_id: str) -> str:
    return f"/api/items/{item_id}"


def episode_path(item_id: str, episode_id: str) -> str:
    return f"/api/podcasts/{item_id}/episode/{episode_id}"


def podcast_libraries(body: dict) -> list[dict]:
    return [lib for lib in body.get("libraries", []) if lib.get("mediaType") == "podcast"]


def item_title(item: dict) -> str | None:
    return ((item.get("media") or {}).get("metadata") or {}).get("title")


def item_episodes(item: dict) -> list[dict]:
    return (item.get("media") or {}).get("episodes") or []


def episode_file_name(episode: dict) -> str | None:
    return (((episode.get("audioFile") or {}).get("metadata")) or {}).get("filename")


def details_body(title: str, description: str, pub_date: str | None) -> dict:
    body = {"title": title, "description": description}
    if pub_date is not None:
        body["pubDate"] = pub_date
    return body


# --- pure helpers ------------------------------------------------------------
class AbsError(Exception):
    pass


def episode_filename(title: str, item_id: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    slug = slug[:60].rstrip("-") or "episode"
    return f"{slug}-{item_id}.mp3"


# --- client ------------------------------------------------------------------
class Audiobookshelf:
    def __init__(self, base_url: str, api_key: str, session=None,
                 sleep=time.sleep, clock=time.monotonic):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock

    def _call(self, method: str, path: str, empty_ok: bool = False, **kw) -> dict:
        try:
            resp = self._session.request(method, self.base_url + path,
                                         headers=self._headers, timeout=300, **kw)
        except requests.RequestException as e:
            raise AbsError(f"{method} {path} failed: {type(e).__name__}") from None
        if resp.status_code != 200:
            raise AbsError(f"{method} {path} returned {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            return body
        if empty_ok:
            return {}
        raise AbsError(f"{method} {path} returned a body that is not a JSON object")

    def _libraries(self) -> list[dict]:
        return podcast_libraries(self._call("GET", LIBRARIES_PATH))

    def _locate(self, podcast: str, filename: str) -> tuple[str, str] | None:
        """(item_id, episode_id) of the episode with this file name, or None."""
        for lib in self._libraries():
            results = self._call("GET", items_path(lib["id"]), params={"limit": 0}).get("results", [])
            for it in results:
                if item_title(it) != podcast:
                    continue
                full = self._call("GET", item_path(it["id"]), params={"expanded": 1})
                for ep in item_episodes(full):
                    if episode_file_name(ep) == filename:
                        return it["id"], ep["id"]
        return None

    def find_episode(self, podcast: str, filename: str) -> str | None:
        found = self._locate(podcast, filename)
        return found[1] if found else None

    def _library_for_upload(self, podcast: str) -> dict:
        libs = self._libraries()
        if not libs:
            raise AbsError("no podcast library on the server")
        for lib in libs:
            results = self._call("GET", items_path(lib["id"]), params={"limit": 0}).get("results", [])
            if any(item_title(it) == podcast for it in results):
                return lib
        return libs[0]

    def upload(self, podcast: str, mp3: Path, filename: str) -> None:
        lib = self._library_for_upload(podcast)
        if not lib.get("folders"):
            raise AbsError(f"library {lib['id']} has no folders")
        data = {"title": podcast, "library": lib["id"], "folder": lib["folders"][0]["id"]}
        with open(mp3, "rb") as f:
            self._call("POST", UPLOAD_PATH, empty_ok=True, data=data,
                       files={"0": (filename, f, "audio/mpeg")})

    def _wait_locate(self, podcast, filename, timeout_s, poll_s) -> tuple[str, str]:
        deadline = self._clock() + timeout_s
        while True:
            found = self._locate(podcast, filename)
            if found:
                return found
            if self._clock() >= deadline:
                raise AbsError(
                    f"episode {filename} did not appear in {podcast} within {timeout_s}s")
            self._sleep(poll_s)

    def wait_episode(self, podcast: str, filename: str,
                     timeout_s: float = 600, poll_s: float = 15) -> str:
        return self._wait_locate(podcast, filename, timeout_s, poll_s)[1]

    def set_episode(self, item_id: str, episode_id: str, title: str,
                    description: str, pub_date: str | None) -> None:
        self._call("PATCH", episode_path(item_id, episode_id), empty_ok=True,
                   json=details_body(title, description, pub_date))

    def publish(self, podcast: str, mp3: Path, filename: str, title: str,
                description: str, pub_date: str | None,
                wait_timeout_s: float = 600, poll_s: float = 15) -> str:
        found = self._locate(podcast, filename)
        if found is None:
            self.upload(podcast, mp3, filename)
            found = self._wait_locate(podcast, filename, wait_timeout_s, poll_s)
        item_id, episode_id = found
        self.set_episode(item_id, episode_id, title, description, pub_date)
        return episode_id


CHECK_FILENAME = "abs-check.mp3"


def abs_check(abs: Audiobookshelf, podcast: str, mp3: Path) -> str:
    """Live-check helper: upload a short MP3, wait for it, set a test description."""
    return abs.publish(podcast, mp3, CHECK_FILENAME, "abs-check test episode",
                       "Test episode from abs-check. Safe to delete.", None)
