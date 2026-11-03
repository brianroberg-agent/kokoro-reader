import fcntl
import json
from dataclasses import fields
from datetime import date, datetime, time, timedelta

import pytest

from narration_pipeline.abs import AbsError, episode_filename
from narration_pipeline.config import Settings
from narration_pipeline.discovery import Candidate
from narration_pipeline.night import Deps, deadlines, run_night
from night_fakes import HELD, ISSUES, NY, Crash, Script, World, restore_record_clock, source


@pytest.fixture(autouse=True)
def _real_record_clock_after():
    yield
    restore_record_clock()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def only_item(world):
    [item_id] = world.narration.items
    return item_id


def test_clean_publishes(world):
    url = world.add_article(1)
    report = world.run()
    [r] = report.results
    assert (r.url, r.title, r.status, r.outcome, r.dry_run_action) == (
        url, "Article 1", "published", "clean", None)
    assert r.cost_usd == 0.4 and r.gemini_usd == pytest.approx(3 * 0.03)
    assert r.notes == "Notes for Article 1"
    item_id = only_item(world)
    [ep] = world.abs.episodes
    assert ep.filename == episode_filename("Article 1", item_id)
    assert (ep.podcast, ep.title, ep.description, ep.pub_date) == (
        "Podcast tna", "Article 1", "Notes for Article 1", "2026-10-01")
    row = world.record.row(url)
    assert (row["status"], row["item_id"], row["episode_id"], row["publish_started"]) == (
        "published", item_id, ep.id, 1)
    assert row["rounds"] == 2 and row["claude_cost_usd"] == 0.4 and row["claude_tokens"] == 51000
    assert row["gemini_cost_usd"] == pytest.approx(0.09) and row["run_seconds"] == 1200
    assert world.sessions.requests == [{"item_id": item_id, "agent_token": f"tok-{item_id}",
                                        "text_rules": ["Read the dateline once."],
                                        "budget_usd": 2.0, "timeout_s": 5400}]
    assert world.sessions.waits == [(item_id, 5700)]
    assert world.narration.finalizes == [(item_id, False)]
    assert world.push.sent == []
    assert report.discovered == {"tna": 1} and report.discovery_errors == []
    assert not report.mac_unreachable and not report.stopped_at_cutoff


def test_minor_publishes_with_notes(world):
    url = world.add_article(1, script=Script(session="minor"))
    [r] = world.run().results
    assert (r.status, r.outcome) == ("published_with_notes", "minor")
    [ep] = world.abs.episodes
    assert ep.description == "Notes for Article 1\n\nRemaining: 12:40 mispronunciation"
    row = world.record.row(url)
    assert row["status"] == "published_with_notes"
    assert json.loads(row["remaining_issues"]) == ISSUES
    assert world.push.sent == []


@pytest.mark.parametrize("session,outcome,reason", [
    ("hold", "hold", "the agent held it (03:10 garbled: a paragraph is missing)"),
    ("gate_hold", "clean", "the prepared text changes a word without declaring it"),
])
def test_hold_pushes_reason(world, session, outcome, reason):
    url = world.add_article(1, script=Script(session=session))
    [r] = world.run().results
    assert (r.status, r.outcome, r.reasons) == ("held", outcome, [reason])
    assert world.abs.episodes == [] and world.narration.downloads == []
    [(title, body, _)] = world.push.sent
    assert "Article 1" in title and reason in body
    row = world.record.row(url)
    assert row["status"] == "held"
    if session == "hold":
        assert json.loads(row["remaining_issues"]) == HELD


def test_no_finish_fails_and_retries_next_night(world):
    url = world.add_article(1, script=Script(session="no_finish"))
    [r] = world.run().results
    assert (r.status, r.outcome) == ("failed", None)
    item_id = only_item(world)
    assert world.narration.finalizes == [(item_id, False), (item_id, True)]
    row = world.record.row(url)
    assert row["failed_nights"] == 1
    assert "without calling finish" in row["last_error"] and "error_max_budget_usd" in row["last_error"]
    assert row["claude_cost_usd"] == 2.0 and row["rounds"] == 4
    assert world.push.sent == [] and world.abs.episodes == []

    world.next_night()
    world.scripts[url] = Script()
    [r] = world.run().results
    assert r.status == "published" and len(world.narration.items) == 2


LOGIN_ERROR = "Claude Code is not logged in (CLAUDE_CODE_OAUTH_TOKEN missing or expired)"


