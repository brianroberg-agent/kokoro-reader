"""The night's run: recover, discover, wait for the Mac, then process up to the cap.

Every dependency comes in through Deps, so the whole flow runs over fakes in the tests.
"""

import fcntl
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from narration_pipeline import handoff as handoff_files
from narration_pipeline.abs import AbsError, AbsTimeout, episode_filename
from narration_pipeline.config import Settings, Source
from narration_pipeline.discovery import DiscoveryError, canonical_url, discover
from narration_pipeline.messages import discovery_failed_message, hold_message, plain, stopped_message
from narration_pipeline.narration_client import NarrationError, NarrationUnavailable, valid_item_id
from narration_pipeline.record import Record, Row
from narration_pipeline.session_service import HEARTBEAT_STALE_S, LOGIN_ERROR, RESULT_GRACE_S

MAC_POLL_S = 300                  # how often to ask whether the Mac is back
SESSION_GRACE_S = RESULT_GRACE_S  # wait for the session's result this long past its own timeout
RECOVERY_WAIT_S = 1800  # an upload has this long to show before recovery uploads again (R16, R21)
# Session categories for a failure outside the article: every later session would fail
# the same way, so the night stops, and the article doesn't count a failed night. The
# third such failure, "service", is a stale heartbeat (service_problem).
OUTSIDE = ("login", "api")


class RunInProgress(RuntimeError):
    pass


class NotRunnable(ValueError):
    """An --only run refused before its article started: the message says why."""


@dataclass
class Deps:
    record: Record
    sources: list[Source]
    settings: Settings
    fetch_text: Callable[[str], str]
    article_assistant: Callable[[str], tuple[str, dict]]  # url -> (markdown, metadata)
    narration: Any       # NarrationClient
    handoff_dir: Path
    abs: Any             # Audiobookshelf
    push: Any            # Push
    now: Callable[[], datetime]
    sleep: Callable[[float], None]
    record_dir: Path     # holds run.lock
    handoff: Any = handoff_files  # write_request, wait_result, withdraw_request, heartbeat_age


@dataclass
class ItemResult:
    url: str
    title: str
    status: str
    outcome: str | None
    reasons: list[str]
    cost_usd: float
    gemini_usd: float
    dry_run_action: str | None = None
    notes: str = ""  # finalize's episode notes, also in a dry run


@dataclass
class NightReport:
    discovered: dict[str, int] = field(default_factory=dict)  # source id -> newly queued
    results: list[ItemResult] = field(default_factory=list)
    discovery_errors: list[str] = field(default_factory=list)
    mac_unreachable: bool = False
    stopped_at_cutoff: bool = False
    stopped_by: str | None = None  # "login", "api" or "service": a failure outside the articles stopped the night


def deadlines(start: datetime, mac_wait_until: time, no_new_after: time) -> tuple[datetime, datetime]:
    """The night's two deadlines: the first `no_new_after` after the run starts, and
    `mac_wait_until` on that same day (already past for a run that starts in between).
    A run started by hand in the afternoon gets the next morning's."""
    tz = start.tzinfo
    no_new = datetime.combine(start.date(), no_new_after, tzinfo=tz)
    if no_new.timestamp() <= start.timestamp():
        no_new = datetime.combine(start.date() + timedelta(days=1), no_new_after, tzinfo=tz)
    return datetime.combine(no_new.date(), mac_wait_until, tzinfo=tz), no_new


def _seconds(a: datetime, b: datetime) -> float:
    return b.timestamp() - a.timestamp()  # real seconds, also across a DST change


