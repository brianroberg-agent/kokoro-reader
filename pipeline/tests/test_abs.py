import json
from pathlib import Path

import pytest

from narration_pipeline.abs import Audiobookshelf, AbsError, AbsTimeout, abs_check, episode_filename

PODCAST = "Test Podcast"
KEY = "sekret-key-123"


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Routes (method, path) to a response or a callable(call) -> response; records calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kw):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        call = {"method": method, "path": path, **kw}
        self.calls.append(call)
        route = self.routes.get((method, path))
        if route is None:
            return FakeResponse(404, {"error": "no route"})
        return route(call) if callable(route) else route

    def paths(self, method):
        return [c["path"] for c in self.calls if c["method"] == method]


LIBS = {"libraries": [
    {"id": "lib-books", "mediaType": "book", "folders": [{"id": "f0"}]},
    {"id": "lib-pod", "mediaType": "podcast", "folders": [{"id": "fold-1"}]},
]}
ITEMS = {"results": [{"id": "it1", "media": {"metadata": {"title": PODCAST}}}]}


def item_with(filenames):
    return {"media": {"episodes": [
        {"id": f"ep-{i}", "audioFile": {"metadata": {"filename": n}}}
        for i, n in enumerate(filenames)]}}


def make(routes, **kw):
    s = FakeSession(routes)
    sleeps = []
    a = Audiobookshelf("http://abs.test/", KEY, session=s, sleep=sleeps.append, **kw)
    return a, s, sleeps


def base_routes(item_fn):
    return {
        ("GET", "/api/libraries"): FakeResponse(200, LIBS),
        ("GET", "/api/libraries/lib-pod/items"): FakeResponse(200, ITEMS),
        ("GET", "/api/items/it1"): item_fn,
        ("POST", "/api/upload"): FakeResponse(200, {}),
        ("PATCH", "/api/podcasts/it1/episode/ep-0"): FakeResponse(200, {}),
    }


@pytest.fixture
def mp3(tmp_path):
    p = tmp_path / "a.mp3"
    p.write_bytes(b"ID3fake")
    return p


