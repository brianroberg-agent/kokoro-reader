"""ntfy notifications. Best effort: a failure is logged without the token or the body."""

import base64
import logging

import requests

log = logging.getLogger(__name__)


def header_safe(value: str) -> str:
    """A latin-1-safe header value; anything else is RFC 2047 base64."""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


class Push:
    def __init__(self, topic_url: str, token: str):
        self._url = topic_url
        self._token = token

    def send(self, title: str, body: str, priority: str = "default") -> None:
        headers = {"Authorization": f"Bearer {self._token}", "Title": header_safe(title),
                   "Priority": priority}
        try:
            resp = requests.post(self._url, data=body.encode(), headers=headers, timeout=10)
            if resp.status_code >= 300:
                log.warning("ntfy returned %s", resp.status_code)
        except Exception as e:  # best effort; the message may echo the token, so log the type only
            log.warning("ntfy send failed: %s", type(e).__name__)