def test_a_lost_claude_login_pushes_once_and_stops_the_night(world):
    a = world.add_article(1, script=Script(session="no_login"))
    b = world.add_article(2)
    report = world.run()
    [r] = report.results  # the second article isn't started
    assert report.login_failed and (r.url, r.status) == (a, "failed")
    item_id = only_item(world)
    assert world.narration.finalizes == [(item_id, False), (item_id, True)]  # finalized as usual
    row = world.record.row(a)
    assert (row["status"], row["failed_nights"], row["last_error"]) == ("failed", 0, LOGIN_ERROR)
    assert world.record.row(b)["status"] == "queued"
    assert world.push.sent == [("Claude login failed", LOGIN_ERROR, "default")]
    assert world.abs.episodes == []


def test_a_lost_login_never_holds_the_article(world):
    url = world.add_article(1, script=Script(session="no_login"))
    for night in range(4):
        [r] = world.run().results
        assert r.status == "failed"
        world.next_night()
    row = world.record.row(url)
    assert (row["status"], row["failed_nights"]) == ("failed", 0)
    assert [title for title, _, _ in world.push.sent] == ["Claude login failed"] * 4
    world.scripts[url] = Script()  # the login is renewed
    [r] = world.run().results
    assert r.status == "published"


def test_a_lost_login_in_a_dry_run_stops_it_without_a_push(world):
    world.add_article(1, script=Script(session="no_login"))
    world.add_article(2)
    report = world.run(dry_run=True)
    assert report.login_failed and len(report.results) == 1 and world.push.sent == []


def test_no_session_result_fails(world):
    url = world.add_article(1, script=Script(session="no_result"))
    [r] = world.run().results
    assert r.status == "failed"
    assert "no session result" in world.record.row(url)["last_error"]
    item_id = only_item(world)
    assert world.narration.finalizes == [(item_id, False), (item_id, True)]


def test_three_failed_nights_hold_and_push(world):
    url = world.add_article(1, script=Script(session="no_finish"))
    for _ in range(2):
        [r] = world.run().results
        assert r.status == "failed"
        world.next_night()
    assert world.push.sent == []
    [r] = world.run().results
    assert r.status == "held" and r.reasons[0].startswith("failed 3 nights: ")
    row = world.record.row(url)
    assert (row["status"], row["failed_nights"]) == ("held", 3)
    [(title, body, _)] = world.push.sent
    assert "Article 1" in title
    assert body.startswith("failed 3 nights: ") and "without calling finish" in body
    world.next_night()
    assert world.run().results == []
    assert len(world.narration.items) == 3  # three sessions, then no more spend


def test_mac_lost_mid_item_fails_item_and_continues(world):
    # The session ends without finish because the Mac stopped answering; the runner's
    # plain finalize fails the same way, finalize(force=True) gets through.
    a = world.add_article(1, script=Script(session="no_finish", mac="blip_finalize"))
    b = world.add_article(2)
    report = world.run()
    assert [(r.url, r.status) for r in report.results] == [(a, "failed"), (b, "published")]
    item_a = next(i for i, it in world.narration.items.items() if it.url == a)
    assert [f for f in world.narration.finalizes if f[0] == item_a] == [(item_a, False), (item_a, True)]
    row = world.record.row(a)
    assert row["failed_nights"] == 1 and "NarrationUnavailable" in row["last_error"]
    assert not report.mac_unreachable


def test_mac_blip_on_finalize_keeps_the_decision(world):
    # The session finished clean; the plain finalize hits a blip, finalize(force=True)
    # gets through and its answer (publish) stands.
    url = world.add_article(1, script=Script(mac="blip_finalize"))
    [r] = world.run().results
    assert r.status == "published"
    item_id = only_item(world)
    assert world.narration.finalizes == [(item_id, False), (item_id, True)]
    assert len(world.abs.episodes) == 1 and world.record.row(url)["failed_nights"] == 0
    world.next_night()
    assert world.run().results == [] and len(world.narration.items) == 1  # nothing re-recorded


def test_mac_blip_in_recovery_leaves_the_row_for_the_next_run(world):
    url = world.add_article(1, script=Script(mac="blip_finalize"))
    world.crash_on = "narration.finalize"  # the session finished; the run dies before finalize
    with pytest.raises(Crash):
        world.run()
    [r] = world.run().results  # recovery's finalize(force=True) hits the blip
    assert r.status == "in_progress" and world.record.row(url)["status"] == "in_progress"
    [r] = world.run().results
    assert r.status == "published"
    assert len(world.narration.items) == 1 and len(world.abs.episodes) == 1