def _error(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


@dataclass
class _Item:
    row: Row
    started: datetime
    recovered: bool = False
    item_id: str | None = None
    forced: bool = False         # finalize(force=True) already tried
    session: dict | None = None  # the session service's result
    fin: dict | None = None      # finalize's answer
    st: dict | None = None       # status()
    uploading: bool = False      # publish_started is set: an upload may exist
    outside: str | None = None   # "login", "api" or "service": the session failed for a reason outside the article
    outside_error: str = ""


def run_night(deps: Deps, dry_run: bool = False, only: str | None = None) -> NightReport:
    with open(deps.record_dir / "run.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunInProgress("another run is in progress") from None
        return _Night(deps, dry_run).run(only)


class _Night:
    def __init__(self, deps: Deps, dry_run: bool):
        self.d, self.s, self.rec = deps, deps.settings, deps.record
        self.dry_run = dry_run
        self.report = NightReport()
        self.sources = {s.id: s for s in deps.sources}
        self.unavailable = False  # an item saw NarrationUnavailable: ask whether the Mac is still there
        self.patient = True       # wait for the Mac (until mac_wait_until), or ask once (--only)
        start = deps.now().astimezone(ZoneInfo(self.s.tz))
        self.mac_until, self.no_new = deadlines(start, self.s.mac_wait_until, self.s.no_new_after)

    def run(self, only: str | None) -> NightReport:
        self.discover()
        if only is not None:
            try:
                only = canonical_url(only)  # as discovery wrote it
            except ValueError:
                raise NotRunnable(f"not a URL: {only}") from None
            row = self.rec.row(only)
            if row is None:
                raise NotRunnable(f"not discovered: {only}")
            if row["status"] not in ("queued", "failed"):
                raise NotRunnable(f"{only} is {row['status']}, not queued or failed")
            self.patient = False  # one article, run by hand: no waiting for the Mac
        if not self.wait_for_mac():
            if only is not None:
                raise NotRunnable("the narration server isn't reachable")
            return self.report
        if only is None and not self.dry_run:
            # After the Mac wait, not before: recovery needs the Mac (finalize, status, the MP3).
            for row in self.rec.in_progress():
                self.report.results.append(self.recover(row))
                if not self.mac_still_there():
                    return self.report
        # One result per article a night: a row recovery just settled waits for the next.
        seen = {r.url for r in self.report.results}
        picked = [r for r in self.rec.pick(self.s.cap + len(seen)) if r["url"] not in seen]
        for row in [self.rec.row(only)] if only else picked[:self.s.cap]:
            if self.d.now().timestamp() >= self.no_new.timestamp():
                self.report.stopped_at_cutoff = True
                break
            problem = self.service_problem()
            if problem:  # no session would run: start no item
                self.stop_night("service", problem)
                break
            self.report.results.append(self.process(row))
            if self.report.stopped_by or not self.mac_still_there():
                break
        return self.report

    # -- the steps around the items --
    def discover(self) -> None:
        for source in self.d.sources:
            try:
                cands = discover(source, self.d.fetch_text)
            except DiscoveryError as e:
                self.report.discovery_errors.append(str(e))
                self.notify(*discovery_failed_message(source.name, e.reason))
                continue
            self.report.discovered[source.id] = self.rec.add_discovered(cands, source)[0]

    def mac_still_there(self) -> bool:
        if not self.unavailable:
            return True
        self.unavailable = False
        return self.wait_for_mac()

    def wait_for_mac(self) -> bool:
        """Ask every 5 minutes until mac_wait_until; past it, give up for the night. An
        --only run asks once."""
        while not self.d.narration.health():
            remaining = _seconds(self.d.now(), self.mac_until)
            if remaining <= 0 or not self.patient:
                self.report.mac_unreachable = True
                return False
            self.d.sleep(min(MAC_POLL_S, remaining))
        return True

    def notify(self, title: str, body: str) -> None:
        if not self.dry_run:
            self.d.push.send(title, body)

    def stop_night(self, category: str, detail: str) -> None:
        self.report.stopped_by = category
        self.notify(*stopped_message(category, detail))

    def service_problem(self) -> str | None:
        """Why the session service isn't running, judged by its heartbeat, or None."""
        age = self.d.handoff.heartbeat_age(self.d.handoff_dir, self.d.now())
        if age is None:
            return "the session service has written no heartbeat"
        if age > HEARTBEAT_STALE_S:
            return f"the session service's last heartbeat was {age:.0f} s ago"
        return None

    # -- one item --
    def process(self, row: Row) -> ItemResult:
        it = _Item(row, self.d.now())
        try:
            source = self.sources[row["source"]]
            markdown, metadata = self.d.article_assistant(row["url"])
            item_id, token = self.d.narration.open(markdown, metadata)
            if not valid_item_id(item_id):  # it goes into handoff paths and the MP3's URL
                raise NarrationError(f"open returned a bad work item id: {item_id!r}")
            it.item_id = item_id
            if not self.dry_run:
                self.rec.transition(row["url"], "in_progress", item_id=it.item_id)
            self.d.handoff.write_request(self.d.handoff_dir, it.item_id, token, list(source.text_rules),
                                         self.s.session_budget_usd, self.s.session_timeout_s)
            it.session = self.d.handoff.wait_result(self.d.handoff_dir, it.item_id,
                                                    timeout_s=self.s.session_timeout_s + SESSION_GRACE_S)
            if it.session is None:  # given up on: if the service hasn't taken it, it won't
                self.d.handoff.withdraw_request(self.d.handoff_dir, it.item_id)
            self.classify(it)
            if it.outside:
                self.stop_night(it.outside, it.outside_error)
        except Exception as e:
            return self.after_error(it, e)
        return self.conclude(it)

    def recover(self, row: Row) -> ItemResult:
        """An in_progress row: the last run stopped partway through it. A row whose upload
        may exist is looked for in Audiobookshelf first, before the Mac is asked (R32)."""
        it = _Item(row, self.d.now(), recovered=True, item_id=row["item_id"],
                   uploading=bool(row["publish_started"]))
        if it.item_id is None:
            return self.fail(it, "the run stopped before its work item was opened")
        if not valid_item_id(it.item_id):
            return self.fail(it, f"the record holds a bad work item id: {it.item_id!r}")
        if it.uploading:
            try:
                located = self.earlier_upload(it)
            except Exception as e:  # Audiobookshelf can't say whether the upload is there:
                return self.leave(it, _error(e))  # not absent (R16); the next run looks again
            if located is not None:
                return self.live_already(it, *located)
        try:
            it.fin = self.force(it)  # on a finished or finalized item, the same as a plain finalize
        except Exception as e:
            return self.after_error(it, e)
        return self.conclude(it)

    def conclude(self, it: _Item) -> ItemResult:
        """Finalize (unless already decided), then publish, hold or fail by its answer."""
        try:
            if it.fin is None:
                it.fin = self.finalize(it)
            if it.outside and it.fin["action"] == "retry" and not self.dry_run:
                return self.not_the_articles(it)
            it.st = self.d.narration.status(it.item_id)
            if self.dry_run:
                return self.result(it, it.row["status"], it.fin.get("reasons") or [], it.fin["action"])
            self.save_costs(it)
            return self.settle(it)
        except Exception as e:
            return self.after_error(it, e)

    def finalize(self, it: _Item) -> dict:
        try:
            return self.d.narration.finalize(it.item_id)
        except NarrationUnavailable:
            raise
        except NarrationError:  # still open: the session ended without calling finish
            return self.force(it)

    def force(self, it: _Item) -> dict:
        """finalize(force=True): revokes the session's token, abandons an item still open,
        and decides. Tried once per item."""
        it.forced = True
        return self.d.narration.finalize(it.item_id, force=True)

    def settle(self, it: _Item) -> ItemResult:
        action = it.fin["action"]
        if action == "publish":
            return self.publish(it)
        if action == "hold":
            reasons = it.fin.get("reasons") or ["held by the gate"]
            self.rec.transition(it.row["url"], "held", last_error=plain("; ".join(reasons)))
            self.notify(*hold_message(it.row, reasons))
            return self.result(it, "held", reasons)
        return self.fail(it, self.why(it))

    def classify(self, it: _Item) -> None:
        """Whether the session failed for a reason outside the article: Claude Code
        couldn't authenticate ("login"), its API requests failed ("api"), or no result
        came and the service's heartbeat has stopped ("service")."""
        if it.session is None:
            problem = self.service_problem()
            if problem:
                it.outside, it.outside_error = "service", problem
            return
        category = it.session.get("category")
        if category == "login":
            it.outside, it.outside_error = "login", LOGIN_ERROR
        elif category in OUTSIDE:
            error = it.session.get("error")
            it.outside = category
            it.outside_error = error if isinstance(error, str) and error else "Claude Code's API requests failed"

    def not_the_articles(self, it: _Item) -> ItemResult:
        """The session failed for a reason outside the article, which says nothing about
        it (the push went when its result came in): the row is failed, so it comes first
        next night, without a failed night."""
        self.rec.transition(it.row["url"], "failed", last_error=it.outside_error)
        return self.result(it, "failed", [it.outside_error])

    def publish(self, it: _Item) -> ItemResult:
        """Download, upload and set the episode's details. A row whose upload may exist
        comes here only after recovery looked for it (earlier_upload) and found none: if
        it shows by now, abs.publish finds it by its file name instead of uploading."""
        meta = it.st["metadata"]  # the server's copy, so a rerun computes the same file name
        title = meta.get("title") or it.row["title"]
        filename = episode_filename(title, it.item_id)
        with tempfile.TemporaryDirectory(dir=self.d.record_dir) as tmp:
            mp3 = Path(tmp) / filename
            self.d.narration.download_mp3(it.item_id, mp3)
            self.rec.update(it.row["url"], publish_started=1)
            it.uploading = True
            episode_id = self.d.abs.publish(self.podcast(it), mp3, filename, title, it.fin.get("notes") or "",
                                            meta.get("date_published") or None)
        status = "published_with_notes" if it.fin["outcome"] == "minor" else "published"
        self.rec.transition(it.row["url"], status, episode_id=episode_id)
        return self.result(it, status, it.fin.get("reasons") or [])

    def podcast(self, it: _Item) -> str:
        return self.sources[it.row["source"]].podcast

    def earlier_upload(self, it: _Item) -> tuple[str, str] | None:
        """publish_started was set, so an upload may have reached Audiobookshelf without
        showing yet. Found by its work item (its title came from the Mac, which may not
        answer now): (Audiobookshelf item id, episode id), looking first and then waiting
        for what is left of RECOVERY_WAIT_S since the row was last written; None if it
        hasn't shown by then. Other AbsErrors propagate: an error is not absence."""
        found = self.d.abs.find_upload(self.podcast(it), it.item_id)
        if found is not None:
            return found
        age = _seconds(datetime.fromisoformat(it.row["updated_at"]), self.d.now())
        wait = min(RECOVERY_WAIT_S, RECOVERY_WAIT_S - age)
        if wait <= 0:
            return None
        try:
            return self.d.abs.wait_upload(self.podcast(it), it.item_id, timeout_s=wait)
        except AbsTimeout:
            return None  # not seen within the wait: publish uploads it

    def live_already(self, it: _Item, abs_item: str, episode_id: str) -> ItemResult:
        """Recovery found the row's episode in Audiobookshelf. It is live, so the row ends
        published whatever the Mac says now (R32): published_with_notes when the session's
        finish said minor. Its details are set again from finalize's notes when the Mac
        answers and finalize still says publish; anything left undone goes in last_error."""
        status, undone = "published", []
        try:
            it.fin = self.force(it)
            it.st = self.d.narration.status(it.item_id)
        except NarrationError as e:  # NarrationUnavailable too: the episode is live all the same
            self.unavailable |= isinstance(e, NarrationUnavailable)
            undone.append(f"the episode's details were not set again: the Mac didn't answer ({_error(e)})")
        else:
            if (it.st.get("finish") or {}).get("outcome") == "minor":
                status = "published_with_notes"
            if it.fin.get("action") == "publish":
                meta = it.st.get("metadata") or {}
                try:
                    self.d.abs.set_episode(abs_item, episode_id, meta.get("title") or it.row["title"],
                                           it.fin.get("notes") or "", meta.get("date_published") or None)
                except AbsError as e:
                    undone.append(f"the episode's details were not set again: {_error(e)}")
            else:
                why = "; ".join(it.fin.get("reasons") or [])
                undone.append(f"live already, though finalize now answers {it.fin.get('action')}"
                              + (f": {why}" if why else ""))
            self.save_costs(it)
        fields = {"episode_id": episode_id}
        if undone:
            fields["last_error"] = plain("; ".join(undone))
        self.rec.transition(it.row["url"], status, **fields)
        return self.result(it, status, undone)

    def after_error(self, it: _Item, e: Exception) -> ItemResult:
        error = _error(e)
        unavailable = isinstance(e, NarrationUnavailable)
        self.unavailable |= unavailable
        if it.item_id is not None and not it.forced and (it.fin is None or unavailable):
            # If the Mac answers finalize(force=True), its decision stands: a blip costs no
            # recorded work. Either way the session's token is revoked.
            try:
                fin = self.force(it)
            except NarrationUnavailable:
                self.unavailable = True
                return self.leave(it, error)
            except NarrationError:
                pass
            else:
                it.fin = fin
                if fin["action"] != "retry":
                    return self.conclude(it)
                if self.dry_run:
                    return self.leave(it, error)
                return self.not_the_articles(it) if it.outside else self.fail(it, error)
        if self.dry_run or (unavailable and it.item_id is not None):
            # The Mac went away after the forced finalize or after a decision: the next
            # run's recovery takes the row from there, without recording again.
            return self.leave(it, error)
        return self.fail(it, error)

    def leave(self, it: _Item, error: str) -> ItemResult:
        """The row stays as it is, for the next run's recovery. A recovery that leaves it
        counts a failed night, so a row recovery can't settle is held in the end."""
        status = self.rec.row(it.row["url"])["status"]
        if self.dry_run or not it.recovered or status != "in_progress":
            return self.result(it, status, [error], "retry" if self.dry_run else None)
        return self.count_failure(it, error, None)

    def fail(self, it: _Item, error: str) -> ItemResult:
        if it.uploading:  # an upload may exist (R16): a failed row would be recorded and uploaded again
            return self.leave(it, error)
        url = it.row["url"]
        if self.rec.row(url)["status"] != "in_progress":
            self.rec.transition(url, "in_progress")
        return self.count_failure(it, error, "failed")

    def count_failure(self, it: _Item, error: str, to: str | None) -> ItemResult:
        """failed_nights + 1, and the row moves to `to` (None: stays in_progress); at
        failed_nights_to_hold it is held instead, with a push naming the last error."""
        url = it.row["url"]
        nights = it.row["failed_nights"] + 1
        if nights >= self.s.failed_nights_to_hold:
            reasons = [f"failed {nights} nights: {error}"]
            self.rec.transition(url, "held", failed_nights=nights, last_error=error)
            self.notify(*hold_message(it.row, reasons))
            return self.result(it, "held", reasons)
        if to is None:
            self.rec.update(url, failed_nights=nights, last_error=error)
        else:
            self.rec.transition(url, to, failed_nights=nights, last_error=error)
        return self.result(it, to or "in_progress", [error])

    def why(self, it: _Item) -> str:
        why = "; ".join(it.fin.get("reasons") or []) or f"finalize answered {it.fin['action']}"
        if it.recovered:
            return f"{why} (after the previous run stopped)"
        if it.session is None:
            return f"{why} (no session result within {self.s.session_timeout_s + SESSION_GRACE_S} s)"
        detail = it.session.get("terminal_reason") or it.session.get("error")
        return f"{why} (session: {detail})" if detail else why

    def save_costs(self, it: _Item) -> None:
        fields = {"rounds": it.st.get("records"), "gemini_cost_usd": self.gemini_usd(it)}
        if not it.recovered:
            fields["claude_cost_usd"] = self.claude_usd(it)
            fields["run_seconds"] = _seconds(it.started, self.d.now())
            tokens = (it.session or {}).get("tokens")  # all four usage counts, from the session (R20a)
            if isinstance(tokens, int):
                fields["claude_tokens"] = tokens
        issues = (it.st.get("finish") or {}).get("remaining_issues")
        if issues:
            fields["remaining_issues"] = issues
        self.rec.update(it.row["url"], **fields)

    def claude_usd(self, it: _Item) -> float | None:
        return (it.session or {}).get("cost_usd")

    def gemini_usd(self, it: _Item) -> float | None:
        return None if it.st is None else it.st.get("checks", 0) * self.s.gemini_usd_per_check

    def result(self, it: _Item, status: str, reasons: list[str], dry: str | None = None) -> ItemResult:
        return ItemResult(url=it.row["url"], title=it.row["title"], status=status,
                          outcome=(it.fin or {}).get("outcome"), reasons=list(reasons),
                          cost_usd=self.claude_usd(it) or 0.0, gemini_usd=self.gemini_usd(it) or 0.0,
                          dry_run_action=dry, notes=(it.fin or {}).get("notes") or "")
