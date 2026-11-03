"""The clock: run the night at start_at, send the summary at summary_at."""

import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from narration_pipeline.messages import plain, summary_text
from narration_pipeline.night import Deps, NightReport, RunInProgress, run_night

log = logging.getLogger(__name__)


def next_at(now: datetime, at: time, tz: str) -> datetime:
    """The first `at` o'clock in `tz` strictly after `now`, whatever zone `now` is in."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    candidate = datetime.combine(local.date(), at, tzinfo=zone)
    if candidate.timestamp() <= now.timestamp():  # compare instants: aware datetimes in one zone ignore fold
        candidate = datetime.combine(local.date() + timedelta(days=1), at, tzinfo=zone)
    return candidate


def _sleep_until(target: datetime, now: Callable[[], datetime], sleep: Callable[[float], None]) -> None:
    wait = target.timestamp() - now().timestamp()
    if wait > 0:
        sleep(wait)


def schedule_loop(make_deps: Callable[[], Deps], *, now: Callable[[], datetime],
                  sleep: Callable[[float], None], push, run=run_night, nights: int | None = None) -> None:
    deps = make_deps()
    s = deps.settings
    done = 0
    while nights is None or done < nights:
        done += 1
        start = next_at(now(), s.start_at, s.tz)
        log.info("next run at %s", start.isoformat())
        _sleep_until(start, now, sleep)
        started = now()
        try:
            report: NightReport | None = run(deps)
        except Exception as e:
            if isinstance(e, RunInProgress):
                log.warning("skipping tonight: %s", e)
            else:
                log.exception("the night's run failed")
                push.send("Narration run failed", plain(f"{type(e).__name__}: {e}"))
            continue
        _sleep_until(next_at(started, s.summary_at, s.tz), now, sleep)
        text = summary_text(report)
        if text is not None:
            push.send(f"Narration: {now().astimezone(ZoneInfo(s.tz)).date().isoformat()}", text)
