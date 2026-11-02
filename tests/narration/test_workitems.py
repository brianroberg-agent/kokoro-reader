import json
import os
import threading
import time

import pytest

from narration.workitems import WorkItemStore


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(tmp_path)


def test_open_creates_files_and_a_working_token(store):
    item_id, token = store.open("Some text.", {"title": "T"})
    assert len(item_id) == 12
    assert store.read_text(item_id, "source.md") == "Some text."
    assert store.read_text(item_id, "prepared.md") == "Some text."
    assert json.loads(store.read_text(item_id, "metadata.json")) == {"title": "T"}
    assert store.load_state(item_id)["status"] == "open"
    assert store.item_for_token(token) == item_id


def test_tokens_are_distinct_and_revocable(store):
    a, ta = store.open("a", {})
    b, tb = store.open("b", {})
    assert ta != tb and store.item_for_token(tb) == b
    store.revoke(a)
    assert store.item_for_token(ta) is None
    assert store.item_for_token(tb) == b


def test_unknown_token(store):
    assert store.item_for_token("nope") is None
    assert store.item_for_token("") is None


@pytest.mark.parametrize("bad_id", [
    "../x", "ABCDEF123456", "abc", "a" * 13,
    "0123456789ab\n", "0123456789ab ", "\n0123456789ab", "", None, 123,
])
def test_path_refuses_bad_ids(store, bad_id):
    with pytest.raises(ValueError):
        store.path(bad_id, "source.md")


@pytest.mark.parametrize("bad_name", [
    "../state.json", "other.txt", "report-9.txt", "report-0.txt",
    "report-1.txt\n", "report-1.txt ", "state.json\n", "", None, ["state.json"],
])
def test_path_refuses_unknown_names(store, bad_name):
    item_id, _ = store.open("x", {})
    with pytest.raises(ValueError):
        store.path(item_id, bad_name)


def test_report_names_one_to_eight_allowed(store):
    item_id, _ = store.open("x", {})
    assert store.path(item_id, "report-8.txt").name == "report-8.txt"


def test_tokens_file_holds_only_hashes(store, tmp_path):
    _, token = store.open("x", {})
    assert token not in (tmp_path / "tokens.json").read_text()


def test_open_cannot_resurrect_a_token_that_is_being_revoked(store, monkeypatch):
    # The race: open() reads tokens.json, revoke() reads and writes it, then open()
    # writes its stale copy back, with the revoked hash restored. The store's lock
    # must make revoke() wait for open() to finish.
    a, ta = store.open("a", {})
    real_tokens = store._tokens
    read = threading.Event()

    def slow_tokens():
        tokens = real_tokens()
        if not read.is_set():
            read.set()
            time.sleep(0.2)  # open() holds its stale copy while revoke() tries to run
        return tokens

    monkeypatch.setattr(store, "_tokens", slow_tokens)
    opened = []
    t = threading.Thread(target=lambda: opened.append(store.open("b", {})))
    t.start()
    assert read.wait(5)
    store.revoke(a)
    t.join(5)
    b, tb = opened[0]
    assert store.item_for_token(ta) is None
    assert store.item_for_token(tb) == b


# -- exact id and name matching --

@pytest.mark.parametrize("bad_id", ["0123456789ab\n", "0123456789ab ", "", None, 123, "../x"])
def test_exists_and_item_lock_refuse_bad_ids(store, tmp_path, bad_id):
    assert store.exists(bad_id) is False
    with pytest.raises(ValueError):
        store.item_lock(bad_id)
    assert not any(tmp_path.glob("items/*"))


def test_refused_names_create_nothing(store, tmp_path):
    item_id, _ = store.open("x", {})
    before = sorted(p.name for p in (tmp_path / "items").iterdir())
    for bad_id, name in [(item_id + "\n", "state.json"), (item_id, "report-1.txt\n")]:
        with pytest.raises(ValueError):
            store.write_text(bad_id, name, "boom")
    assert sorted(p.name for p in (tmp_path / "items").iterdir()) == before
    assert sorted(p.name for p in (tmp_path / "items" / item_id).iterdir()) == [
        "metadata.json", "prepared.md", "source.md", "state.json"]


