import asyncio
import contextlib
import threading

import pytest
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from narration.auth import BearerAuth, bearer
from narration.workitems import WorkItemStore

RUNNER = {"Authorization": "Bearer runner-secret"}


def _app(tmp_path):
    store = WorkItemStore(tmp_path)
    ok = lambda request: PlainTextResponse("ok")
    inner = Starlette(routes=[Route("/health", ok), Route("/runner/x", ok), Route("/agent/x", ok)])
    return TestClient(BearerAuth(inner, "runner-secret", store)), store


def _call(app, scope):
    """Drive an ASGI app once; return the messages it sent."""
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(app(scope, receive, send))
    return sent


class _Inner:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope["path"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})


class _Store:
    def __init__(self):
        self.threads = []

    def item_for_token(self, token):
        self.threads.append(threading.get_ident())
        return "a1b2c3d4e5f6" if token == "item-token" else None


def test_health_is_open(tmp_path):
    client, _ = _app(tmp_path)
    assert client.get("/health").status_code == 200


def test_runner_needs_the_runner_token(tmp_path):
    client, store = _app(tmp_path)
    _, item_token = store.open("x", {})
    assert client.get("/runner/x").status_code == 401
    assert client.get("/runner/x", headers={"Authorization": f"Bearer {item_token}"}).status_code == 401
    assert client.get("/runner/x", headers={"Authorization": "Bearer runner-secret"}).status_code == 200


