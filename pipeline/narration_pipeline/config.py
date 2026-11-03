"""Source list and run settings, read from one TOML file."""

import math
import tomllib
from dataclasses import dataclass, fields
from datetime import date, time
from pathlib import Path

# The session service's limits on a request: a setting outside them would fail every
# article's session, so loading refuses it.
from narration_pipeline.session_service import (MAX_BUDGET_USD, MAX_RULE_CHARS, MAX_RULES, MAX_TIMEOUT_S,
                                                MIN_TIMEOUT_S)


# article-assistant picks its extractor from the article's URL and has no option for it:
# a source's `extractor` documents which one its site needs, checked against these names.
EXTRACTORS = frozenset({"new-atlantis"})


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    podcast: str
    feed: str
    listing: str | None
    extractor: str
    start_edition: int
    start_date: date
    text_rules: tuple[str, ...]
    cover: Path | None = None  # the podcast's cover image; sources.toml requires one


MAX_CAP = 20  # articles a night; the spec's 5 is a starting point, tuned from the cost review


@dataclass(frozen=True)
class Settings:
    cap: int = 5
    start_at: time = time(1, 0)
    mac_wait_until: time = time(5, 0)
    no_new_after: time = time(6, 0)
    summary_at: time = time(7, 0)
    tz: str = "America/New_York"
    failed_nights_to_hold: int = 3
    session_budget_usd: float = 2.00
    session_timeout_s: int = 5400
    gemini_usd_per_check: float = 0.03


_SOURCE_KEYS = {"id", "name", "podcast", "feed", "listing", "extractor", "start", "text_rules", "cover"}
_REQUIRED = _SOURCE_KEYS - {"listing", "text_rules"}


def _check_rules(source_id: str, rules) -> tuple[str, ...]:
    if not (isinstance(rules, list) and len(rules) <= MAX_RULES
            and all(isinstance(r, str) and len(r) <= MAX_RULE_CHARS for r in rules)):
        raise ValueError(f"{source_id}: text_rules must be a list of at most {MAX_RULES} strings "
                         f"of at most {MAX_RULE_CHARS} characters")
    return tuple(rules)


def check_settings(s: "Settings") -> "Settings":
    """The settings, or ValueError naming the first one outside its limits: the session
    service's for the timeout and the budget, and 1 to MAX_CAP articles a night."""
    if not (isinstance(s.cap, int) and not isinstance(s.cap, bool) and 1 <= s.cap <= MAX_CAP):
        raise ValueError(f"cap must be an integer from 1 to {MAX_CAP}")
    timeout, budget = s.session_timeout_s, s.session_budget_usd
    if not (isinstance(timeout, int) and not isinstance(timeout, bool) and MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S):
        raise ValueError(f"session_timeout_s must be an integer from {MIN_TIMEOUT_S} to {MAX_TIMEOUT_S}")
    if not (isinstance(budget, (int, float)) and not isinstance(budget, bool) and math.isfinite(budget)
            and 0 < budget <= MAX_BUDGET_USD):
        raise ValueError(f"session_budget_usd must be a number above 0 and at most {MAX_BUDGET_USD:.2f}")
    return s


def load_sources(text: str, base: Path | None = None) -> list[Source]:
    """The [[source]] entries. With base (the config file's folder), each relative
    cover path is resolved against it, and a cover that isn't a file is refused."""
    out = []
    for entry in tomllib.loads(text).get("source", []):
        unknown = set(entry) - _SOURCE_KEYS
        if unknown:
            raise ValueError(f"unknown source key(s): {', '.join(sorted(unknown))}")
        missing = _REQUIRED - set(entry)
        if missing:
            raise ValueError(f"missing source key(s): {', '.join(sorted(missing))}")
        if entry["extractor"] not in EXTRACTORS:
            raise ValueError(f"{entry['id']}: unknown extractor {entry['extractor']!r} "
                             f"(article-assistant has {', '.join(sorted(EXTRACTORS))})")
        start = entry["start"]
        if set(start) != {"edition", "date"}:
            raise ValueError("start needs exactly edition and date")
        cover = Path(entry["cover"])
        if base is not None:
            cover = cover if cover.is_absolute() else base / cover
            if not cover.is_file():
                raise ValueError(f"{entry['id']}: cover {cover} not found")
        out.append(
            Source(
                id=entry["id"],
                name=entry["name"],
                podcast=entry["podcast"],
                feed=entry["feed"],
                listing=entry.get("listing"),
                extractor=entry["extractor"],
                start_edition=start["edition"],
                start_date=date.fromisoformat(start["date"]),
                text_rules=_check_rules(entry["id"], entry.get("text_rules", [])),
                cover=cover,
            )
        )
    return out


def load_settings(text: str) -> Settings:
    table = tomllib.loads(text).get("settings", {})
    known = {f.name: f for f in fields(Settings)}
    unknown = set(table) - set(known)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    values = {
        k: time.fromisoformat(v) if known[k].type is time else v for k, v in table.items()
    }
    return check_settings(Settings(**values))