def test_publish_uploads_then_sets_details(mp3):
    shown = {"n": 0}

    def item(call):
        shown["n"] += 1
        # first lookup (before upload): empty; later: episode there
        return FakeResponse(200, item_with([] if shown["n"] == 1 else ["x-1.mp3"]))

    a, s, _ = make(base_routes(item))
    ep = a.publish(PODCAST, mp3, "x-1.mp3", "Title", "Notes", "2026-11-02T10:00:00Z")
    assert ep == "ep-0"
    up = [c for c in s.calls if c["path"] == "/api/upload"]
    assert len(up) == 1
    assert up[0]["data"] == {"title": PODCAST, "library": "lib-pod", "folder": "fold-1"}
    assert up[0]["files"]["0"][0] == "x-1.mp3"
    assert up[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    patch = [c for c in s.calls if c["method"] == "PATCH"][0]
    assert patch["json"] == {"title": "Title", "description": "Notes",
                             "pubDate": "2026-11-02T10:00:00Z"}


def test_set_episode_omits_pubdate_when_none():
    a, s, _ = make(base_routes(FakeResponse(200, item_with(["x.mp3"]))))
    a.set_episode("it1", "ep-0", "T", "D", None)
    assert s.calls[0]["json"] == {"title": "T", "description": "D"}


def test_publish_finds_existing_episode_instead_of_uploading(mp3):
    a, s, _ = make(base_routes(FakeResponse(200, item_with(["x-1.mp3"]))))
    ep = a.publish(PODCAST, mp3, "x-1.mp3", "T", "D", None)
    assert ep == "ep-0"
    assert s.paths("POST") == []
    assert s.paths("PATCH") == ["/api/podcasts/it1/episode/ep-0"]


def test_wait_episode_appears_after_polls(mp3):
    n = {"i": 0}

    def item(call):
        n["i"] += 1
        return FakeResponse(200, item_with(["x.mp3"] if n["i"] >= 3 else []))

    a, _, sleeps = make(base_routes(item))
    assert a.wait_episode(PODCAST, "x.mp3", timeout_s=100, poll_s=5) == "ep-0"
    assert sleeps == [5, 5]


def test_wait_episode_times_out():
    t = {"now": 0.0}
    a, _, _ = make(base_routes(FakeResponse(200, item_with([]))), clock=lambda: t["now"])
    a._sleep = lambda s: t.__setitem__("now", t["now"] + s)
    with pytest.raises(AbsTimeout, match=r"episode x.mp3 did not appear in Test Podcast within 30s") as e:
        a.wait_episode(PODCAST, "x.mp3", timeout_s=30, poll_s=10)
    assert isinstance(e.value, AbsError)


def test_an_error_during_the_wait_is_not_a_timeout():
    routes = {("GET", "/api/libraries"): FakeResponse(502, {})}
    a, _, _ = make(routes)
    with pytest.raises(AbsError) as e:
        a.wait_episode(PODCAST, "x.mp3", timeout_s=30, poll_s=10)
    assert not isinstance(e.value, AbsTimeout)


def test_http_error_names_status_and_path_not_key():
    routes = {("GET", "/api/libraries"): FakeResponse(500, {"e": KEY})}
    a, _, _ = make(routes)
    with pytest.raises(AbsError) as e:
        a.find_episode(PODCAST, "x.mp3")
    assert "500" in str(e.value) and "/api/libraries" in str(e.value)
    assert KEY not in str(e.value)


def test_find_episode_none_when_podcast_missing():
    routes = base_routes(FakeResponse(200, item_with([])))
    routes[("GET", "/api/libraries/lib-pod/items")] = FakeResponse(200, {"results": []})
    a, _, _ = make(routes)
    assert a.find_episode(PODCAST, "x.mp3") is None


def test_filename_is_stable_and_safe():
    t = "Ünïcode / Title: with  spaces & symbols! " * 5
    n = episode_filename(t, "abc123")
    assert n == episode_filename(t, "abc123")
    assert "/" not in n and " " not in n
    assert n.endswith("-abc123.mp3") and n.isascii() and n == n.lower()
    assert len(n) <= 60 + len("-abc123.mp3")
    assert "--" not in n


def test_filename_cut_leaves_no_trailing_hyphen_and_empty_slug():
    assert episode_filename("a" * 59 + " bbb", "i1") == "a" * 59 + "-i1.mp3"
    assert episode_filename("???", "i1") == "episode-i1.mp3"


def test_abs_check_publishes_test_episode(mp3):
    shown = {"n": 0}

    def item(call):
        shown["n"] += 1
        return FakeResponse(200, item_with([] if shown["n"] == 1 else ["abs-check.mp3"]))

    a, s, _ = make(base_routes(item))
    assert abs_check(a, PODCAST, mp3) == "ep-0"
    assert s.paths("POST") == ["/api/upload"]
    assert s.calls[-1]["json"]["description"]


class HtmlResponse(FakeResponse):
    def json(self):
        raise ValueError("not json")


@pytest.mark.parametrize("bad", ["libraries", "items", "item"])
def test_non_json_200_on_lookup_raises_and_does_not_upload(mp3, bad):
    routes = base_routes(FakeResponse(200, item_with([])))
    key = {"libraries": ("GET", "/api/libraries"),
           "items": ("GET", "/api/libraries/lib-pod/items"),
           "item": ("GET", "/api/items/it1")}[bad]
    routes[key] = HtmlResponse(200, {})
    a, s, _ = make(routes)
    with pytest.raises(AbsError):
        a.publish(PODCAST, mp3, "x.mp3", "T", "D", None)
    assert s.paths("POST") == []


def test_publish_passes_wait_values_to_the_wait(mp3):
    t = {"now": 0.0}
    a, s, sleeps = make(base_routes(FakeResponse(200, item_with([]))), clock=lambda: t["now"])
    a._sleep = lambda x: (sleeps.append(x), t.__setitem__("now", t["now"] + x))
    with pytest.raises(AbsError, match="within 30s"):
        a.publish(PODCAST, mp3, "x.mp3", "T", "D", None, wait_timeout_s=30, poll_s=10)
    assert sleeps == [10, 10, 10]


def lookups(s):
    """(libraries, podcast item lists, item reads) made so far."""
    gets = s.paths("GET")
    return (gets.count("/api/libraries"), gets.count("/api/libraries/lib-pod/items"), gets.count("/api/items/it1"))


def test_a_wait_resolves_the_podcast_once_then_reads_only_its_item():
    n = {"i": 0}

    def item(call):
        n["i"] += 1
        assert call["params"] == {"expanded": 1}
        return FakeResponse(200, item_with(["x.mp3"] if n["i"] >= 4 else []))

    a, s, sleeps = make(base_routes(item))
    assert a.wait_episode(PODCAST, "x.mp3", timeout_s=100, poll_s=5) == "ep-0"
    assert sleeps == [5, 5, 5] and lookups(s) == (1, 1, 4)  # one item read per poll


def test_a_wait_looks_for_the_podcast_again_only_while_it_does_not_exist():
    # a first upload creates the podcast's item: until it shows, each poll lists again
    listed = {"n": 0}

    def items(call):
        listed["n"] += 1
        return FakeResponse(200, ITEMS if listed["n"] >= 3 else {"results": []})

    routes = base_routes(FakeResponse(200, item_with(["x.mp3"])))
    routes[("GET", "/api/libraries/lib-pod/items")] = items
    a, s, sleeps = make(routes)
    assert a.wait_episode(PODCAST, "x.mp3", timeout_s=100, poll_s=5) == "ep-0"
    assert sleeps == [5, 5] and lookups(s) == (3, 3, 1)


def test_publish_waits_on_the_item_alone_after_its_upload(mp3):
    shown = {"n": 0}

    def item(call):
        shown["n"] += 1
        return FakeResponse(200, item_with([] if shown["n"] <= 3 else ["x-1.mp3"]))

    a, s, sleeps = make(base_routes(item))
    assert a.publish(PODCAST, mp3, "x-1.mp3", "T", "D", None, poll_s=5) == "ep-0"
    # the look before the upload (1, 1, 1), the upload's library pick (1, 1, 0), then
    # the wait: resolved once, and one item read for each of its three polls
    assert sleeps == [5, 5] and lookups(s) == (3, 3, 4)