def test_mac_gone_mid_item_waits_for_it_and_recovers_next_night(world):
    # The Mac is gone for 30 minutes from 01:05: finalize(force=True) can't get through
    # either, so the row is left in_progress, and the run waits for the Mac.
    a = world.add_article(1, script=Script(mac="down_in_session"))
    b = world.add_article(2)
    report = world.run()
    assert [(r.url, r.status) for r in report.results] == [(a, "in_progress"), (b, "published")]
    assert not report.mac_unreachable
    assert world.clock.sleeps[-6:] == [300] * 5 + [1200]  # polls 01:10..01:30, back at 01:35
    assert world.record.row(a)["status"] == "in_progress"

    world.next_night()
    world.scripts[a] = Script()
    assert [(r.url, r.status) for r in world.run().results] == [(a, "failed")]
    assert world.record.row(a)["failed_nights"] == 1
    world.next_night()
    assert [(r.url, r.status) for r in world.run().results] == [(a, "published")]


def test_a_recovered_row_is_not_retried_the_same_night(world):
    a = world.add_article(1)
    b = world.add_article(2)
    world.crash_on = "handoff.wait_result"  # the run dies while a's session is open
    with pytest.raises(Crash):
        world.run()
    world.next_night()
    report = world.run()
    assert [(r.url, r.status) for r in report.results] == [(a, "failed"), (b, "published")]
    assert world.record.row(a)["failed_nights"] == 1
    world.next_night()
    assert [(r.url, r.status) for r in world.run().results] == [(a, "published")]


def test_the_cap_holds_after_recovery_settles_a_row(tmp_path):
    world = World(tmp_path, settings=Settings(cap=2))
    a, b, c, d = (world.add_article(n) for n in range(1, 5))
    world.crash_on = "abs.publish:uploaded"  # a is uploaded, then the run dies
    with pytest.raises(Crash):
        world.run()
    report = world.run()
    assert [(r.url, r.status) for r in report.results] == [
        (a, "published"), (b, "published"), (c, "published")]  # a recovered, then two new
    assert world.record.row(d)["status"] == "queued" and len(world.narration.items) == 3


def test_waits_for_mac_until_0500_then_stops(world):
    world.narration.down_until = datetime(2026, 11, 4, 0, 0, tzinfo=NY)
    url = world.add_article(1)
    report = world.run()
    assert report.mac_unreachable and report.results == []
    assert world.clock.now() == datetime(2026, 11, 3, 5, 0, tzinfo=NY)
    assert world.narration.health_calls == 49 and set(world.clock.sleeps) == {300}
    assert world.narration.items == {}
    assert world.record.row(url)["status"] == "queued"  # discovery still recorded
    assert report.discovered == {"tna": 1}


def test_mac_back_before_0500_runs_the_night(world):
    world.narration.down_until = datetime(2026, 11, 3, 3, 12, tzinfo=NY)
    world.add_article(1)
    report = world.run()
    assert not report.mac_unreachable
    [r] = report.results
    assert r.status == "published"
    assert world.narration.health_calls == 28  # 01:00 .. 03:15, every 5 minutes


def test_no_new_item_after_0600(world):
    urls = [world.add_article(n, script=Script(session_s=7200)) for n in range(1, 5)]
    report = world.run()
    # 01:00-03:00, 03:00-05:00, 05:00-07:00; the fourth would start at 07:00.
    assert [r.url for r in report.results] == urls[:3]
    assert report.stopped_at_cutoff
    assert world.record.row(urls[3])["status"] == "queued"
    assert len(world.narration.items) == 3


@pytest.mark.parametrize("first_s,started", [(5 * 3600, 1), (5 * 3600 - 1, 2)])
def test_no_item_starts_at_exactly_0600(world, first_s, started):
    a = world.add_article(1, script=Script(session_s=first_s))  # 01:00 to 06:00, or one second less
    b = world.add_article(2)
    report = world.run()
    assert [r.url for r in report.results] == [a, b][:started]
    assert report.stopped_at_cutoff == (started == 1)
    if started == 1:
        assert world.clock.now() == datetime(2026, 11, 3, 6, 0, tzinfo=NY)
        assert world.record.row(b)["status"] == "queued"


