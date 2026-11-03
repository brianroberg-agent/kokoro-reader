import contextlib
import json
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx2 as httpx
import pytest
import requests

from narration_pipeline import fetch, handoff, narration_client, push
from narration_pipeline.fetch import FetchError, fetch_text, run_article_assistant
from narration_pipeline.handoff import HandoffError, wait_result, write_request
from narration_pipeline.narration_client import NarrationClient, NarrationError, NarrationUnavailable
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
    c, fake = _client(_res(json.dumps({"item_id": "0123456789ab", "agent_token": "at"})))
    assert c.open("text", {"title": "T"}) == ("0123456789ab", "at")
    assert fake.calls == [("open", {"article_text": "text", "metadata": {"title": "T"}})]
    assert (fake.url, fake.token) == ("http://h:1/runner/mcp", "rtok")
    c, fake = _client(_res(json.dumps({"ok": 1})))
    assert c.finalize("i9", force=True) == {"ok": 1}
    assert fake.calls == [("finalize", {"item_id": "i9", "force": True})]
    assert c.status("i9") == {"ok": 1}


@pytest.mark.parametrize("item_id", ["../../etc", "0123456789AB", "0123456789a", "0123456789ab\n", 12])
def test_open_refuses_a_bad_item_id(item_id):
    c, _ = _client(_res(json.dumps({"item_id": item_id, "agent_token": "at"})))
    with pytest.raises(NarrationError, match="item_id"):
        c.open("text", {"title": "T"})


def test_runner_mcp_client_reads_for_900_seconds(monkeypatch):
    # The server's finalize waits up to 600 s for a busy item; mcp's default read timeout is 300 s.
    seen = {}

    def create(headers=None, timeout=None, auth=None):
        seen["headers"], seen["timeout"] = headers, timeout
        raise RuntimeError("stop before any request")

    monkeypatch.setattr(narration_client, "create_mcp_http_client", create)
    with pytest.raises(NarrationUnavailable):
        NarrationClient("http://h:1", "rtok").status("0123456789ab")
    assert seen["headers"] == {"Authorization": "Bearer rtok"}
    assert seen["timeout"].read == 900 and seen["timeout"].connect == 30


def _stream_resp(chunks, status=200, fail_after=None):
    def it(chunk_size=None):
        for i, c in enumerate(chunks):
            if fail_after is not None and i >= fail_after:
                raise requests.ConnectionError("cut")
            yield c

    return SimpleNamespace(status_code=status, iter_content=it, close=lambda: None, text="{}")


def test_health_and_download(monkeypatch, tmp_path):
    seen = []

    def get(url, **kw):
        seen.append((url, kw.get("headers")))
        if url.endswith("/health"):
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True})
        return _stream_resp([b"MP3", b"DATA"])

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

    monkeypatch.setattr(requests, "get", lambda u, **kw: _stream_resp([], status=404))
    with pytest.raises(NarrationError):
        c.download_mp3("i9", tmp_path / "x.mp3")


def _raising_client(exc):
    @contextlib.asynccontextmanager
    async def factory(url, token):
        raise exc
        yield

    return NarrationClient("http://h:1", "rtok", client_factory=factory)


@pytest.mark.parametrize("exc", [
    ExceptionGroup("unhandled errors", [RuntimeError("401")]),
    httpx.ConnectError("refused"),
    TimeoutError("slow"),
])
def test_transport_failures_become_narration_unavailable(exc):
    with pytest.raises(NarrationUnavailable):
        _raising_client(exc).status("i1")


def test_tool_refusal_is_not_unavailable():
    c, _ = _client(_res("nope", is_error=True))
    with pytest.raises(NarrationError) as ei:
        c.status("i1")
    assert not isinstance(ei.value, NarrationUnavailable)


def test_malformed_replies_are_narration_errors():
    c, _ = _client(_res(json.dumps({"item_id": "x"})))
    with pytest.raises(NarrationError):
        c.open("t", {})
    c, _ = _client(SimpleNamespace(is_error=False, content=[SimpleNamespace()]))
    with pytest.raises(NarrationError):
        c.status("i1")


def test_download_transport_failure_is_unavailable(monkeypatch, tmp_path):
    def down(url, **kw):
        raise requests.ConnectionError("x")

    monkeypatch.setattr(requests, "get", down)
    with pytest.raises(NarrationUnavailable):
        NarrationClient("http://h:1", "t").download_mp3("i", tmp_path / "a.mp3")
    monkeypatch.setattr(requests, "get", lambda u, **kw: _stream_resp([], status=503))
    with pytest.raises(NarrationUnavailable):
        NarrationClient("http://h:1", "t").download_mp3("i", tmp_path / "a.mp3")


def test_download_failure_midstream_leaves_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "get", lambda u, **kw: _stream_resp([b"a", b"b"], fail_after=1))
    dest = tmp_path / "a.mp3"
    with pytest.raises(NarrationUnavailable):
        NarrationClient("http://h:1", "t").download_mp3("i", dest)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("payload", ['["ok"]', "true", "{}", '{"ok": false}'])
def test_health_false_for_non_ok_shapes(monkeypatch, payload):
    monkeypatch.setattr(requests, "get", lambda u, **kw: SimpleNamespace(
        status_code=200, json=lambda: json.loads(payload)))
    assert NarrationClient("http://h:1", "t").health() is False


def test_missing_uv_is_a_fetch_error(monkeypatch, tmp_path):
    def run(*a, **kw):
        raise FileNotFoundError("uv")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(FetchError):
        run_article_assistant("https://example.org/a", tmp_path)


def test_handoff_modes_and_cleanup(monkeypatch, tmp_path):
    write_request(tmp_path / "h", "i1", "tok", [], 1.0, 10)
    for sub in ("requests", "running", "results"):
        assert (tmp_path / "h" / sub).stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "h" / "requests" / "i1.json").stat().st_mode & 0o777 == 0o600

    def boom(*a, **kw):
        raise OSError("disk")

    monkeypatch.setattr(json, "dumps", boom)
    with pytest.raises(OSError):
        write_request(tmp_path / "h", "i2", "tok", [], 1.0, 10)
    assert sorted(p.name for p in (tmp_path / "h" / "requests").iterdir()) == ["i1.json"]


def test_wait_result_polls_past_a_partial_file(tmp_path):
    (tmp_path / "results").mkdir()
    f = tmp_path / "results" / "i1.json"
    f.write_text('{"item_')
    sleeps = []

    def fix(s):
        sleeps.append(s)
        f.write_text(json.dumps({"item_id": "i1", "finished_ok": True}))

    import time as _t
    orig = _t.sleep
    _t.sleep = fix
    try:
        assert wait_result(tmp_path, "i1", timeout_s=5, poll_s=0.01)["finished_ok"] is True
    finally:
        _t.sleep = orig
    assert sleeps


def test_wait_result_corrupt_at_timeout_raises(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "i1.json").write_text("{nope")
    with pytest.raises(HandoffError, match="unreadable result for i1"):
        wait_result(tmp_path, "i1", timeout_s=0.05, poll_s=0.01)
