"""Publish finished MP3s as episodes in an Audiobookshelf podcast.

The endpoint paths and JSON field names below were written from the Audiobookshelf
API docs and verified against Audiobookshelf 2.37.2 (2026-11-02). Each one is a
module-level constant or a one-line function, so a correction is a one-line change.
"""
from __future__ import annotations

import logging
import mimetypes
import re
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import requests

log = logging.getLogger(__name__)

# --- endpoints and field names (verified against Audiobookshelf 2.37.2, 2026-11-02) ---------------------------------
LIBRARIES_PATH = "/api/libraries"
UPLOAD_PATH = "/api/upload"
PODCASTS_PATH = "/api/podcasts"  # POST creates a podcast item (verified against 2.37.2, 2026-11-03)


def items_path(library_id: str) -> str:
    return f"/api/libraries/{library_id}/items"


def item_path(item_id: str) -> str:
    return f"/api/items/{item_id}"


def media_path(item_id: str) -> str:
    """PATCH {"metadata": {...}} updates the item's media (verified against 2.37.2, 2026-11-03)."""
    return f"/api/items/{item_id}/media"


def cover_path(item_id: str) -> str:
    """POST, multipart with the image as field "cover" (verified against 2.37.2, 2026-11-03)."""
    return f"/api/items/{item_id}/cover"


def item_cover(item: dict) -> str | None:
    """The podcast item's cover path on the server, or None when it has none."""
    return (item.get("media") or {}).get("coverPath") or None


def episode_path(item_id: str, episode_id: str) -> str:
    return f"/api/podcasts/{item_id}/episode/{episode_id}"


def podcast_libraries(body: dict) -> list[dict]:
    return [lib for lib in body.get("libraries", []) if lib.get("mediaType") == "podcast"]


def item_title(item: dict) -> str | None:
    return ((item.get("media") or {}).get("metadata") or {}).get("title")


def is_podcast(item: dict, podcast: str) -> bool:
    """A library item is the podcast when it has its title, or, since the scanner can
    retitle it, when its folder has the podcast's name (create_podcast makes it so)."""
    return item_title(item) == podcast or PurePosixPath(item.get("path") or "").name == podcast


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


class AbsTimeout(AbsError):
    """The episode did not appear within the wait. Any other AbsError is a failure."""


