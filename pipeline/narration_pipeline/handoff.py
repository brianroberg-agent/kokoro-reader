"""File handoff between the runner and the session service."""

import json
import os
import time
from pathlib import Path

SUBDIRS = ("requests", "running", "results")


def write_request(handoff: Path, item_id: str, agent_token: str, text_rules: list[str],
                  budget_usd: float, timeout_s: int) -> None:
    for sub in SUBDIRS:
        (handoff / sub).mkdir(parents=True, exist_ok=True)
    body = {"item_id": item_id, "agent_token": agent_token, "text_rules": list(text_rules),
            "budget_usd": budget_usd, "timeout_s": timeout_s}
    tmp = handoff / "requests" / f".{item_id}.tmp"
    tmp.write_text(json.dumps(body))
    os.replace(tmp, handoff / "requests" / f"{item_id}.json")


def wait_result(handoff: Path, item_id: str, timeout_s: float, poll_s: float = 5) -> dict | None:
    path = handoff / "results" / f"{item_id}.json"
    deadline = time.monotonic() + timeout_s
    while True:
        if path.exists():
            data = json.loads(path.read_text())
            path.unlink()
            return data
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_s, remaining))