def test_daytime_run_is_not_past_the_cutoff(tmp_path):
    world = World(tmp_path, start=datetime(2026, 11, 3, 14, 0, tzinfo=NY))
    world.add_article(1)
    report = world.run()
    assert [r.status for r in report.results] == ["published"]
    assert not report.stopped_at_cutoff


@pytest.mark.parametrize("start,mac,no_new", [
    (datetime(2026, 11, 3, 1, 0), datetime(2026, 11, 3, 5, 0), datetime(2026, 11, 3, 6, 0)),
    (datetime(2026, 11, 3, 5, 30), datetime(2026, 11, 3, 5, 0), datetime(2026, 11, 3, 6, 0)),
    (datetime(2026, 11, 3, 14, 0), datetime(2026, 11, 4, 5, 0), datetime(2026, 11, 4, 6, 0)),
    (datetime(2026, 11, 1, 1, 0), datetime(2026, 11, 1, 5, 0), datetime(2026, 11, 1, 6, 0)),
])
def test_deadlines_are_the_nights_0500_and_0600(start, mac, no_new):
    got = deadlines(start.replace(tzinfo=NY), time(5), time(6))
    assert got == (mac.replace(tzinfo=NY), no_new.replace(tzinfo=NY))
    if start.day == 1:  # the clocks went back at 02:00: the deadlines are in EST
        assert got[1].utcoffset() == timedelta(hours=-5)


def test_discovery_failure_is_reported_not_silent(tmp_path):
    broken, ok = source("broken"), source("ok")
    world = World(tmp_path, sources=[broken, ok])
    world.add_article(1, src=broken)
    world.broken_feeds[broken.feed] = "<html><body>Down for maintenance</body></html>"
    url = world.add_article(2, src=ok)
    report = world.run()
    [err] = report.discovery_errors
    assert err.startswith("broken: ") and "no items" in err
    assert report.discovered == {"ok": 1}
    assert world.record.row("https://example.test/broken/a1") is None
    assert [r.url for r in report.results] == [url]
    [(title, body, _)] = world.push.sent
    assert "Source broken" in title and "no items" in body


def test_crash_after_upload_recovers_without_second_upload(world):
    url = world.add_article(1)
    world.crash_on = "abs.publish:uploaded"
    with pytest.raises(Crash):
        world.run()
    row = world.record.row(url)
    assert (row["status"], row["publish_started"], row["episode_id"]) == ("in_progress", 1, None)
    assert len(world.abs.episodes) == 1

    [r] = world.run().results  # the crashed run's lock is gone
    assert r.status == "published"
    [ep] = world.abs.episodes
    assert world.record.row(url)["episode_id"] == ep.id
    assert ep.description == "Notes for Article 1"
    assert len(world.narration.items) == 1  # recorded once
    assert world.narration.downloads == [only_item(world)]  # the episode is there: no second download


def test_late_episode_after_crash_is_waited_for_not_uploaded_again(world):
    url = world.add_article(1, script=Script(late_s=1000))
    world.crash_on = "abs.publish:uploaded"
    with pytest.raises(Crash):
        world.run()
    [r] = world.run().results
    assert r.status == "published"
    [ep] = world.abs.episodes
    assert world.abs.waits == [(ep.filename, 1800)]
    assert world.record.row(url)["episode_id"] == ep.id
    assert len(world.narration.downloads) == 1


def test_wait_ended_by_an_error_is_not_taken_for_absent(world):
    url = world.add_article(1, script=Script(late_s=1000, abs="error_in_wait"))
    world.crash_on = "abs.publish:uploaded"
    with pytest.raises(Crash):
        world.run()
    [r] = world.run().results
    assert r.status == "in_progress" and "AbsError" in r.reasons[0]
    assert len(world.abs.episodes) == 1
    [r] = world.run().results
    assert r.status == "published" and len(world.abs.episodes) == 1
    assert world.record.row(url)["status"] == "published"


def test_an_error_at_the_end_of_the_wait_is_not_taken_for_absent(world, monkeypatch):
    url = world.add_article(1, script=Script(late_s=2400))  # shows only after the wait
    world.crash_on = "abs.publish:uploaded"
    with pytest.raises(Crash):
        world.run()

    def wait_then_error(podcast, filename, timeout_s=600, poll_s=15):
        world.clock.sleep(timeout_s)
        raise AbsError("GET /api/libraries returned 502")

    monkeypatch.setattr(world.abs, "wait_episode", wait_then_error)
    [r] = world.run().results
    assert r.status == "in_progress" and len(world.abs.episodes) == 1
    world.next_night()
    [r] = world.run().results
    assert r.status == "published" and len(world.abs.episodes) == 1
    assert world.record.row(url)["episode_id"] == world.abs.episodes[0].id