def test_agent_needs_a_live_item_token(tmp_path):
    client, store = _app(tmp_path)
    item, token = store.open("x", {})
    assert client.get("/agent/x", headers={"Authorization": "Bearer runner-secret"}).status_code == 401
    assert client.get("/agent/x", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    store.revoke(item)
    assert client.get("/agent/x", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_unknown_paths_are_404(tmp_path):
    client, _ = _app(tmp_path)
    assert client.get("/other", headers={"Authorization": "Bearer runner-secret"}).status_code == 404


def test_bearer_parsing():
    assert bearer({"authorization": "Bearer abc"}) == "abc"
    assert bearer({"Authorization": "bearer abc"}) == "abc"
    assert bearer({"authorization": "Basic abc"}) is None
    assert bearer({}) is None and bearer(None) is None


def test_bearer_without_a_token_is_none():
    assert bearer({"authorization": "Bearer"}) is None
    assert bearer({"authorization": "Bearer "}) is None
    assert bearer({"authorization": "Bearer    "}) is None
    assert bearer({"authorization": ""}) is None


def test_bearer_takes_a_starlette_headers_object():
    assert bearer(Headers({"Authorization": "Bearer abc"})) == "abc"


def test_bearer_drops_spaces_and_tabs_around_the_token_only():
    # RFC 7235 allows more than one space after the scheme, so the token is what is left.
    assert bearer({"authorization": "Bearer  x"}) == "x"
    assert bearer({"authorization": "Bearer x  "}) == "x"
    assert bearer({"authorization": "Bearer \tx\t"}) == "x"
    # Anything else that is whitespace or a control character, anywhere, refuses the header.
    for value in ("Bearer x\n", "Bearer \nx", "Bearer \xa0x", "Bearer x\xa0", "Bearer x\x85",
                  "Bearer x\r", "Bearer x\x00", "Bearer x\x1f", "Bearer x\x7f", "Bearer x\x9f",
                  "Bearer x y", "Bearer x\ty", "Bearer x ", "Bearer\tx"):
        assert bearer({"authorization": value}) is None, repr(value)


def test_bearer_reads_bytes_and_non_string_values():
    assert bearer({"authorization": b"Bearer abc"}) == "abc"
    assert bearer({"authorization": 7}) is None
    assert bearer({"authorization": None}) is None


def test_bearer_refuses_two_authorization_headers():
    two = Headers(raw=[(b"authorization", b"Bearer one"), (b"authorization", b"Bearer one")])
    assert bearer(two) is None
    assert bearer([("Authorization", "Bearer a"), ("authorization", "Bearer b")]) is None
    assert bearer([("Authorization", "Bearer a"), ("accept", "*/*")]) == "a"


def test_failures_are_401_json(tmp_path):
    client, _ = _app(tmp_path)
    for path in ("/runner/x", "/agent/x"):
        r = client.get(path)
        assert r.status_code == 401
        assert r.headers["content-type"] == "application/json"
        assert r.json() == {"error": "unauthorized"}
        assert int(r.headers["content-length"]) == len(r.content)


def test_401_says_how_to_authenticate_and_is_not_cacheable(tmp_path):
    client, _ = _app(tmp_path)
    for path in ("/runner/x", "/agent/x"):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            r = client.get(path, headers=headers)
            assert r.status_code == 401
            assert r.headers["www-authenticate"] == "Bearer"
            assert r.headers["cache-control"] == "no-store"


def test_unknown_path_is_404_json_even_without_a_token(tmp_path):
    client, _ = _app(tmp_path)
    r = client.get("/other")
    assert r.status_code == 404
    assert r.headers["content-type"] == "application/json"
    assert r.json() == {"error": "not found"}


def test_a_non_ascii_token_is_401_not_500(tmp_path):
    # hmac.compare_digest on str raises TypeError for non-ASCII; it must compare bytes.
    client, _ = _app(tmp_path)
    for token in ("ü", "runner-secrét", "日本語"):
        for path in ("/runner/x", "/agent/x"):
            r = client.get(path, headers={"Authorization": f"Bearer {token}".encode("utf-8")})
            assert r.status_code == 401, (path, token)


def test_header_name_case_and_scheme_case_are_tolerated(tmp_path):
    client, _ = _app(tmp_path)
    for name in ("authorization", "AUTHORIZATION", "Authorization"):
        assert client.get("/runner/x", headers={name: "bearer runner-secret"}).status_code == 200
        assert client.get("/runner/x", headers={name: "BEARER runner-secret"}).status_code == 200


def test_malformed_headers_are_401(tmp_path):
    client, store = _app(tmp_path)
    _, token = store.open("x", {})
    for value in ("Bearer", "Bearer ", "Basic runner-secret", "runner-secret",
                  "Bearer runner-secret extra", "Bearer runner secret", "Bearer\trunner-secret x"):
        assert client.get("/runner/x", headers={"Authorization": value}).status_code == 401, value
    for value in ("Bearer", "Bearer ", f"Token {token}", token, f"Bearer {token} extra"):
        assert client.get("/agent/x", headers={"Authorization": value}).status_code == 401, value


def test_whitespace_and_control_characters_in_the_header_are_401():
    # Raw ASGI: a server would not pass a newline, but the gate must not depend on that.
    inner, store = _Inner(), _Store()
    app = BearerAuth(inner, "runner-secret", store)

    def status(path, value):
        scope = {"type": "http", "path": path, "headers": [(b"authorization", value)]}
        return _call(app, scope)[0]["status"]

    for value in (b"Bearer runner-secret\n", b"Bearer \xa0runner-secret", b"Bearer runner-secret\x85",
                  b"Bearer runner-secret\x00", b"Bearer runner secret"):
        assert status("/runner/x", value) == 401, value
    for value in (b"Bearer item-token\n", b"Bearer \xa0item-token", b"Bearer item-token\x85"):
        assert status("/agent/x", value) == 401, value
    # Spaces and tabs around the token are dropped.
    assert status("/runner/x", b"Bearer  runner-secret ") == 200
    assert status("/agent/x", b"Bearer \titem-token") == 200


def test_two_authorization_headers_are_401(tmp_path):
    client, store = _app(tmp_path)
    _, token = store.open("x", {})
    good_runner, good_item = "Bearer runner-secret", f"Bearer {token}"
    for path, good in (("/runner/x", good_runner), ("/agent/x", good_item)):
        for pair in ([good, good], [good, "Bearer wrong"], ["Bearer wrong", good]):
            headers = [("Authorization", pair[0]), ("authorization", pair[1])]
            assert client.get(path, headers=headers).status_code == 401, (path, pair)
        assert client.get(path, headers=[("Authorization", good)]).status_code == 200
    # /health is open whatever the headers say.
    assert client.get("/health", headers=[("Authorization", "a"), ("Authorization", "b")]).status_code == 200


def test_paths_that_only_look_like_ours_are_404_from_the_gate(tmp_path):
    # The inner app answers 200 on every one of these, so only the gate can produce the 404.
    ok = lambda request: PlainTextResponse("ok")
    paths = ["/healthz", "/health/x", "/runners/x", "/runnerx", "/agents/x", "/agentx", "/Runner/x",
             "/RUNNER/x", "/Agent/x", "/Health", "//runner/x", "//agent/x", "//health"]
    inner = Starlette(routes=[Route(p, ok) for p in paths] + [Route("/", ok)])
    store = WorkItemStore(tmp_path)
    _, item_token = store.open("x", {})
    client = TestClient(BearerAuth(inner, "runner-secret", store))
    for path in paths + ["/"]:
        url = "http://testserver" + path  # a bare "//runner/x" would be read as a host
        for headers in ({}, {"Authorization": "Bearer wrong"}, RUNNER, {"Authorization": f"Bearer {item_token}"}):
            r = client.get(url, headers=headers)
            assert r.status_code == 404, (path, headers)
            assert r.json() == {"error": "not found"}, (path, headers)


def test_runner_and_agent_without_a_trailing_slash_are_gated():
    inner, store = _Inner(), _Store()
    app = BearerAuth(inner, "runner-secret", store)

    def status(path, value):
        headers = [(b"authorization", value)] if value else []
        return _call(app, {"type": "http", "path": path, "headers": headers})[0]["status"]

    assert status("/runner", b"Bearer runner-secret") == 200
    assert status("/runner", b"Bearer nope") == 401
    assert status("/runner", None) == 401
    assert status("/agent", b"Bearer item-token") == 200
    assert status("/agent", None) == 401


def test_head_and_options_without_a_token_are_401(tmp_path):
    client, _ = _app(tmp_path)
    for path in ("/runner/x", "/agent/x"):
        assert client.head(path).status_code == 401
        assert client.options(path).status_code == 401
    # With the token the request is the inner app's: HEAD is served, OPTIONS gets its 405.
    assert client.head("/runner/x", headers=RUNNER).status_code == 200
    assert client.options("/runner/x", headers=RUNNER).status_code == 405


def test_asgi_header_list_of_bytes_is_read():
    inner, store = _Inner(), _Store()
    app = BearerAuth(inner, "runner-secret", store)

    def status(path, headers):
        return _call(app, {"type": "http", "path": path, "headers": headers})[0]["status"]

    assert status("/runner/x", [(b"authorization", b"Bearer runner-secret")]) == 200
    assert status("/agent/x", [(b"Authorization", b"Bearer item-token")]) == 200
    assert status("/agent/x", [(b"authorization", b"Bearer \xfc")]) == 401  # latin-1 u-umlaut
    assert status("/agent/x", []) == 401
    assert status("/agent/x", [(b"authorization", b"Bearer item-token"), (b"Authorization", b"Bearer item-token")]) == 401
    assert inner.calls == ["/runner/x", "/agent/x"]


def test_item_lookup_runs_off_the_event_loop(tmp_path):
    inner, store = _Inner(), _Store()
    app = BearerAuth(inner, "runner-secret", store)
    loop_thread = []

    async def main():
        loop_thread.append(threading.get_ident())

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            pass

        scope = {"type": "http", "path": "/agent/x", "headers": [(b"authorization", b"Bearer item-token")]}
        await app(scope, receive, send)

    asyncio.run(main())
    assert len(store.threads) == 1 and store.threads[0] != loop_thread[0]
    assert inner.calls == ["/agent/x"]


def test_revoking_an_item_takes_effect_on_the_next_request(tmp_path):
    client, store = _app(tmp_path)
    item, token = store.open("x", {})
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/agent/x", headers=headers).status_code == 200
    assert client.get("/agent/x", headers=headers).status_code == 200
    store.revoke(item)
    assert client.get("/agent/x", headers=headers).status_code == 401


@pytest.mark.parametrize("token, problem", [
    (" ", "blank"),
    ("", "blank"),
    ("abc\n", "leading or trailing whitespace"),
    (" abc", "leading or trailing whitespace"),
    ("abc ", "leading or trailing whitespace"),
    ("ü", "ASCII"),
    ("runner-secrét", "ASCII"),
    (b"x", "must be a str"),
    (None, "must be a str"),
    (123, "must be a str"),
    ("a b", "whitespace or control"),
    ("a\x00b", "whitespace or control"),
    ("a\x7fb", "whitespace or control"),
])
def test_a_bad_runner_token_fails_at_construction(tmp_path, token, problem):
    with pytest.raises(ValueError, match=problem):
        BearerAuth(_Inner(), token, WorkItemStore(tmp_path))


def test_a_url_safe_runner_token_is_accepted(tmp_path):
    import secrets

    BearerAuth(_Inner(), secrets.token_urlsafe(32), WorkItemStore(tmp_path))


def test_lifespan_scope_passes_through_untouched(tmp_path):
    store = WorkItemStore(tmp_path)
    got = []

    async def inner(scope, receive, send):
        got.append((scope, receive, send))

    app = BearerAuth(inner, "runner-secret", store)
    scope, receive, send = {"type": "lifespan", "path": "/anything"}, object(), object()
    asyncio.run(app(scope, receive, send))
    assert got[0][0] is scope and got[0][1] is receive and got[0][2] is send


def test_an_unknown_scope_type_is_refused_not_forwarded(tmp_path):
    inner = _Inner()
    app = BearerAuth(inner, "runner-secret", WorkItemStore(tmp_path))
    with pytest.raises(RuntimeError, match="unsupported ASGI scope type"):
        _call(app, {"type": "something-new", "path": "/agent/x", "headers": []})
    assert inner.calls == []


def test_lifespan_reaches_the_wrapped_app(tmp_path):
    store = WorkItemStore(tmp_path)
    events = []

    @contextlib.asynccontextmanager
    async def lifespan(app):
        events.append("start")
        yield
        events.append("stop")

    inner = Starlette(routes=[Route("/health", lambda request: PlainTextResponse("ok"))], lifespan=lifespan)
    with TestClient(BearerAuth(inner, "runner-secret", store)) as client:
        assert events == ["start"]
        assert client.get("/health").status_code == 200
    assert events == ["start", "stop"]


# -- websockets get the same rules as HTTP --

def _ws_client(tmp_path):
    async def hello(websocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    inner = Starlette(routes=[WebSocketRoute(p, hello) for p in ("/health", "/runner/ws", "/agent/ws", "/other/ws")])
    store = WorkItemStore(tmp_path)
    return TestClient(BearerAuth(inner, "runner-secret", store)), store


def _refused(client, path, headers=None):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(path, headers=headers or {}):
            pass
    return excinfo.value.code


def test_a_websocket_under_agent_needs_a_live_item_token(tmp_path):
    client, store = _ws_client(tmp_path)
    item, token = store.open("x", {})
    assert _refused(client, "/agent/ws") == 1008
    assert _refused(client, "/agent/ws", {"Authorization": "Bearer runner-secret"}) == 1008
    assert _refused(client, "/agent/ws", {"Authorization": "Bearer wrong"}) == 1008
    with client.websocket_connect("/agent/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
        assert ws.receive_text() == "hello"
    store.revoke(item)
    assert _refused(client, "/agent/ws", {"Authorization": f"Bearer {token}"}) == 1008


def test_a_websocket_under_runner_needs_the_runner_token(tmp_path):
    client, store = _ws_client(tmp_path)
    _, token = store.open("x", {})
    assert _refused(client, "/runner/ws") == 1008
    assert _refused(client, "/runner/ws", {"Authorization": f"Bearer {token}"}) == 1008
    with client.websocket_connect("/runner/ws", headers=RUNNER) as ws:
        assert ws.receive_text() == "hello"


def test_websocket_paths_outside_the_rules_are_refused_and_health_is_open(tmp_path):
    client, _ = _ws_client(tmp_path)
    assert _refused(client, "/other/ws", RUNNER) == 1008
    with client.websocket_connect("/health") as ws:
        assert ws.receive_text() == "hello"


def test_a_websocket_with_two_authorization_headers_is_refused():
    # TestClient takes a dict of headers, so the duplicate goes in at the ASGI level.
    inner, store = _Inner(), _Store()
    app = BearerAuth(inner, "runner-secret", store)
    both = [(b"authorization", b"Bearer runner-secret"), (b"authorization", b"Bearer runner-secret")]
    assert _call(app, {"type": "websocket", "path": "/runner/ws", "headers": both}) == [
        {"type": "websocket.close", "code": 1008}]
    assert inner.calls == []


def test_a_refused_websocket_is_closed_before_accept_and_never_reaches_the_app():
    inner, store = _Inner(), _Store()
    app = BearerAuth(inner, "runner-secret", store)
    sent = _call(app, {"type": "websocket", "path": "/agent/ws", "headers": []})
    assert sent == [{"type": "websocket.close", "code": 1008}]
    sent = _call(app, {"type": "websocket", "path": "/agent/ws", "headers": [(b"authorization", b"Bearer item-token")]})
    assert sent == []  # forwarded: the inner fake sends nothing for a websocket
    assert inner.calls == ["/agent/ws"]