def test_exists(store):
    item_id, _ = store.open("x", {})
    assert store.exists(item_id) is True
    assert store.exists("0123456789ab") is False


# -- per-item locks --

def test_item_lock_is_one_object_per_item(store):
    a, _ = store.open("a", {})
    b, _ = store.open("b", {})
    first = store.item_lock(a)
    assert isinstance(first, type(threading.Lock()))
    assert store.item_lock(a) is first
    assert store.item_lock(b) is not first
    got = []
    barrier = threading.Barrier(8)

    def grab():
        barrier.wait()
        got.append(store.item_lock(a))

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(got) == 8 and all(lock is first for lock in got)


# -- atomic writes --

def _stray_temp_files(tmp_path):
    return [p for p in tmp_path.rglob("*") if p.name.startswith(".tmp-")]


def test_failed_replace_leaves_old_content_and_no_temp_file(store, tmp_path, monkeypatch):
    item_id, _ = store.open("old", {})

    def boom(src, dst):
        raise OSError("replace failed")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            store.write_text(item_id, "source.md", "new")
    assert store.read_text(item_id, "source.md") == "old"
    assert _stray_temp_files(tmp_path) == []


def test_failed_write_leaves_old_content_and_no_temp_file(store, tmp_path, monkeypatch):
    item_id, _ = store.open("old", {})
    real_fdopen = os.fdopen

    class FailingFile:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()

        def write(self, data):
            raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(os, "fdopen", lambda fd, mode: FailingFile(real_fdopen(fd, mode)))
        with pytest.raises(OSError):
            store.write_text(item_id, "source.md", "new")
    assert store.read_text(item_id, "source.md") == "old"
    assert _stray_temp_files(tmp_path) == []


def test_write_is_flushed_to_disk_before_replace(store, monkeypatch):
    item_id, _ = store.open("old", {})
    calls = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    store.write_text(item_id, "source.md", "new")
    assert calls == ["fsync", "replace"]


def test_text_round_trips_byte_for_byte(store):
    text = "a\r\nb\rc\nd"
    item_id, _ = store.open(text, {})
    assert store.read_text(item_id, "source.md") == text
    assert store.path(item_id, "source.md").read_bytes() == text.encode()


# -- tokens: bad input and near misses --

def test_item_for_token_ignores_near_misses(store, tmp_path):
    item_id, token = store.open("x", {})
    stored_hash = next(iter(json.loads((tmp_path / "tokens.json").read_text())))
    assert store.item_for_token(token[:-1]) is None
    assert store.item_for_token(token[:10]) is None
    assert store.item_for_token(token + "x") is None
    assert store.item_for_token(stored_hash) is None
    assert store.item_for_token(token) == item_id


@pytest.mark.parametrize("bad", [None, 123, b"token", ["a"], {"a": 1}, "\ud800", "ok\udfff"])
def test_item_for_token_returns_none_for_bad_input(store, bad):
    store.open("x", {})
    assert store.item_for_token(bad) is None


# -- open() is all or nothing --

def _nothing_left(tmp_path):
    return not any((tmp_path / "items").iterdir()) and not (
        (tmp_path / "tokens.json").exists() and json.loads((tmp_path / "tokens.json").read_text()))


def test_open_with_unserializable_metadata_leaves_nothing(store, tmp_path):
    with pytest.raises(TypeError):
        store.open("x", {"bad": object()})
    assert _nothing_left(tmp_path)


def test_open_with_unencodable_article_leaves_nothing(store, tmp_path):
    with pytest.raises(UnicodeEncodeError):
        store.open("bad \ud800 text", {})
    assert _nothing_left(tmp_path)


@pytest.mark.parametrize("failing", ["save_state", "_save_tokens"])
def test_open_that_fails_part_way_removes_its_folder(store, tmp_path, monkeypatch, failing):
    keep, keep_token = store.open("keep", {})

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, failing, boom)
    with pytest.raises(OSError):
        store.open("x", {})
    assert [p.name for p in (tmp_path / "items").iterdir()] == [keep]
    assert store.item_for_token(keep_token) == keep
    assert len(json.loads((tmp_path / "tokens.json").read_text())) == 1
