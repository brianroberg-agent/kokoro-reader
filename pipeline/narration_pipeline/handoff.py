"""File handoff between the runner and the session service."""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from narration_pipeline.session_service import HEARTBEAT

class HandoffError(Exception):
    pass


SUBDIRS = ("requests", "running", "results")


def write_request(handoff: Path, item_id: str, agent_token: str, text_rules: list[str],
                  budget_usd: float, timeout_s: int) -> None:
    handoff.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        d = handoff / sub
        d.mkdir(mode=0o700, exist_ok=True)
        d.chmod(0o700)
    body = {"item_id": item_id, "agent_token": agent_token, "text_rules": list(text_rules),
            "budget_usd": budget_usd, "timeout_s": timeout_s}
    tmp = handoff / "requests" / f".{item_id}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(body))
        os.replace(tmp, handoff / "requests" / f"{item_id}.json")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def wait_result(handoff: Path, item_id: str, timeout_s: float, poll_s: float = 5) -> dict | None:
    path = handoff / "results" / f"{item_id}.json"
    deadline = time.monotonic() + timeout_s
    while True:
        unreadable = False
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except ValueError:  # partial or corrupt: maybe not fully written yet
                unreadable = True
            else:
                path.unlink()
                return data
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if unreadable:
                raise HandoffError(f"unreadable result for {item_id}")
            return None
        time.sleep(min(poll_s, remaining))


def withdraw_request(handoff: Path, item_id: str) -> None:
    """Remove a request the service hasn't taken yet, so no session runs later on a work
    item the runner gave up on. One the service took already is left to it."""
    (handoff / "requests" / f"{item_id}.json").unlink(missing_ok=True)


def heartbeat_age(handoff: Path, now: datetime) -> float | None:
    """Seconds since the session service last touched its heartbeat; None if it never did."""
    try:
        return now.timestamp() - (handoff / HEARTBEAT).stat().st_mtime
    except FileNotFoundError:
        return None
