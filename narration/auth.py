"""Bearer-token gate in front of the whole app.

The gate matches absolute request paths (`/health`, `/runner/...`, `/agent/...`), so
the server must not be run with `--root-path`.
"""
from __future__ import annotations

import asyncio
import hmac
import json


def _has_space_or_control(text: str) -> bool:
    # Python's isspace() covers NBSP and NEL; isprintable() is False for control characters.
    return any(c.isspace() or not c.isprintable() for c in text)


def _token(values) -> str | None:
    """The token from the `Authorization` header values a request carries (str or bytes).

    Exactly one header is accepted: Starlette's `Headers` would read the first of two
    and a dict the last, so a request carrying two is refused rather than guessed at.
    The scheme is case-insensitive. Spaces and tabs around the token are dropped (RFC 7235
    allows more than one space after the scheme); a token with any whitespace, NBSP, NEL or
    control character inside it is refused.
    """
    if len(values) != 1:
        return None
    value = values[0]
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    if not isinstance(value, str) or value[:7].lower() != "bearer ":
        return None
    token = value[7:].strip(" \t")
    if not token or _has_space_or_control(token):
        return None
    return token


def bearer(headers) -> str | None:
    """The bearer token from a Starlette or MCP headers mapping, or None."""
    if not headers:
        return None
    pairs = headers.items() if hasattr(headers, "items") else headers
    return _token([v for k, v in pairs if str(k).lower() == "authorization"])


def _bytes(token: str) -> bytes:
    # hmac.compare_digest on str raises TypeError for non-ASCII input, which would
    # turn a bad header into a 500 instead of a 401; compare bytes instead.
    return token.encode("utf-8", "surrogatepass")


def _check_runner_token(token) -> None:
    if not isinstance(token, str):
        raise ValueError(f"the runner token must be a str, not {type(token).__name__}")
    if not token.strip():
        raise ValueError("the runner token must not be blank")
    if token != token.strip():
        raise ValueError("the runner token must not have leading or trailing whitespace")
    if not token.isascii():
        raise ValueError("the runner token must be ASCII")
    if _has_space_or_control(token):
        raise ValueError("the runner token must not contain whitespace or control characters")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


class BearerAuth:
    def __init__(self, app, runner_token: str, store):
        _check_runner_token(runner_token)  # a bad value fails at boot, not as a permanent 401
        self.app, self.store = app, store
        self._runner_bytes = _bytes(runner_token)

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            return await self.app(scope, receive, send)
        if kind not in ("http", "websocket"):
            raise RuntimeError(f"unsupported ASGI scope type: {kind!r}")
        status = await self._refusal(scope)
        if status is None:
            return await self.app(scope, receive, send)
        if kind == "websocket":
            # Closed before accept: the server answers the handshake with HTTP 403.
            return await send({"type": "websocket.close", "code": 1008})
        if status == 401:
            return await _respond(send, 401, {"error": "unauthorized"},
                                  [(b"www-authenticate", b"Bearer"), (b"cache-control", b"no-store")])
        return await _respond(send, 404, {"error": "not found"})

    async def _refusal(self, scope) -> int | None:
        """None to let the request through, else the status to refuse it with."""
        path = scope.get("path", "")
        if path == "/health":
            return None
        if _under(path, "/runner"):
            gate = self._runner_ok
        elif _under(path, "/agent"):
            gate = self._agent_ok
        else:
            return 404
        token = _token([v for k, v in scope.get("headers", []) if k.lower() == b"authorization"])
        return None if token is not None and await gate(token) else 401

    async def _runner_ok(self, token: str) -> bool:
        return hmac.compare_digest(_bytes(token), self._runner_bytes)

    async def _agent_ok(self, token: str) -> bool:
        # The lookup reads a file under a lock: keep it off the event loop.
        return await asyncio.to_thread(self.store.item_for_token, token) is not None


async def _respond(send, status, body, headers=()):
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(data)).encode()), *headers]})
    await send({"type": "http.response.body", "body": data})