def episode_filename(title: str, item_id: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    slug = slug[:60].rstrip("-") or "episode"
    return f"{slug}-{item_id}.mp3"


def _named(filename: str) -> Callable[[str | None], bool]:
    return lambda name: name == filename


def _uploaded_for(item_id: str) -> Callable[[str | None], bool]:
    """A file name episode_filename gives this work item, whatever the title: the title
    comes from the Mac, which may no longer answer."""
    suffix = f"-{item_id}.mp3"
    return lambda name: isinstance(name, str) and name.endswith(suffix)


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

    def _podcast_items(self, podcast: str) -> list[str]:
        """The ids of the library items that are `podcast` (normally one), in library order."""
        ids = []
        for lib in self._libraries():
            results = self._call("GET", items_path(lib["id"]), params={"limit": 0}).get("results", [])
            ids += [it["id"] for it in results if is_podcast(it, podcast)]
        return ids

    def _episode_in(self, item_id: str, wanted: Callable[[str | None], bool]) -> str | None:
        """The id of the item's episode whose file name is wanted, or None: one GET."""
        full = self._call("GET", item_path(item_id), params={"expanded": 1})
        return next((ep["id"] for ep in item_episodes(full) if wanted(episode_file_name(ep))), None)

    def _in_items(self, items: list[str], wanted) -> tuple[str, str] | None:
        for item_id in items:
            episode_id = self._episode_in(item_id, wanted)
            if episode_id is not None:
                return item_id, episode_id
        return None

    def _locate(self, podcast: str, wanted) -> tuple[str, str] | None:
        """(item_id, episode_id) of the episode whose file name is wanted, or None."""
        return self._in_items(self._podcast_items(podcast), wanted)

    def find_episode(self, podcast: str, filename: str) -> str | None:
        found = self._locate(podcast, _named(filename))
        return found[1] if found else None

    def find_upload(self, podcast: str, item_id: str) -> tuple[str, str] | None:
        """(item_id, episode_id) of the episode uploaded for this work item, under
        whatever title, or None."""
        return self._locate(podcast, _uploaded_for(item_id))

    def _library_for_upload(self, podcast: str) -> dict:
        libs = self._libraries()
        if not libs:
            raise AbsError("no podcast library on the server")
        for lib in libs:
            results = self._call("GET", items_path(lib["id"]), params={"limit": 0}).get("results", [])
            if any(is_podcast(it, podcast) for it in results):
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

    def _wait_locate(self, podcast, wanted, what, timeout_s, poll_s) -> tuple[str, str]:
        """Poll for the episode: the podcast's item is looked up once (again on each poll
        only while it doesn't exist yet, as before a first upload), then each poll reads
        only that item."""
        deadline = self._clock() + timeout_s
        items: list[str] = []
        while True:
            if not items:
                items = self._podcast_items(podcast)
            found = self._in_items(items, wanted)
            if found:
                return found
            if self._clock() >= deadline:
                raise AbsTimeout(f"{what} did not appear in {podcast} within {timeout_s}s")
            self._sleep(poll_s)

    def wait_episode(self, podcast: str, filename: str,
                     timeout_s: float = 600, poll_s: float = 15) -> str:
        return self._wait_locate(podcast, _named(filename), f"episode {filename}", timeout_s, poll_s)[1]

    def wait_upload(self, podcast: str, item_id: str,
                    timeout_s: float = 600, poll_s: float = 15) -> tuple[str, str]:
        """find_upload, polling until it shows; AbsTimeout if it doesn't in time."""
        return self._wait_locate(podcast, _uploaded_for(item_id), f"the episode for work item {item_id}",
                                 timeout_s, poll_s)

    def set_episode(self, item_id: str, episode_id: str, title: str,
                    description: str, pub_date: str | None) -> None:
        self._call("PATCH", episode_path(item_id, episode_id), empty_ok=True,
                   json=details_body(title, description, pub_date))

    def create_podcast(self, podcast: str, cover: Path | None) -> str:
        """Create the podcast, titled `podcast`, in the folder an upload for it goes to
        (upload: the first podcast library, its first folder, a subfolder named after
        the podcast), and give it its cover. The scanner can't be left to make it from
        the first upload: it titles the podcast from the MP3's album tag ("The New
        Atlantis, No. 86 (Fall 2026)"), and the episode is never found under its own."""
        lib = self._library_for_upload(podcast)
        if not lib.get("folders"):
            raise AbsError(f"library {lib['id']} has no folders")
        folder = lib["folders"][0]
        item = self._call("POST", PODCASTS_PATH, json={
            "path": f"{folder['fullPath'].rstrip('/')}/{podcast}", "folderId": folder["id"],
            "libraryId": lib["id"], "autoDownloadEpisodes": False,
            "media": {"metadata": {"title": podcast}}})
        if not isinstance(item.get("id"), str):
            raise AbsError("POST /api/podcasts returned no item id")
        if cover is not None:
            self._upload_cover(item["id"], cover)  # just created: it has none
        return item["id"]

    def _upload_cover(self, item_id: str, cover: Path) -> None:
        try:
            kind = mimetypes.guess_type(cover.name)[0] or "application/octet-stream"
            with open(cover, "rb") as f:
                self._call("POST", cover_path(item_id), empty_ok=True, files={"cover": (cover.name, f, kind)})
        except (AbsError, OSError) as e:  # AbsError carries no key; OSError: the file
            log.warning("the podcast's cover was not set: %s", e)

    def settle_podcast(self, item_id: str, podcast: str, cover: Path | None) -> None:
        """After an episode is published: the podcast's title back to `podcast` if the
        scanner changed it (a podcast's first episode's album tag becomes its title,
        Audiobookshelf 2.37.2), and its cover if it has none. Best effort: the episode
        is live by now, the podcast is found by its folder meanwhile (is_podcast), and
        the next publish tries again."""
        try:
            item = self._call("GET", item_path(item_id))
        except AbsError as e:
            log.warning("the podcast's title and cover were not checked: %s", e)
            return
        if item_title(item) != podcast:
            try:
                self._call("PATCH", media_path(item_id), empty_ok=True, json={"metadata": {"title": podcast}})
            except AbsError as e:
                log.warning("the podcast's title was not set back: %s", e)
        if cover is not None and not item_cover(item):
            self._upload_cover(item_id, cover)

    def publish(self, podcast: str, mp3: Path, filename: str, title: str,
                description: str, pub_date: str | None,
                wait_timeout_s: float = 600, poll_s: float = 15, *, cover: Path | None = None) -> str:
        items = self._podcast_items(podcast)  # _locate's two steps, so the items are at hand
        found = self._in_items(items, _named(filename))
        if found is None:
            if not items:
                self.create_podcast(podcast, cover)
            self.upload(podcast, mp3, filename)
            found = self._wait_locate(podcast, _named(filename), f"episode {filename}", wait_timeout_s, poll_s)
        item_id, episode_id = found
        self.set_episode(item_id, episode_id, title, description, pub_date)
        self.settle_podcast(item_id, podcast, cover)
        return episode_id


CHECK_FILENAME = "abs-check.mp3"


def abs_check(abs: Audiobookshelf, podcast: str, mp3: Path) -> str:
    """Live-check helper: upload a short MP3, wait for it, set a test description."""
    return abs.publish(podcast, mp3, CHECK_FILENAME, "abs-check test episode",
                       "Test episode from abs-check. Safe to delete.", None)
