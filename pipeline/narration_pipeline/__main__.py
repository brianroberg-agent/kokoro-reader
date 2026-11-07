"""python -m narration_pipeline run | schedule [--nights N] | status | abs-check"""

import argparse
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from narration_pipeline import schedule
from narration_pipeline.abs import Audiobookshelf, abs_check
from narration_pipeline.config import load_settings, load_sources
from narration_pipeline.fetch import fetch_text, run_article_assistant
from narration_pipeline.messages import STOPPED, plain
from narration_pipeline.narration_client import NarrationClient
from narration_pipeline.night import Deps, NightReport, NotRunnable, RunInProgress, run_night
from narration_pipeline.push import Push
from narration_pipeline.record import Record

ABS_VARS = ("AUDIOBOOKSHELF_URL", "AUDIOBOOKSHELF_API_KEY")
REQUIRED = ("NARRATION_URL", "NARRATION_RUNNER_TOKEN", *ABS_VARS, "NTFY_TOPIC_URL", "NTFY_TOKEN")


def require(env: Mapping[str, str], names: tuple[str, ...]) -> dict[str, str]:
    """The named variables; a missing or empty one stops startup, naming it (never a value)."""
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise SystemExit(f"missing environment variable: {', '.join(missing)}")
    return {n: env[n] for n in names}


def make_deps(env: Mapping[str, str]) -> Deps:
    v = require(env, REQUIRED)
    data = Path(env.get("PIPELINE_DATA", "/data"))
    config = Path(env.get("PIPELINE_CONFIG", "/app/sources.toml"))
    checkout = Path(env.get("ARTICLE_ASSISTANT_DIR", "/opt/article-assistant"))
    text = config.read_text()
    settings = load_settings(text)
    return Deps(
        record=Record(data / "record.sqlite"),
        sources=load_sources(text, config.parent),
        settings=settings,
        fetch_text=fetch_text,
        article_assistant=lambda url: run_article_assistant(url, checkout),
        narration=NarrationClient(v["NARRATION_URL"], v["NARRATION_RUNNER_TOKEN"]),
        handoff_dir=Path(env.get("HANDOFF_DIR", "/handoff")),
        abs=Audiobookshelf(v["AUDIOBOOKSHELF_URL"], v["AUDIOBOOKSHELF_API_KEY"]),
        push=Push(v["NTFY_TOPIC_URL"], v["NTFY_TOKEN"]),
        now=lambda: datetime.now(ZoneInfo(settings.tz)),
        sleep=time.sleep,
        record_dir=data,
    )


def report_text(report: NightReport, dry_run: bool) -> str:
    lines = []
    for r in report.results:
        lines.append(f"{r.status}  {r.dry_run_action or r.outcome or '-'}  {r.title}")
        lines += ["    " + plain(reason).replace("\n", "\n    ") for reason in r.reasons]
        if dry_run and r.notes:
            lines.append("    " + r.notes.replace("\n", "\n    "))
    lines += [f"discovery failed: {e}" for e in report.discovery_errors]
    if report.mac_unreachable:
        lines.append("the Mac wasn't reachable")
    if report.stopped_at_cutoff:
        lines.append("stopped at the cutoff")
    if report.stopped_by:
        lines.append(f"stopped: {STOPPED[report.stopped_by]}")
    return "\n".join(lines) or "nothing to do"


def status_text(counts: list[tuple[str, int]], recent: list[tuple[str, str, str]]) -> str:
    lines = [f"{status}: {n}" for status, n in counts]
    lines += ["", "last updated:"] + [f"{updated}  {status}  {title}" for updated, status, title in recent]
    return "\n".join(lines)


def _status(data: Path) -> str:
    path = data / "record.sqlite"
    if not path.exists():
        return f"no record yet at {path}"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    counts = conn.execute("SELECT status, COUNT(*) FROM articles GROUP BY status ORDER BY status").fetchall()
    recent = conn.execute("SELECT updated_at, status, title FROM articles "
                          "ORDER BY updated_at DESC LIMIT 10").fetchall()
    conn.close()
    return status_text(counts, recent)


def nights_arg(value: str) -> int:
    """--nights N, a whole number from 1. With 0 the loop would return at once, and compose's
    restart policy would start the container again and again without a night's run."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--nights must be a whole number, not {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"--nights must be at least 1, not {n}")
    return n


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         deps_factory: Callable[[Mapping[str, str]], Deps] = make_deps) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="narration_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--only", metavar="URL")
    sched = sub.add_parser("schedule")
    sched.add_argument("--nights", type=nights_arg, metavar="N",
                       help="run N nights (each with its summary), then exit; default: until stopped")
    sub.add_parser("status")
    check = sub.add_parser("abs-check")
    check.add_argument("--podcast", required=True)
    check.add_argument("--mp3", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2"):  # one INFO line per MCP call buries the run's own output
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.command == "abs-check":
        v = require(env, ABS_VARS)
        print(abs_check(Audiobookshelf(*(v[n] for n in ABS_VARS)), args.podcast, args.mp3))
    elif args.command == "status":
        print(_status(Path(env.get("PIPELINE_DATA", "/data"))))
    elif args.command == "run":
        deps = deps_factory(env)
        try:
            report = run_night(deps, dry_run=args.dry_run, only=args.only)
        except RunInProgress:
            raise SystemExit("another run is in progress") from None
        except NotRunnable as e:  # --only: not discovered, not queued or failed, or no Mac
            raise SystemExit(str(e)) from None
        print(report_text(report, args.dry_run))
    else:
        deps = deps_factory(env)
        schedule.schedule_loop(lambda: deps, now=deps.now, sleep=deps.sleep, push=deps.push, nights=args.nights)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
