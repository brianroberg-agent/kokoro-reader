"""Source list and run settings, read from one TOML file."""

import tomllib
from dataclasses import dataclass, fields
from datetime import date, time


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


_SOURCE_KEYS = {"id", "name", "podcast", "feed", "listing", "extractor", "start", "text_rules"}
_REQUIRED = _SOURCE_KEYS - {"listing", "text_rules"}


def load_sources(text: str) -> list[Source]:
    out = []
    for entry in tomllib.loads(text).get("source", []):
        unknown = set(entry) - _SOURCE_KEYS
        if unknown:
            raise ValueError(f"unknown source key(s): {', '.join(sorted(unknown))}")
        missing = _REQUIRED - set(entry)
        if missing:
            raise ValueError(f"missing source key(s): {', '.join(sorted(missing))}")
        start = entry["start"]
        if set(start) != {"edition", "date"}:
            raise ValueError("start needs exactly edition and date")
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
                text_rules=tuple(entry.get("text_rules", ())),
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
    return Settings(**values)
