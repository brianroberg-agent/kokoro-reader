"""Every push text and the morning summary. Plain text, built from values; no I/O."""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # night imports this module for its push texts
    from narration_pipeline.night import NightReport

_URL_TAIL = re.compile(r"(https?://[^\s?#]+)[?#]\S*")


def _plain(text: str) -> str:
    """Cut the query string and fragment off any URL in the text, where a token could ride."""
    return _URL_TAIL.sub(r"\1", text)


def hold_message(row, reasons: list[str]) -> tuple[str, str]:
    return f"Held: {row['title']}", _plain("\n".join(reasons))


def discovery_failed_message(source_name: str, reason: str) -> tuple[str, str]:
    return f"Discovery failed: {source_name}", _plain(reason)


def summary_text(report: "NightReport") -> str | None:
    queued = sum(report.discovered.values())
    if not (report.results or report.discovery_errors or report.mac_unreachable
            or report.stopped_at_cutoff or queued):
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
    lines += [f"Discovery failed: {e}" for e in report.discovery_errors]
    if queued:
        lines.append(f"{queued} new articles queued.")
    return "\n".join(lines)
