"""In-memory fakes for the night's dependencies, shared by test_night.py and
test_night_properties.py. A World holds one fake clock and every fake; each article
has a Script saying what happens to it when it is processed."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from narration_pipeline.abs import AbsError
from narration_pipeline.config import Settings, Source
from narration_pipeline.fetch import FetchError
from narration_pipeline.narration_client import NarrationError, NarrationUnavailable
from narration_pipeline.night import Deps, NightReport, run_night
from narration_pipeline.record import Record

NY = ZoneInfo("America/New_York")
START = datetime(2026, 11, 3, 1, 0, tzinfo=NY)
ISSUES = [{"kind": "mispronunciation", "where": "12:40", "note": "Zounk"}]
HELD = [{"kind": "garbled", "where": "03:10", "note": "a paragraph is missing"}]


def source(id: str = "tna") -> Source:
    return Source(id=id, name=f"Source {id}", podcast=f"Podcast {id}",
                  feed=f"https://example.test/{id}/feed", listing=None, extractor="generic",
                  start_edition=86, start_date=date(2026, 9, 8),
                  text_rules=("Read the dateline once.",))


class Crash(BaseException):
    """The runner process dies here. A BaseException, so the runner's `except Exception`
    lets it through."""


@dataclass
class Script:
    fetch: str = "ok"        # ok | error
    session: str = "clean"   # clean | minor | hold | gate_hold | no_finish | no_result
    session_s: int = 1200    # how long the session takes
    mac: str | None = None   # None | blip_<call> (that call fails once) | down_in_session
    abs: str = "ok"          # ok | error_before_upload | error_after_upload | error_in_wait (once each)
    late_s: int = 0          # how long Audiobookshelf takes to show an upload


class Clock:
    def __init__(self, start: datetime):
        self.t = start.astimezone(UTC)  # arithmetic in UTC, so a DST change doesn't skew it
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.t.astimezone(NY)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)


def feed_xml(items: list[tuple[str, str, date]]) -> str:
    body = "".join(
        f"<item><title>{escape(title)}</title><link>{escape(url)}</link>"
        f"<pubDate>{format_datetime(datetime.combine(day, time(12), tzinfo=UTC))}</pubDate></item>"
        for url, title, day in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>{body}</channel></rss>'


@dataclass
class FakeItem:
    url: str
    metadata: dict
    script: Script
    status: str = "open"
    finish: dict | None = None
    records: int = 0
    checks: int = 0

    def decision(self) -> dict:
        title = self.metadata["title"]
        kind = self.finish["kind"]
        if kind == "clean":
            return {"action": "publish", "outcome": "clean", "reasons": [],
                    "notes": f"Notes for {title}", "mp3": True}
        if kind == "minor":
            return {"action": "publish", "outcome": "minor", "reasons": [],
                    "notes": f"Notes for {title}\n\nRemaining: 12:40 mispronunciation", "mp3": True}
        if kind == "hold":
            return {"action": "hold", "outcome": "hold", "notes": "", "mp3": False,
                    "reasons": ["the agent held it (03:10 garbled: a paragraph is missing)"]}
        return {"action": "hold", "outcome": "clean", "notes": "", "mp3": False,
                "reasons": ["the prepared text changes a word without declaring it"]}


class FakeNarration:
    def __init__(self, world: "World"):
        self.w = world
        self.items: dict[str, FakeItem] = {}
        self.down_until: datetime | None = None
        self.blipped: set[tuple[str, str]] = set()
        self.health_calls = 0
        self.finalizes: list[tuple[str, bool]] = []
        self.downloads: list[str] = []

    def up(self) -> bool:
        return self.down_until is None or self.w.clock.now() >= self.down_until

    def _call(self, name: str, key: str, script: Script) -> None:
        self.w.tick(f"narration.{name}")
        if script.mac == f"blip_{name}" and (key, name) not in self.blipped:
            self.blipped.add((key, name))
            raise NarrationUnavailable(f"{name}: ConnectError")
        if not self.up():
            raise NarrationUnavailable(f"{name}: ConnectError")

    def _item(self, name: str, item_id: str) -> FakeItem:
        item = self.items.get(item_id)
        self._call(name, item_id, item.script if item else Script())
        if item is None:
            raise NarrationError("no such work item")
        return item

    def health(self) -> bool:
        self.w.tick("narration.health")
        self.health_calls += 1
        return self.up()

    def open(self, text: str, metadata: dict) -> tuple[str, str]:
        url = metadata["url"]
        script = self.w.scripts[url]
        self._call("open", url, script)
        item_id = f"{len(self.items) + 1:012x}"
        self.items[item_id] = FakeItem(url, dict(metadata), script)
        return item_id, f"tok-{item_id}"

    def finalize(self, item_id: str, force: bool = False) -> dict:
        self.finalizes.append((item_id, force))
        item = self._item("finalize", item_id)
        if item.status == "open":
            if not force:
                raise NarrationError("this work item is still open: the session has not called finish")
            item.status = "abandoned"
        if item.status == "abandoned":
            return {"action": "retry", "outcome": None, "notes": "", "mp3": False,
                    "reasons": ["the session ended without calling finish"]}
        item.status = "finalized"
        return item.decision()

    def status(self, item_id: str) -> dict:
        item = self._item("status", item_id)
        meta = {"title": item.metadata["title"].strip(),
                "date_published": item.metadata.get("date_published") or "", "authors": []}
        return {"exists": True, "metadata": meta, "status": item.status, "records": item.records,
                "recorded_round": item.records or None, "checks_used": item.checks,
                "checks": item.checks, "prepared_matches_recording": True,
                "finish": item.finish and {k: v for k, v in item.finish.items() if k != "kind"},
                "mp3": item.status == "finalized" and item.decision()["mp3"]}

    def download_mp3(self, item_id: str, dest: Path) -> Path:
        item = self._item("download", item_id)
        if item.status != "finalized" or not item.decision()["mp3"]:
            raise NarrationError(f"download of {item_id} returned 404")
        dest.write_bytes(b"ID3" + item_id.encode())
        self.downloads.append(item_id)
        return dest


class FakeSessions:
    """The session service on the far side of the handoff: wait_result runs the scripted session."""

    def __init__(self, world: "World"):
        self.w = world
        self.requests: list[dict] = []
        self.waits: list[tuple[str, float]] = []

    def write_request(self, handoff, item_id, agent_token, text_rules, budget_usd, timeout_s):
        self.w.tick("handoff.write_request")
        self.requests.append({"item_id": item_id, "agent_token": agent_token,
                              "text_rules": list(text_rules), "budget_usd": budget_usd,
                              "timeout_s": timeout_s})

    def wait_result(self, handoff, item_id, timeout_s, poll_s=5):
        self.w.tick("handoff.wait_result")
        self.waits.append((item_id, timeout_s))
        item = self.w.narration.items[item_id]
        script, clock = item.script, self.w.clock
        if script.session == "no_result":
            clock.sleep(timeout_s)
            return None
        result = {"item_id": item_id, "finished_ok": False, "cost_usd": 0.4, "num_turns": 30,
                  "terminal_reason": None, "error": None}
        if script.mac == "down_in_session":
            clock.sleep(300)
            self.w.narration.down_until = clock.now() + timedelta(minutes=30)
            clock.sleep(300)
            item.records = 1
            return {**result, "cost_usd": 0.12, "error": "the narration server stopped answering"}
        clock.sleep(script.session_s)
        if script.session == "no_finish":
            item.records, item.checks = 4, 8
            return {**result, "cost_usd": 2.0, "terminal_reason": "error_max_budget_usd"}
        kind = script.session
        item.records, item.checks, item.status = 2, 3, "finished"
        issues = {"minor": ISSUES, "hold": HELD}.get(kind, [])
        item.finish = {"kind": kind, "outcome": "clean" if kind == "gate_hold" else kind,
                       "remaining_issues": issues, "substitutions": []}
        return {**result, "finished_ok": True, "terminal_reason": "success"}


@dataclass
class Episode:
    id: str
    podcast: str
    filename: str
    appears_at: datetime
    title: str | None = None
    description: str | None = None
    pub_date: str | None = None


class FakeAbs:
    def __init__(self, world: "World"):
        self.w = world
        self.episodes: list[Episode] = []  # one per upload, as a real server would make
        self.waits: list[tuple[str, float]] = []
        self.failed: set[tuple[str, str]] = set()
        self.down = False  # every call fails until it is set back

    def _call(self, name: str) -> None:
        self.w.tick(name)
        if self.down:
            raise AbsError("GET /api/libraries failed: ConnectionError")

    def _visible(self, podcast: str, filename: str) -> Episode | None:
        now = self.w.clock.now()
        return next((e for e in self.episodes if (e.podcast, e.filename) == (podcast, filename)
                     and e.appears_at <= now), None)

    def _fails(self, filename: str, mode: str) -> bool:
        item_id = filename.rsplit("-", 1)[1].removesuffix(".mp3")
        if self.w.narration.items[item_id].script.abs == mode and (item_id, mode) not in self.failed:
            self.failed.add((item_id, mode))
            return True
        return False

    def _wait(self, podcast: str, filename: str, timeout_s: float) -> Episode:
        clock = self.w.clock
        limit = clock.now() + timedelta(seconds=timeout_s)
        pending = [e for e in self.episodes if (e.podcast, e.filename) == (podcast, filename)
                   and e.appears_at <= limit]
        if not pending:
            clock.sleep(timeout_s)
            raise AbsError(f"episode {filename} did not appear in {podcast} within {timeout_s}s")
        ep = min(pending, key=lambda e: e.appears_at)
        if ep.appears_at > clock.now():
            clock.sleep((ep.appears_at - clock.now()).total_seconds())
        return ep

    def find_episode(self, podcast: str, filename: str) -> str | None:
        self._call("abs.find_episode")
        ep = self._visible(podcast, filename)
        return ep.id if ep else None

    def wait_episode(self, podcast: str, filename: str, timeout_s: float = 600,
                     poll_s: float = 15) -> str:
        self._call("abs.wait_episode")
        self.waits.append((filename, timeout_s))
        if self._fails(filename, "error_in_wait"):
            raise AbsError("GET /api/libraries failed: ConnectionError")
        return self._wait(podcast, filename, timeout_s).id

    def publish(self, podcast, mp3, filename, title, description, pub_date, *,
                wait_timeout_s=600, poll_s=15) -> str:
        self._call("abs.publish")
        if self._fails(filename, "error_before_upload"):
            raise AbsError("GET /api/libraries returned 502")
        ep = self._visible(podcast, filename)
        if ep is None:
            assert Path(mp3).read_bytes().startswith(b"ID3")
            item_id = filename.rsplit("-", 1)[1].removesuffix(".mp3")
            late = timedelta(seconds=self.w.narration.items[item_id].script.late_s)
            self.episodes.append(Episode(f"ep-{len(self.episodes) + 1}", podcast, filename,
                                         self.w.clock.now() + late))
            self.w.tick("abs.publish:uploaded")
            if self._fails(filename, "error_after_upload"):
                raise AbsError("PATCH /api/podcasts/x/episode/y returned 500")
            ep = self._wait(podcast, filename, wait_timeout_s)
        ep.title, ep.description, ep.pub_date = title, description, pub_date
        return ep.id


class FakePush:
    def __init__(self, world: "World"):
        self.w = world
        self.sent: list[tuple[str, str, str]] = []

    def send(self, title: str, body: str, priority: str = "default") -> None:
        self.w.tick("push.send")
        self.sent.append((title, body, priority))


@dataclass
class World:
    root: Path
    start: datetime = START
    settings: Settings = field(default_factory=Settings)
    sources: list[Source] = field(default_factory=lambda: [source()])

    def __post_init__(self):
        self.clock = Clock(self.start)
        self.record = Record(self.root / "record.db")
        self.narration = FakeNarration(self)
        self.sessions = FakeSessions(self)
        self.abs = FakeAbs(self)
        self.push = FakePush(self)
        self.feeds: dict[str, list[tuple[str, str, date]]] = {s.feed: [] for s in self.sources}
        self.broken_feeds: dict[str, str] = {}  # feed URL -> the page served instead
        self.scripts: dict[str, Script] = {}
        self.titles: dict[str, str] = {}
        self.calls = 0
        self.crash_at: int | None = None
        self.crash_on: str | None = None

    def tick(self, name: str) -> None:
        self.calls += 1
        if self.calls == self.crash_at or name == self.crash_on:
            self.crash_at = self.crash_on = None
            raise Crash(name)

    def add_article(self, n: int, *, published: date = date(2026, 10, 1), src: Source | None = None,
                    script: Script | None = None) -> str:
        src = src or self.sources[0]
        url = f"https://example.test/{src.id}/a{n}"
        self.titles[url] = f"Article {n}"
        self.feeds[src.feed].append((url, f"Article {n}", published))
        self.scripts[url] = script or Script()
        return url

    def urls(self) -> list[str]:
        return list(self.scripts)

    def url_of(self, episode: Episode) -> str:
        return self.narration.items[episode.filename.rsplit("-", 1)[1].removesuffix(".mp3")].url

    def fetch_text(self, url: str) -> str:
        self.tick("fetch_text")
        if url in self.broken_feeds:
            return self.broken_feeds[url]
        if url not in self.feeds:
            raise FetchError(f"GET {url} returned 404")
        return feed_xml(self.feeds[url])

    def article_assistant(self, url: str) -> tuple[str, dict]:
        self.tick("article_assistant")
        if self.scripts[url].fetch == "error":
            raise FetchError("article_assistant exited 1")
        return f"Text of {url}.", {"url": url, "title": self.titles[url],
                                   "date_published": "2026-10-01", "authors": ["A. Writer"]}

    def next_night(self) -> None:
        nxt = datetime.combine(self.clock.now().date() + timedelta(days=1), time(1), tzinfo=NY)
        self.clock.t = nxt.astimezone(UTC)

    def deps(self, **overrides) -> Deps:
        kw = dict(record=self.record, sources=self.sources, settings=self.settings,
                  fetch_text=self.fetch_text, article_assistant=self.article_assistant,
                  narration=self.narration, handoff_dir=self.root / "handoff", abs=self.abs,
                  push=self.push, now=self.clock.now, sleep=self.clock.sleep,
                  record_dir=self.root, handoff=self.sessions)
        kw.update(overrides)
        return Deps(**kw)

    def run(self, **kw) -> NightReport:
        return run_night(self.deps(), **kw)
