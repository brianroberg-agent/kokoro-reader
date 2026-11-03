"""Property tests for the night's run: any sequence of nights (with any outcome per item),
crashes at any step, reruns and time passing.

The fake Audiobookshelf shows an upload within 30 minutes (late_s <= 1800): that is
the window recovery waits for (R16). With a server slower than that, recovery takes
the episode for absent and uploads it again."""

import shutil
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from narration_pipeline.config import Settings
from narration_pipeline.record import STATUSES
from night_fakes import Crash, Script, World, restore_record_clock

CAP = 3

SCRIPTS = st.builds(
    Script,
    fetch=st.sampled_from(["ok"] * 5 + ["error"]),
    session=st.sampled_from(["clean", "clean", "minor", "hold", "gate_hold", "no_finish", "no_result",
                             "no_login", "api_down"]),
    session_s=st.sampled_from([60, 600, 7200]),  # few values: the windows between them matter
    mac=st.sampled_from([None] * 5 + ["blip_open", "blip_finalize", "blip_status", "blip_download",
                                      "down_in_session"]),
    abs=st.sampled_from(["ok"] * 4 + ["error_before_upload", "error_after_upload", "error_in_wait"]),
    late_s=st.sampled_from([0, 300, 1700, 1800]),
)


class Nights(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.dir = Path(tempfile.mkdtemp())
        self.world = World(self.dir, settings=Settings(cap=CAP))
        self.n = 0
        self.out_of_scope: set[str] = set()
        self.started: list[int] = []  # work items opened, per night
        self.report = None  # the last run's report, if it neither crashed nor was a dry run

    def teardown(self):
        restore_record_clock()
        shutil.rmtree(self.dir)

    def _run(self, dry_run: bool, crash_at: int | None) -> None:
        opened = len(self.world.narration.items)
        self.world.calls, self.world.crash_at = 0, crash_at
        try:
            report = self.world.run(dry_run=dry_run)
        except Crash:
            report = None
        self.world.crash_at = None
        self.report = None if dry_run else report
        self.started.append(len(self.world.narration.items) - opened)

    def _add(self, in_scope: bool, count: int) -> None:
        for _ in range(count):
            self.n += 1
            url = self.world.add_article(
                self.n, published=date(2026, 10, 1) if in_scope else date(2026, 9, 7))
            if not in_scope:
                self.out_of_scope.add(url)

    @initialize(in_scope=st.integers(1, 8), out_of_scope=st.integers(0, 2))
    def first_articles(self, in_scope, out_of_scope):
        self._add(True, in_scope)
        self._add(False, out_of_scope)

    @rule(in_scope=st.booleans(), count=st.integers(1, 3))
    def articles_appear(self, in_scope, count):
        self._add(in_scope, count)

    @rule(data=st.data(), dry_run=st.sampled_from([False, False, False, True]),
          crash_at=st.none() | st.integers(1, 30))
    def run_a_night(self, data, dry_run, crash_at):
        for url in self.world.urls():
            self.world.scripts[url] = data.draw(SCRIPTS)
        self._run(dry_run, crash_at)

    @rule()
    def rerun(self):
        self._run(False, None)

    @rule(seconds=st.integers(60, 2 * 86400))
    def let_time_pass(self, seconds):
        self.world.clock.sleep(seconds)

    @invariant()
    def no_article_has_two_episodes(self):
        per_article = Counter(self.world.url_of(ep) for ep in self.world.abs.episodes)
        assert all(n == 1 for n in per_article.values()), per_article

    @invariant()
    def every_article_processed_in_a_run_ends_in_one_status(self):
        if self.report is None:
            return
        urls = [r.url for r in self.report.results]
        assert len(urls) == len(set(urls)), urls
        for r in self.report.results:
            row = self.world.record.row(r.url)
            assert r.status == row["status"] in STATUSES, (r.url, r.status, row["status"])

    @invariant()
    def no_night_starts_more_than_the_cap(self):
        assert all(n <= CAP for n in self.started), self.started

    @invariant()
    def nothing_out_of_scope_is_queued_or_published(self):
        opened = {item.url for item in self.world.narration.items.values()}
        for url in self.out_of_scope:
            row = self.world.record.row(url)
            assert row is None or row["status"] == "baseline"
            assert url not in opened

    @invariant()
    def a_row_that_may_have_uploaded_is_never_failed(self):
        for url in self.world.urls():
            row = self.world.record.row(url)
            assert row is None or not (row["publish_started"] and row["status"] == "failed")


Nights.TestCase.settings = settings(max_examples=200, deadline=None, derandomize=True,
                                    database=None, stateful_step_count=12)
TestNights = Nights.TestCase
