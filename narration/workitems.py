"""Work items: one folder per article, owned by the narration server.

The agent never names a path or an item: it holds a per-item token, which
this store maps to an item id. Only token hashes are written to disk.

One process serves every item, and the MCP SDK runs sync tools in worker
threads, so two calls can overlap. Two kinds of lock keep that safe: `lock`
(store-wide) for tokens.json and state.json, and one lock per item that a
tool holds for a whole call.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from pathlib import Path

# Matched with fullmatch: `$` would also accept a trailing newline.
ID_RE = re.compile(r"[0-9a-f]{12}")
REPORT_RE = re.compile(r"report-[1-8]\.txt")
FILES = {"source.md", "prepared.md", "metadata.json", "state.json",
         "recording.wav", "episode.mp3", "result.json"}
# status: open -> finished (finish) -> finalized (finalize); open -> abandoned (finalize, force=True).
# records: record calls so far. recorded_round: the round whose audio is in recording.wav.
INITIAL_STATE = {"status": "open", "records": 0, "recorded_round": None, "checks": [], "finish": None}


def _hash(token: str) -> str:
    # surrogatepass: a lone surrogate (a JSON string can carry one) hashes to a
    # value that matches nothing, instead of raising.
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def _valid_id(item_id) -> bool:
    return isinstance(item_id, str) and ID_RE.fullmatch(item_id) is not None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class WorkItemStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "items").mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()  # held for every read or write of tokens.json and state.json
        self._item_locks: dict[str, threading.Lock] = {}

    def item_lock(self, item_id: str) -> threading.Lock:
        """The item's lock. An agent tool holds it for one call and refuses if it's
        taken; finalize waits for it. So one call at a time works on an item."""
        if not _valid_id(item_id):
            raise ValueError(f"bad work item id: {item_id!r}")
        with self.lock:
            return self._item_locks.setdefault(item_id, threading.Lock())

    # -- tokens: read-modify-written only here, under the store lock --
    def _tokens(self) -> dict:
        p = self.root / "tokens.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def _save_tokens(self, tokens: dict) -> None:
        _atomic_write(self.root / "tokens.json", json.dumps(tokens).encode())

    def item_for_token(self, token: str):
        if not token or not isinstance(token, str):
            return None
        with self.lock:
            return self._tokens().get(_hash(token))

    def revoke(self, item_id: str) -> None:
        with self.lock:
            tokens = {h: i for h, i in self._tokens().items() if i != item_id}
            self._save_tokens(tokens)

    # -- items --
    def open(self, article_text: str, metadata: dict):
        # Serialize and encode everything first, so bad input fails before any file exists.
        files = {
            "source.md": article_text.encode("utf-8"),
            "prepared.md": article_text.encode("utf-8"),
            "metadata.json": json.dumps(metadata).encode("utf-8"),
        }
        token = secrets.token_urlsafe(32)
        # mkdir without exist_ok claims a fresh id; the folder is ours to remove on failure.
        while True:
            item_id = secrets.token_hex(6)
            try:
                (self.root / "items" / item_id).mkdir(parents=True)
                break
            except FileExistsError:
                continue
        try:
            for name, data in files.items():
                _atomic_write(self.path(item_id, name), data)
            self.save_state(item_id, copy.deepcopy(INITIAL_STATE))
            with self.lock:
                tokens = self._tokens()
                tokens[_hash(token)] = item_id
                self._save_tokens(tokens)
        except BaseException:
            shutil.rmtree(self.root / "items" / item_id, ignore_errors=True)
            raise
        return item_id, token

    def exists(self, item_id: str) -> bool:
        return _valid_id(item_id) and (self.root / "items" / item_id).is_dir()

    def path(self, item_id: str, name: str) -> Path:
        if not _valid_id(item_id):
            raise ValueError(f"bad work item id: {item_id!r}")
        if not isinstance(name, str) or (name not in FILES and REPORT_RE.fullmatch(name) is None):
            raise ValueError(f"unknown work item file: {name!r}")
        return self.root / "items" / item_id / name

    def read_text(self, item_id: str, name: str) -> str:
        # newline="": no newline translation, so text round-trips what write_text wrote.
        with self.path(item_id, name).open(encoding="utf-8", newline="") as f:
            return f.read()

    def write_text(self, item_id: str, name: str, text: str) -> None:
        _atomic_write(self.path(item_id, name), text.encode("utf-8"))

    def load_state(self, item_id: str) -> dict:
        with self.lock:
            return json.loads(self.read_text(item_id, "state.json"))

    def save_state(self, item_id: str, state: dict) -> None:
        with self.lock:
            self.write_text(item_id, "state.json", json.dumps(state))
