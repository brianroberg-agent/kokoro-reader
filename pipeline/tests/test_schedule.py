import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from narration_pipeline.config import Settings
from narration_pipeline.night import ItemResult, NightReport, RunInProgress
from narration_pipeline.schedule import next_at, schedule_loop

NY = "America/New_York"
Z = ZoneInfo(NY)


def test_next_run_is_0100_new_york_across_dst():
    first = next_at(datetime(2026, 11, 1, 0, 30, tzinfo=Z), time(1), NY)
    assert first == datetime(2026, 11, 1, 1, 0, tzinfo=Z) and first.utcoffset() == timedelta(hours=-4)
    second = next_at(first, time(1), NY)
    assert second == datetime(2026, 11, 2, 1, 0, tzinfo=Z) and second.utcoffset() == timedelta(hours=-5)


def test_next_run_across_the_march_change():
    a = next_at(datetime(2027, 3, 13, 1, 0, tzinfo=Z), time(1), NY)
    assert a == datetime(2027, 3, 14, 1, 0, tzinfo=Z) and a.utcoffset() == timedelta(hours=-5)
    b = next_at(a, time(1), NY)
    assert b == datetime(2027, 3, 15, 1, 0, tzinfo=Z) and b.utcoffset() == timedelta(hours=-4)


def test_next_at_reads_a_utc_now_in_the_zone():
    now = datetime(2026, 11, 2, 5, 59, tzinfo=UTC)  # 00:59 EST
    assert next_at(now, time(1), NY).astimezone(UTC) == datetime(2026, 11, 2, 6, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t):
        self.t = t.astimezone(UTC)
        self.sleeps = []

    def now(self):
        return self.t.astimezone(Z)

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += timedelta(seconds=s)


class Push:
    def __init__(self):
        self.sent = []

    def send(self, title, body, priority="default"):
        self.sent.append((title, body))


class D:
    settings = Settings()


def ran(clock, report=None, spend=0.0, minutes=30):
    """A run_night stand-in that takes `minutes` of the fake clock."""
    calls = []

    def run(deps, dry_run=False, only=None):
        calls.append(clock.now())
        clock.t += timedelta(minutes=minutes)
        return report if report is not None else NightReport(results=[
            ItemResult("u", "T", "published", None, [], 0.4, 0.1)])
    run.calls = calls
    return run


def loop(clock, push, run, nights=1):
    schedule_loop(lambda: D(), now=clock.now, sleep=clock.sleep, push=push, run=run, nights=nights)


def test_runs_at_start_and_sends_the_summary_at_seven():
    clock, push = Clock(datetime(2026, 11, 2, 12, 0, tzinfo=Z)), Push()
    run = ran(clock)
    loop(clock, push, run)
    assert run.calls == [datetime(2026, 11, 3, 1, 0, tzinfo=Z)]
    [(title, body)] = push.sent
    assert title == "Narration: 2026-11-03" and body.startswith("1 published")
    assert clock.now() == datetime(2026, 11, 3, 7, 0, tzinfo=Z)


def test_no_summary_when_nothing_happened():
    clock, push = Clock(datetime(2026, 11, 2, 12, 0, tzinfo=Z)), Push()
    loop(clock, push, ran(clock, NightReport()))
    assert push.sent == []


def test_a_run_that_ends_after_seven_sends_at_once():
    clock, push = Clock(datetime(2026, 11, 2, 12, 0, tzinfo=Z)), Push()
    loop(clock, push, ran(clock, minutes=7 * 60))  # ends 08:00
    assert len(push.sent) == 1 and clock.now() == datetime(2026, 11, 3, 8, 0, tzinfo=Z)


def test_two_nights_run_two_nights():
    clock, push = Clock(datetime(2026, 11, 2, 12, 0, tzinfo=Z)), Push()
    run = ran(clock)
    loop(clock, push, run, nights=2)
    assert run.calls == [datetime(2026, 11, 3, 1, 0, tzinfo=Z), datetime(2026, 11, 4, 1, 0, tzinfo=Z)]
    assert len(push.sent) == 2


def test_another_run_in_progress_is_logged_and_the_loop_goes_on(caplog):
    clock, push = Clock(datetime(2026, 11, 2, 12, 0, tzinfo=Z)), Push()
    good = ran(clock)
    calls = []

    def run(deps, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RunInProgress("another run is in progress")
        return good(deps, **kw)
    with caplog.at_level(logging.INFO):
        loop(clock, push, run, nights=2)
    assert len(calls) == 2 and len(push.sent) == 1
    assert "another run is in progress" in caplog.text


def test_run_in_progress_is_caught_by_class_not_message(caplog):
    clock, push = Clock(datetime(2026, 11, 2, 12, 0, tzinfo=Z)), Push()

    def run(deps, **kw):
        raise RunInProgress("worded differently")
    loop(clock, push, run)
    assert push.sent == []


def test_failure_push_is_sanitized():
    clock, push = Clock(datetime(2026, 11, 2, 12, 0, tzinfo=Z)), Push()

    def run(deps, **kw):
        raise ValueError("GET https://u:p@h.test/x?token=abc#f failed")
    loop(clock, push, run)
    assert push.sent == [("Narration run failed", "ValueError: GET https://h.test/x failed")]


def test_a_failed_run_is_pushed_once_with_its_traceback_logged(caplog):
    clock, push = Clock(datetime(2026, 11, 2, 12, 0, tzinfo=Z)), Push()
    good = ran(clock)
    calls = []

    def run(deps, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("boom")
        return good(deps, **kw)
    with caplog.at_level(logging.INFO):
        loop(clock, push, run, nights=2)
    assert len(calls) == 2
    assert push.sent[0] == ("Narration run failed", "ValueError: boom")
    assert len(push.sent) == 2 and push.sent[1][0].startswith("Narration: ")
    assert "Traceback" in caplog.text