def test_crash_before_upload_waits_then_uploads_once(world):
    url = world.add_article(1)
    world.crash_on = "abs.publish"  # publish_started is set, nothing uploaded
    with pytest.raises(Crash):
        world.run()
    [r] = world.run().results
    assert r.status == "published" and len(world.abs.episodes) == 1
    assert world.abs.waits == [(world.abs.episodes[0].filename, 1800)]


def test_recovery_waits_only_what_is_left_of_30_minutes(world):
    world.add_article(1)
    world.crash_on = "abs.publish"  # publish_started is set, nothing uploaded
    with pytest.raises(Crash):
        world.run()
    world.clock.sleep(600)
    [r] = world.run().results
    assert r.status == "published"
    assert world.abs.waits == [(world.abs.episodes[0].filename, 1200)]


def test_next_night_recovery_does_not_wait_for_an_old_upload(world):
    world.add_article(1)
    world.crash_on = "abs.publish"
    with pytest.raises(Crash):
        world.run()
    world.next_night()
    [r] = world.run().results
    assert r.status == "published" and len(world.abs.episodes) == 1
    assert world.abs.waits == []


def test_upload_error_leaves_row_for_recovery_not_failed(world):
    url = world.add_article(1, script=Script(abs="error_after_upload"))
    [r] = world.run().results
    assert r.status == "in_progress"
    row = world.record.row(url)
    assert (row["status"], row["publish_started"], row["failed_nights"]) == ("in_progress", 1, 0)
    world.next_night()
    [r] = world.run().results
    assert r.status == "published" and len(world.abs.episodes) == 1


def test_a_row_recovery_cannot_settle_counts_nights_then_is_held(world):
    url = world.add_article(1)
    world.abs.down = True  # Audiobookshelf is down for good
    [r] = world.run().results
    assert r.status == "in_progress" and world.record.row(url)["failed_nights"] == 0
    for nights in (1, 2):
        world.next_night()
        [r] = world.run().results
        row = world.record.row(url)
        assert (r.status, row["status"], row["failed_nights"]) == ("in_progress", "in_progress", nights)
        assert row["last_error"].startswith("AbsError: ")
    assert world.push.sent == []
    world.next_night()
    [r] = world.run().results
    row = world.record.row(url)
    assert (r.status, row["status"], row["failed_nights"], row["publish_started"]) == ("held", "held", 3, 1)
    [(title, body, _)] = world.push.sent
    assert "Article 1" in title and body.startswith("failed 3 nights: AbsError: ")
    assert world.abs.episodes == [] and len(world.narration.items) == 1


def test_a_missing_title_falls_back_to_the_records(world, monkeypatch):
    world.add_article(1)
    real = world.article_assistant

    def untitled(url):
        text, meta = real(url)
        del meta["title"]
        return text, meta

    monkeypatch.setattr(world, "article_assistant", untitled)
    world.crash_on = "abs.publish:uploaded"
    with pytest.raises(Crash):
        world.run()
    [ep] = world.abs.episodes
    assert ep.filename == episode_filename("Article 1", only_item(world))
    [r] = world.run().results  # recovery computes the same file name and finds the upload
    assert r.status == "published"
    [ep] = world.abs.episodes
    assert ep.title == "Article 1"


def test_crash_after_finish_recovers_by_finalizing(world):
    url = world.add_article(1, script=Script(session="minor"))
    world.crash_on = "narration.finalize"
    with pytest.raises(Crash):
        world.run()
    assert world.record.row(url)["status"] == "in_progress"
    [r] = world.run().results
    assert r.status == "published_with_notes"
    assert len(world.narration.items) == 1 and len(world.abs.episodes) == 1


def test_a_bad_item_id_from_the_record_reaches_no_call(world):
    url = world.add_article(1)
    world.record.add_discovered([Candidate(url, "Article 1", date(2026, 10, 1), None, None)],
                                world.sources[0])
    world.record.transition(url, "in_progress", item_id="../../x")
    [r] = world.run().results
    assert r.status == "failed" and "work item id" in r.reasons[0]
    assert world.narration.finalizes == [] and world.sessions.requests == []


