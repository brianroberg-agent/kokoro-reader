"""Every push text and the morning summary. Plain text, built from values; no I/O."""

import re
from urllib.parse import urlsplit
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # night imports this module for its push texts
    from narration_pipeline.night import NightReport

_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_TRAILING = ").,;:'\"]>"


def _rebuild(match: re.Match) -> str:
    raw = match.group(0)
    tail = ""
    while raw and raw[-1] in _TRAILING:
        raw, tail = raw[:-1], raw[-1] + tail
    try:
        parts = urlsplit(raw)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "[link removed]" + tail
    if not host:
        return "[link removed]" + tail
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}{':' + str(port) if port else ''}{parts.path}{tail}"


def plain(text: str) -> str:
    """Rebuild every URL in the text as scheme://host[:port]/path: no userinfo, query or
    fragment, where a secret could ride. Every outgoing text goes through this."""
    return _URL.sub(_rebuild, text)


def hold_message(row, reasons: list[str]) -> tuple[str, str]:
    return f"Held: {row['title']}", plain("\n".join(reasons))


def discovery_failed_message(source_name: str, reason: str) -> tuple[str, str]:
    return f"Discovery failed: {source_name}", plain(reason)


# What stopped the night, by NightReport.stopped_by: each push's title, and the
# summary's and the run's line.
STOPPED = {"login": "Claude login failed", "api": "Claude API unreachable",
           "service": "Narration session service not running"}


def stopped_message(category: str, detail: str) -> tuple[str, str]:
    return STOPPED[category], plain(detail)


def summary_text(report: "NightReport") -> str | None:
    queued = sum(report.discovered.values())
    if not (report.results or report.discovery_errors or report.mac_unreachable
            or report.stopped_at_cutoff or report.stopped_by or queued):
        return None
    lines = []
    by_status = {}
    for r in report.results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    counts = [(by_status.get("published", 0), "published"),
              (by_status.get("published_with_notes", 0), "with notes"),
              (by_status.get("held", 0), "held"),
              (by_status.get("failed", 0), "failed")]
    line = ", ".join(f"{n} {label}" for n, label in counts if n)
    if line:
        lines.append(line)
    lines += [f"Held: {r.title}" for r in report.results if r.status == "held"]
    if report.results:
        claude = sum(r.cost_usd for r in report.results)
        gemini = sum(r.gemini_usd for r in report.results)
        lines.append(f"Claude ${claude:.2f}, Gemini about ${gemini:.2f}")
    if report.mac_unreachable:
        lines.append("The Mac wasn't reachable.")
    if report.stopped_at_cutoff:
        lines.append("Stopped at the 06:00 cutoff.")
    if report.stopped_by:
        lines.append(f"Stopped: {STOPPED[report.stopped_by]}.")
    lines += [f"Discovery failed: {e}" for e in report.discovery_errors]
    if queued:
        lines.append(f"{queued} new article{'' if queued == 1 else 's'} queued.")
    return plain("\n".join(lines))
