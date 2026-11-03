import contextlib
import json
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from narration_pipeline import fetch, handoff, narration_client, push
from narration_pipeline.fetch import FetchError, fetch_text, run_article_assistant
from narration_pipeline.handoff import wait_result, write_request
from narration_pipeline.narration_client import NarrationClient, NarrationError
from narration_pipeline.push import Push


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _fake_run(content_out="# Body", meta=None, returncode=0, stderr=""):
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        if returncode:
            return _completed("", stderr, returncode)
        out = content_out if "content" in args else meta
        return _completed(out)

    run.calls = calls
    return run


GOOD_META = json.dumps({"title": "A Title", "authors": ["X"]})


def test_article_assistant_returns_markdown_and_metadata(monkeypatch, tmp_path):
    run = _fake_run(meta=GOOD_META)
    monkeypatch.setattr(subprocess, "run", run)
    md, meta = run_article_assistant("https://example.org/a", tmp_path)
    assert md == "# Body" and meta["title"] == "A Title"
    (a1, k1), (a2, k2) = run.calls
    assert a1 == ["uv", "run", "--frozen", "python", "article_assistant.py", "content",
                  "https://example.org/a", "--no-images"]
    assert a2 == ["uv", "run", "--frozen", "python", "article_assistant.py", "metadata",
                  "--json", "https://example.org/a"]
    assert k1["cwd"] == tmp_path and k1["timeout"] == 120 and k2["timeout"] == 120


def test_article_assistant_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_run(returncode=1, stderr="warn\nboom: no such page\n"))
    with pytest.raises(FetchError, match="boom: no such page"):
        run_article_assistant("https://example.org/a", tmp_path)


def test_article_assistant_non_json_is_a_fetch_error(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_run(meta="<html>"))
    with pytest.raises(FetchError):
        run_article_assistant("https://example.org/a", tmp_path)


def test_empty_title_is_a_fetch_error(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_run(meta=json.dumps({"title": "  "})))
    with pytest.raises(FetchError, match="title"):
        run_article_assistant("https://example.org/a", tmp_path)


def test_fetch_text_raises_on_non_200(monkeypatch):
    seen = {}

    def get(url, **kw):
        seen.update(kw)
        return SimpleNamespace(status_code=503, text="x")

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(FetchError):
        fetch_text("https://example.org/feed")
    assert seen["timeout"] == 30
    monkeypatch.setattr(requests, "get", lambda u, **kw: SimpleNamespace(status_code=200, text="ok"))
    assert fetch_text("https://example.org/feed") == "ok"


def test_push_failure_is_swallowed(monkeypatch, caplog):
    def post(*a, **kw):
        raise requests.ConnectionError("down: SECRET-TOKEN")

    monkeypatch.setattr(requests, "post", post)
    with caplog.at_level(logging.DEBUG):
        Push("https://ntfy.example/t", "SECRET-TOKEN").send("T", "private body")
    assert "SECRET-TOKEN" not in caplog.text and "private body" not in caplog.text


def test_push_sends_headers_and_encodes_curly_quote(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(requests, "post", post)
    Push("https://ntfy.example/t", "tok").send("The “New” Atlantis", "hello", priority="high")
    assert seen["url"] == "https://ntfy.example/t" and seen["timeout"] == 10
    assert seen["data"] == b"hello"
    h = seen["headers"]
    assert h["Authorization"] == "Bearer tok" and h["Priority"] == "high"
    h["Title"].encode("latin-1")  # must not raise
    assert h["Title"].startswith("=?UTF-8?B?")
    seen.clear()
    Push("https://ntfy.example/t", "tok").send("Plain", "b")
    assert seen["headers"]["Title"] == "Plain"


def test_push_non_2xx_is_swallowed(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: SimpleNamespace(status_code=500))
    Push("https://ntfy.example/t", "tok").send("T", "b")


def test_handoff_request_is_atomic(monkeypatch, tmp_path):
    visible = {}
    real_replace = os.replace

    def spy(src, dst):
        visible["before"] = sorted(p.name for p in (tmp_path / "requests").iterdir() if p.suffix == ".json")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    write_request(tmp_path, "item1", "agent-tok", ["rule a"], 2.0, 5400)
    assert visible["before"] == []  # nothing json-named existed before the rename
    data = json.loads((tmp_path / "requests" / "item1.json").read_text())
    assert data == {"item_id": "item1", "agent_token": "agent-tok", "text_rules": ["rule a"],
                    "budget_usd": 2.0, "timeout_s": 5400}
    assert sorted(p.name for p in (tmp_path / "requests").iterdir()) == ["item1.json"]
    assert (tmp_path / "running").is_dir() and (tmp_path / "results").is_dir()


def test_wait_result_reads_and_removes(tmp_path):
    (tmp_path / "results").mkdir()
    f = tmp_path / "results" / "i1.json"
    f.write_text(json.dumps({"item_id": "i1", "finished_ok": True}))
    assert wait_result(tmp_path, "i1", timeout_s=5, poll_s=0.01)["finished_ok"] is True
    assert not f.exists()


def test_wait_result_times_out_to_none(tmp_path):
    assert wait_result(tmp_path, "nope", timeout_s=0.05, poll_s=0.01) is None


class FakeMcp:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.result


def _client(result):
    fake = FakeMcp(result)

    @contextlib.asynccontextmanager
    async def factory(url, token):
        fake.url, fake.token = url, token
        yield fake

    return NarrationClient("http://h:1", "rtok", client_factory=factory), fake


def _res(text, is_error=False):
    return SimpleNamespace(is_error=is_error, content=[SimpleNamespace(text=text)])


def test_tool_error_becomes_narration_error():
    c, _ = _client(_res("item not found", is_error=True))
    with pytest.raises(NarrationError, match="item not found"):
        c.status("zzz")


def test_open_finalize_status_call_the_tools():
    c, fake = _client(_res(json.dumps({"item_id": "i9", "agent_token": "at"})))
    assert c.open("text", {"title": "T"}) == ("i9", "at")
    assert fake.calls == [("open", {"article_text": "text", "metadata": {"title": "T"}})]
    assert (fake.url, fake.token) == ("http://h:1/runner/mcp", "rtok")
    c, fake = _client(_res(json.dumps({"ok": 1})))
    assert c.finalize("i9", force=True) == {"ok": 1}
    assert fake.calls == [("finalize", {"item_id": "i9", "force": True})]
    assert c.status("i9") == {"ok": 1}


def test_health_and_download(monkeypatch, tmp_path):
    seen = []

    def get(url, **kw):
        seen.append((url, kw.get("headers")))
        if url.endswith("/health"):
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True})
        return SimpleNamespace(status_code=200, content=b"MP3DATA", text="")

    monkeypatch.setattr(requests, "get", get)
    c = NarrationClient("http://h:1/", "rtok")
    assert c.health() is True
    dest = tmp_path / "e.mp3"
    assert c.download_mp3("i9", dest) == dest and dest.read_bytes() == b"MP3DATA"
    assert seen[1] == ("http://h:1/runner/files/i9/episode.mp3", {"Authorization": "Bearer rtok"})

    def down(url, **kw):
        raise requests.ConnectionError("x")

    monkeypatch.setattr(requests, "get", down)
    assert c.health() is False

    monkeypatch.setattr(requests, "get", lambda u, **kw: SimpleNamespace(status_code=404, content=b"", text="{}"))
    with pytest.raises(NarrationError):
        c.download_mp3("i9", tmp_path / "x.mp3")