def test_a_bad_item_id_from_open_reaches_no_call(world, monkeypatch):
    url = world.add_article(1)
    monkeypatch.setattr(world.narration, "open", lambda text, meta: ("../x", "tok"))
    [r] = world.run().results
    assert r.status == "failed" and "work item id" in r.reasons[0]
    assert world.record.row(url)["item_id"] is None
    assert world.narration.finalizes == [] and world.sessions.requests == []


def test_dry_run_publishes_nothing_and_requeues(tmp_path):
    broken = source("broken")
    world = World(tmp_path, sources=[source(), broken])
    world.broken_feeds[broken.feed] = "<html></html>"
    a = world.add_article(1)
    b = world.add_article(2, script=Script(session="hold"))
    report = world.run(dry_run=True)
    assert [(r.url, r.status, r.dry_run_action) for r in report.results] == [
        (a, "queued", "publish"), (b, "queued", "hold")]
    assert report.results[0].notes == "Notes for Article 1"  # the dry run shows the would-be notes
    assert report.results[1].reasons == ["the agent held it (03:10 garbled: a paragraph is missing)"]
    assert world.abs.episodes == [] and world.narration.downloads == [] and world.push.sent == []
    assert len(report.discovery_errors) == 1
    for url in (a, b):
        row = world.record.row(url)
        assert (row["status"], row["item_id"]) == ("queued", None)
    assert report.discovered == {"tna": 2}

    world.next_night()
    assert [r.status for r in world.run().results] == ["published", "held"]


def test_dry_run_leaves_crashed_rows_alone(world):
    url = world.add_article(1)
    world.crash_on = "narration.finalize"
    with pytest.raises(Crash):
        world.run()
    calls = list(world.narration.finalizes)
    assert world.run(dry_run=True).results == []
    assert world.narration.finalizes == calls
    assert world.record.row(url)["status"] == "in_progress"


def test_only_runs_one_url(world):
    a, b, c = (world.add_article(n) for n in (1, 2, 3))
    report = world.run(only=b)
    assert [(r.url, r.status) for r in report.results] == [(b, "published")]
    assert world.record.row(a)["status"] == "queued" and world.record.row(c)["status"] == "queued"
    with pytest.raises(ValueError, match="not discovered: https://example.test/elsewhere"):
        world.run(only="https://example.test/elsewhere")
    with pytest.raises(ValueError, match="published"):
        world.run(only=b)


def test_only_refuses_a_baseline_row(world):
    old = world.add_article(1, published=date(2026, 9, 7))  # the day before the start date
    with pytest.raises(ValueError, match="baseline"):
        world.run(only=old)
    assert world.record.row(old)["status"] == "baseline" and world.narration.items == {}


def test_recovery_of_a_row_with_no_item_id_fails_it(world):
    url = world.add_article(1)
    world.record.add_discovered([Candidate(url, "Article 1", date(2026, 10, 1), None, None)],
                                world.sources[0])
    world.record.transition(url, "in_progress")  # the run stopped before open answered
    [r] = world.run().results
    assert (r.status, r.reasons) == ("failed", ["the run stopped before its work item was opened"])
    assert world.record.row(url)["failed_nights"] == 1 and world.narration.finalizes == []


def test_second_run_is_refused_while_one_holds_the_lock(world):
    world.add_article(1)
    with open(world.root / "run.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another run is in progress"):
            world.run()
    assert world.calls == 0
    assert [r.status for r in world.run().results] == ["published"]


def test_default_handoff_is_the_request_and_result_files(world):
    url = world.add_article(1)
    handoff = world.root / "handoff"
    (handoff / "results").mkdir(parents=True)
    item_id = f"{1:012x}"
    (handoff / "results" / f"{item_id}.json").write_text(json.dumps(
        {"item_id": item_id, "finished_ok": False, "cost_usd": 0.25, "num_turns": 3,
         "terminal_reason": "error_during_execution", "error": None}))
    base = world.deps()
    deps = Deps(**{f.name: getattr(base, f.name) for f in fields(Deps) if f.name != "handoff"})
    [r] = run_night(deps).results
    request = json.loads((handoff / "requests" / f"{item_id}.json").read_text())
    assert request == {"item_id": item_id, "agent_token": f"tok-{item_id}",
                       "text_rules": ["Read the dateline once."], "budget_usd": 2.0, "timeout_s": 5400}
    assert r.status == "failed" and r.cost_usd == 0.25
    row = world.record.row(url)
    assert "error_during_execution" in row["last_error"]
    assert row["claude_tokens"] is None  # this result has no tokens
