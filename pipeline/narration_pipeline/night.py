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
from narration_pipeline.abs import AbsError, episode_filename
from narration_pipeline.config import Settings, Source
from narration_pipeline.discovery import DiscoveryError, discover
from narration_pipeline.narration_client import NarrationError, NarrationUnavailable
from narration_pipeline.record import Record, Row

MAC_POLL_S = 300        # how often to ask whether the Mac is back
SESSION_GRACE_S = 300   # wait for the session's result this long past its own timeout
RECOVERY_WAIT_S = 1800  # how long recovery waits for an earlier upload to show (R16)


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
    handoff: Any = handoff_files  # write_request and wait_result


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


@dataclass
class NightReport:
    discovered: dict[str, int] = field(default_factory=dict)  # source id -> newly queued
    results: list[ItemResult] = field(default_factory=list)
    discovery_errors: list[str] = field(default_factory=list)
    mac_unreachable: bool = False
    stopped_at_cutoff: bool = False


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


def run_night(deps: Deps, dry_run: bool = False, only: str | None = None) -> NightReport:
    with open(deps.record_dir / "run.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another run is in progress") from None
        return _Night(deps, dry_run).run(only)


class _Night:
    def __init__(self, deps: Deps, dry_run: bool):
        self.d, self.s, self.rec = deps, deps.settings, deps.record
        self.dry_run = dry_run
        self.report = NightReport()
        self.sources = {s.id: s for s in deps.sources}
        self.unavailable = False  # an item saw NarrationUnavailable: ask whether the Mac is still there
        start = deps.now().astimezone(ZoneInfo(self.s.tz))
        self.mac_until, self.no_new = deadlines(start, self.s.mac_wait_until, self.s.no_new_after)

    def run(self, only: str | None) -> NightReport:
        self.discover()
        if only is not None:
            row = self.rec.row(only)
            if row is None:
                raise ValueError(f"not discovered: {only}")
            if row["status"] not in ("queued", "failed"):
                raise ValueError(f"{only} is {row['status']}, not queued or failed")
        if not self.wait_for_mac():
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
            self.report.results.append(self.process(row))
            if not self.mac_still_there():
                break
        return self.report

    # -- the steps around the items --
    def discover(self) -> None:
        for source in self.d.sources:
            try:
                cands = discover(source, self.d.fetch_text)
            except DiscoveryError as e:
                self.report.discovery_errors.append(str(e))
                self.notify(f"Discovery failed: {source.name}", e.reason)
                continue
            self.report.discovered[source.id] = self.rec.add_discovered(cands, source)[0]

    def mac_still_there(self) -> bool:
        if not self.unavailable:
            return True
        self.unavailable = False
        return self.wait_for_mac()

    def wait_for_mac(self) -> bool:
        """Ask every 5 minutes until mac_wait_until; past it, give up for the night."""
        while not self.d.narration.health():
            remaining = _seconds(self.d.now(), self.mac_until)
            if remaining <= 0:
                self.report.mac_unreachable = True
                return False
            self.d.sleep(min(MAC_POLL_S, remaining))
        return True

    def notify(self, title: str, body: str) -> None:
        if not self.dry_run:
            self.d.push.send(title, body)

    # -- one item --
    def process(self, row: Row) -> ItemResult:
        it = _Item(row, self.d.now())
        try:
            source = self.sources[row["source"]]
            markdown, metadata = self.d.article_assistant(row["url"])
            it.item_id, token = self.d.narration.open(markdown, metadata)
            if not self.dry_run:
                self.rec.transition(row["url"], "in_progress", item_id=it.item_id)
            self.d.handoff.write_request(self.d.handoff_dir, it.item_id, token, list(source.text_rules),
                                         self.s.session_budget_usd, self.s.session_timeout_s)
            it.session = self.d.handoff.wait_result(self.d.handoff_dir, it.item_id,
                                                    timeout_s=self.s.session_timeout_s + SESSION_GRACE_S)
        except Exception as e:
            return self.after_error(it, e)
        return self.conclude(it)

    def recover(self, row: Row) -> ItemResult:
        """An in_progress row: the last run stopped partway through it."""
        it = _Item(row, self.d.now(), recovered=True, item_id=row["item_id"],
                   uploading=bool(row["publish_started"]))
        if it.item_id is None:
            return self.fail(it, "the run stopped before its work item was opened")
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
            it.st = self.d.narration.status(it.item_id)
            if self.dry_run:
                return self.result(it, it.row["status"], it.fin.get("reasons") or [], it.fin["action"])
            self.save_costs(it)
            if it.uploading and it.fin["action"] == "publish":
                self.await_earlier_upload(it)
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
            self.rec.transition(it.row["url"], "held")
            self.notify(f"Held: {it.row['title']}", "\n".join(reasons))
            return self.result(it, "held", reasons)
        return self.fail(it, self.why(it))

    def publish(self, it: _Item) -> ItemResult:
        meta = it.st["metadata"]  # the server's copy, so recovery computes the same file name
        title = meta["title"]
        filename = episode_filename(title, it.item_id)
        podcast = self.sources[it.row["source"]].podcast
        with tempfile.TemporaryDirectory(dir=self.d.record_dir) as tmp:
            mp3 = self.d.narration.download_mp3(it.item_id, Path(tmp) / filename)
            self.rec.update(it.row["url"], publish_started=1)
            it.uploading = True
            episode_id = self.d.abs.publish(podcast, mp3, filename, title, it.fin.get("notes") or "",
                                            meta.get("date_published") or None)
        status = "published_with_notes" if it.fin["outcome"] == "minor" else "published"
        self.rec.transition(it.row["url"], status, episode_id=episode_id)
        return self.result(it, status, it.fin.get("reasons") or [])

    def await_earlier_upload(self, it: _Item) -> None:
        """publish_started was set, so an upload may have reached Audiobookshelf without
        showing yet. Look for it, then wait for it, before publish may upload again."""
        podcast = self.sources[it.row["source"]].podcast
        filename = episode_filename(it.st["metadata"]["title"], it.item_id)
        if self.d.abs.find_episode(podcast, filename) is not None:
            return
        waited_from = self.d.now()
        try:
            self.d.abs.wait_episode(podcast, filename, timeout_s=RECOVERY_WAIT_S)
        except AbsError:
            if _seconds(waited_from, self.d.now()) < RECOVERY_WAIT_S:
                raise  # the wait ended in an error, not at its timeout: absence isn't known
        # It showed (publish then finds it), or the wait timed out (publish then uploads).

    def after_error(self, it: _Item, e: Exception) -> ItemResult:
        error = _error(e)
        unavailable = isinstance(e, NarrationUnavailable)
        self.unavailable |= unavailable
        if it.uploading:
            return self.leave(it, error)
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
                return self.leave(it, error) if self.dry_run else self.fail(it, error)
        if self.dry_run or (unavailable and it.item_id is not None):
            # The Mac went away after the forced finalize or after a decision: the next
            # run's recovery takes the row from there, without recording again.
            return self.leave(it, error)
        return self.fail(it, error)

    def leave(self, it: _Item, error: str) -> ItemResult:
        status = self.rec.row(it.row["url"])["status"]
        return self.result(it, status, [error], "retry" if self.dry_run else None)

    def fail(self, it: _Item, error: str) -> ItemResult:
        if it.uploading:  # an upload may exist (R16): a failed row would be recorded and uploaded again
            return self.leave(it, error)
        url = it.row["url"]
        if self.rec.row(url)["status"] != "in_progress":
            self.rec.transition(url, "in_progress")
        nights = it.row["failed_nights"] + 1
        if nights < self.s.failed_nights_to_hold:
            self.rec.transition(url, "failed", failed_nights=nights, last_error=error)
            return self.result(it, "failed", [error])
        reasons = [f"failed {nights} nights: {error}"]
        self.rec.transition(url, "held", failed_nights=nights, last_error=error)
        self.notify(f"Held: {it.row['title']}", reasons[0])
        return self.result(it, "held", reasons)

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
                          dry_run_action=dry)
