import json
import sqlite3
from datetime import date

import pytest

from narration_pipeline.config import Source
from narration_pipeline.discovery import Candidate
from narration_pipeline.record import STATUSES, TRANSITIONS, Record

SOURCE = Source(
    id="new-atlantis",
    name="The New Atlantis",
    podcast="The New Atlantis",
    feed="https://example.test/feed",
    listing=None,
    extractor="new-atlantis",
    start_edition=80,
    start_date=date(2025, 1, 1),
    text_rules=(),
)


def cand(n, published=date(2026, 1, 1), edition=80, title=None):
    return Candidate(f"https://example.test/a{n}", title or f"Title {n}", published, edition, "Fall")


@pytest.fixture
def rec(tmp_path):
    return Record(tmp_path / "record.db")


def test_first_run_baselines_out_of_scope(rec):
    old = cand(1, edition=70, published=date(2020, 1, 1))
    new = cand(2)
    assert rec.add_discovered([old, new], SOURCE) == (1, 1)
    assert rec.row(old.url)["status"] == "baseline"
    r = rec.row(new.url)
    assert r["status"] == "queued"
    assert r["source"] == "new-atlantis"
    assert r["edition"] == 80
    assert r["title"] == "Title 2"
    assert r["published"] == "2026-01-01"
    assert r["created_at"] and r["updated_at"]


def test_rediscovery_changes_nothing(rec):
    cands = [cand(1, edition=70), cand(2)]
    assert rec.add_discovered(cands, SOURCE) == (1, 1)
    assert rec.add_discovered(cands, SOURCE) == (0, 0)


def test_pick_orders_and_caps(rec):
    cs = [cand(i, published=date(2026, 1, 10 - i)) for i in range(1, 6)]
    cs += [cand(6, published=date(2026, 1, 1)), cand(7, published=date(2026, 1, 1))]
    rec.add_discovered(cs, SOURCE)
    got = [r["url"] for r in rec.pick(5)]
    assert got == [c.url for c in (cs[5], cs[6], cs[4], cs[3], cs[2])]


def test_pick_puts_unknown_date_before_dated(rec):
    rec.add_discovered([cand(1, published=date(2020, 1, 1)), cand(2, published=None)], SOURCE)
    assert [r["url"] for r in rec.pick(5)] == [cand(2).url, cand(1).url]


def test_pick_takes_failed_before_queued(rec):
    rec.add_discovered([cand(1, published=date(2026, 1, 1)), cand(2, published=date(2026, 6, 1))], SOURCE)
    rec.transition(cand(2).url, "in_progress")
    rec.transition(cand(2).url, "failed", failed_nights=1)
    assert [r["url"] for r in rec.pick(5)] == [cand(2).url, cand(1).url]
    assert [r["url"] for r in rec.pick(1)] == [cand(2).url]


@pytest.mark.parametrize(
    "src,dst", [(s, d) for s, ds in TRANSITIONS.items() for d in sorted(ds)]
)
def test_every_allowed_transition(tmp_path, src, dst):
    rec = Record(tmp_path / "r.db")
    rec.add_discovered([cand(1)], SOURCE)
    url = cand(1).url
    path = {
        "queued": [],
        "failed": ["in_progress", "failed"],
        "in_progress": ["in_progress"],
    }[src]
    for step in path:
        rec.transition(url, step)
    assert rec.row(url)["status"] == src
    rec.transition(url, dst, last_error="x")
    r = rec.row(url)
    assert r["status"] == dst and r["last_error"] == "x"


def test_transitions_table_shape():
    assert len(STATUSES) == 7
    for s in ("baseline", "published", "published_with_notes", "held"):
        assert TRANSITIONS[s] == frozenset()


@pytest.mark.parametrize("src,dst", [("published", "in_progress"), ("baseline", "queued")])
def test_disallowed_transition_raises(rec, src, dst):
    rec.add_discovered([cand(1)], SOURCE)
    url = cand(1).url
    if src == "published":
        rec.transition(url, "in_progress")
        rec.transition(url, "published")
    else:
        rec.add_discovered([cand(2, edition=1, published=date(2000, 1, 1))], SOURCE)
        url = cand(2).url
    with pytest.raises(ValueError):
        rec.transition(url, dst)
    assert rec.row(url)["status"] == src


def test_transition_unknown_url_and_field_raise(rec):
    rec.add_discovered([cand(1)], SOURCE)
    with pytest.raises(ValueError):
        rec.transition("https://example.test/nope", "in_progress")
    with pytest.raises(ValueError):
        rec.transition(cand(1).url, "in_progress", bogus=1)
    assert rec.row(cand(1).url)["status"] == "queued"


def test_update_changes_fields_not_status(rec):
    rec.add_discovered([cand(1)], SOURCE)
    url = cand(1).url
    rec.transition(url, "in_progress", item_id="w1")
    before = rec.row(url)["updated_at"]
    rec.update(url, publish_started=1, claude_cost_usd=0.5)
    r = rec.row(url)
    assert r["status"] == "in_progress" and r["publish_started"] == 1
    assert r["claude_cost_usd"] == 0.5 and r["item_id"] == "w1"
    assert r["updated_at"] >= before
    for bad in ({"status": "published"}, {"nope": 1}, {"created_at": "x"}):
        with pytest.raises(ValueError):
            rec.update(url, **bad)
    assert [r["url"] for r in rec.in_progress()] == [url]


def test_remaining_issues_stored_as_json(rec):
    rec.add_discovered([cand(1)], SOURCE)
    url = cand(1).url
    rec.transition(url, "in_progress")
    rec.transition(url, "published_with_notes", remaining_issues=["a", "b"], episode_id="e1")
    assert json.loads(rec.row(url)["remaining_issues"]) == ["a", "b"]


def test_record_survives_reopen(tmp_path):
    path = tmp_path / "r.db"
    rec = Record(path)
    rec.add_discovered([cand(1)], SOURCE)
    rec.transition(cand(1).url, "in_progress", item_id="w9")
    rec2 = Record(path)
    assert rec2.row(cand(1).url)["item_id"] == "w9"
    assert rec2.in_progress()[0]["url"] == cand(1).url
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert rec2.row("https://example.test/none") is None
