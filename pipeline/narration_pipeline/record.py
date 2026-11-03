"""The record: one SQLite file, one row per article keyed by URL, and its state machine."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from narration_pipeline.config import Source
from narration_pipeline.discovery import Candidate, in_scope

STATUSES = (
    "baseline",
    "queued",
    "in_progress",
    "published",
    "published_with_notes",
    "held",
    "failed",
)

TRANSITIONS: dict[str, frozenset[str]] = {
    "baseline": frozenset(),
    "queued": frozenset({"in_progress"}),
    "failed": frozenset({"in_progress"}),
    "in_progress": frozenset({"published", "published_with_notes", "held", "failed"}),
    "published": frozenset(),
    "published_with_notes": frozenset(),
    "held": frozenset(),
}

Row = sqlite3.Row

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    published TEXT,
    edition INTEGER,
    status TEXT NOT NULL,
    item_id TEXT,
    publish_started INTEGER NOT NULL DEFAULT 0,
    episode_id TEXT,
    failed_nights INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    remaining_issues TEXT,
    rounds INTEGER,
    claude_cost_usd REAL,
    claude_tokens INTEGER,
    gemini_cost_usd REAL,
    run_seconds REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_FIELDS = frozenset(
    {
        "item_id",
        "publish_started",
        "episode_id",
        "failed_nights",
        "last_error",
        "remaining_issues",
        "rounds",
        "claude_cost_usd",
        "claude_tokens",
        "gemini_cost_usd",
        "run_seconds",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _checked(fields: dict) -> dict:
    unknown = set(fields) - _FIELDS
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    out = dict(fields)
    if "remaining_issues" in out and not isinstance(out["remaining_issues"], str | None):
        out["remaining_issues"] = json.dumps(out["remaining_issues"])
    return out


class Record:
    def __init__(self, path: Path):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._conn:
            self._conn.execute(_SCHEMA)

    def add_discovered(self, cands: list[Candidate], source: Source) -> tuple[int, int]:
        queued = baselined = 0
        now = _now()
        with self._conn:
            for c in cands:
                status = "queued" if in_scope(c, source) else "baseline"
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO articles"
                    " (url, source, title, published, edition, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        c.url,
                        source.id,
                        c.title,
                        c.published.isoformat() if c.published else None,
                        c.edition,
                        status,
                        now,
                        now,
                    ),
                )
                if cur.rowcount:
                    if status == "queued":
                        queued += 1
                    else:
                        baselined += 1
        return queued, baselined

    def pick(self, cap: int) -> list[Row]:
        # failed before queued; unknown date (NULL) before dated; then oldest, then URL.
        return self._conn.execute(
            "SELECT * FROM articles WHERE status IN ('failed', 'queued')"
            " ORDER BY status != 'failed', published IS NOT NULL, published, url LIMIT ?",
            (cap,),
        ).fetchall()

    def transition(self, url: str, to: str, **fields) -> None:
        fields = _checked(fields)
        with self._conn:
            row = self._conn.execute("SELECT status FROM articles WHERE url = ?", (url,)).fetchone()
            if row is None:
                raise ValueError(f"unknown article: {url}")
            if to not in TRANSITIONS.get(row["status"], frozenset()):
                raise ValueError(f"cannot move {url} from {row['status']} to {to}")
            self._write(url, {**fields, "status": to})

    def update(self, url: str, **fields) -> None:
        fields = _checked(fields)
        with self._conn:
            if self._conn.execute("SELECT 1 FROM articles WHERE url = ?", (url,)).fetchone() is None:
                raise ValueError(f"unknown article: {url}")
            self._write(url, fields)

    def _write(self, url: str, fields: dict) -> None:
        fields = {**fields, "updated_at": _now()}
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE articles SET {sets} WHERE url = ?", (*fields.values(), url))

    def in_progress(self) -> list[Row]:
        return self._conn.execute(
            "SELECT * FROM articles WHERE status = 'in_progress' ORDER BY published, url"
        ).fetchall()

    def row(self, url: str) -> Row | None:
        return self._conn.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()
