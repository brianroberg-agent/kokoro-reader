import json

import pytest
from starlette.testclient import TestClient

from narration import agent_tools
from narration.agent_tools import (ISSUE_KINDS, MAX_EDIT_TEXT, MAX_FIELD, MAX_ISSUES, MAX_NOTE,
                                   MAX_WORD, OUTCOMES, VOICE_RE, WHERE_RE, AgentTools, ToolError,
                                   _plain, _speed)
from narration.app import AGENT_INSTRUCTIONS, build_app
from narration.fakes import FakeConverter, FakeG2P, FakeTTS, FakeVerifier
from narration.runner_tools import RunnerTools
from narration.words import MAX_SUBSTITUTIONS
from narration.workitems import WorkItemStore

RUNNER = "runner-secret"
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
AGENT_TOOLS = {"get_text", "replace_text", "record", "check", "phonemes", "finish"}
RUNNER_TOOLS = {"open", "finalize", "status"}


def _build(tmp_path, tts=None, verifier=None, allowed=("testserver", "testserver:80")):
    store = WorkItemStore(tmp_path)
    g2p = FakeG2P()
    app = build_app(store, AgentTools(store, tts or FakeTTS(), verifier or FakeVerifier(), g2p),
                    RunnerTools(store, FakeConverter(), g2p), RUNNER, allowed_hosts=list(allowed))
    return app, store


@pytest.fixture
def client(tmp_path):
    app, store = _build(tmp_path)
    with TestClient(app) as c:  # runs the lifespan (both session managers) through BearerAuth
        yield c, store


def _rpc(c, path, token, method, params=None, **headers):
    return c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                  headers={**HDR, "Authorization": f"Bearer {token}", **headers})


def _call(c, path, token, name, arguments=None):
    """A tools/call over HTTP (stateless_http=True: no initialize first); its result."""
    r = _rpc(c, path, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _text(result) -> str:
    return result["content"][0]["text"]


def _ok(result) -> dict:
    assert result["isError"] is False, _text(result)
    return json.loads(_text(result))


def _tools(c, path, token) -> dict:
    r = _rpc(c, path, token, "tools/list")
    assert r.status_code == 200, r.text
    return {t["name"]: t for t in r.json()["result"]["tools"]}


# -- the HTTP layer --

def test_health(client):
    c, _ = client
    assert c.get("/health").json() == {"ok": True}  # no token needed


def test_mcp_endpoints_are_token_gated(client):
    c, store = client
    _, token = store.open("x", {})
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert c.post("/agent/mcp", json=body, headers=HDR).status_code == 401
    assert c.post("/runner/mcp", json=body, headers=HDR).status_code == 401
    # Each endpoint takes only its own kind of token.
    assert c.post("/runner/mcp", json=body, headers={**HDR, "Authorization": f"Bearer {token}"}).status_code == 401
    assert c.post("/agent/mcp", json=body, headers={**HDR, "Authorization": f"Bearer {RUNNER}"}).status_code == 401
    assert c.post("/agent/mcp", json=body, headers={**HDR, "Authorization": f"Bearer {token}"}).status_code == 200
    assert c.post("/runner/mcp", json=body, headers={**HDR, "Authorization": f"Bearer {RUNNER}"}).status_code == 200


def test_each_endpoint_lists_its_own_tools(client):
    c, store = client
    _, token = store.open("x", {})
    assert set(_tools(c, "/agent/mcp", token)) == AGENT_TOOLS
    assert set(_tools(c, "/runner/mcp", RUNNER)) == RUNNER_TOOLS


def test_download_needs_runner_token_and_a_finalized_mp3(client):
    c, store = client
    item, token = store.open("x", {})
    url = f"/runner/files/{item}/episode.mp3"
    runner = {"Authorization": f"Bearer {RUNNER}"}
    assert c.get(url).status_code == 401
    assert c.get(url, headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert c.get(url, headers=runner).status_code == 404  # no MP3 yet
    store.path(item, "episode.mp3").write_bytes(b"ID3")
    r = c.get(url, headers=runner)
    assert r.status_code == 200 and r.content == b"ID3"
    assert r.headers["content-type"] == "audio/mpeg"
    assert c.get("/runner/files/0123456789ab/episode.mp3", headers=runner).status_code == 404  # unknown id


def test_download_refuses_an_id_that_is_not_twelve_lowercase_hex(client):
    c, store = client
    runner = {"Authorization": f"Bearer {RUNNER}"}
    # A folder the store would never make, with an MP3 in it: the id check refuses it,
    # not the missing file.
    for odd in ("ABCDEF012345", "0123456789abc", "0123456789a"):
        (store.root / "items" / odd).mkdir()
        (store.root / "items" / odd / "episode.mp3").write_bytes(b"ID3")
        assert c.get(f"/runner/files/{odd}/episode.mp3", headers=runner).status_code == 404
    (store.root / "secret").mkdir()
    (store.root / "secret" / "episode.mp3").write_bytes(b"ID3")
    for path in ("/runner/files/../../secret/episode.mp3",
                 "/runner/files/%2E%2E/episode.mp3",
                 "/runner/files/..%2F..%2Fsecret/episode.mp3",
                 "/runner/files/%2E%2E%2F%2E%2E%2Fsecret/episode.mp3"):
        r = c.get(path, headers=runner)
        assert r.status_code == 404 and r.content != b"ID3", path


def test_host_not_on_allow_list_is_refused(tmp_path):
    app, store = _build(tmp_path, allowed=["mac.tailnet:8770"])
    _, token = store.open("x", {})
    with TestClient(app) as c:  # one lifespan per app: a session manager runs once
        r = _rpc(c, "/agent/mcp", token, "tools/list")
        assert r.status_code == 421  # Host check; Content-Type is valid, so 400 can't mask it
        assert _rpc(c, "/runner/mcp", RUNNER, "tools/list").status_code == 421
        assert _rpc(c, "/agent/mcp", token, "tools/list", Host="mac.tailnet:8770").status_code == 200


def test_a_cross_origin_request_is_refused(client):
    c, store = client
    _, token = store.open("x", {})
    assert _rpc(c, "/agent/mcp", token, "tools/list", Origin="http://evil.example").status_code == 403
    assert _rpc(c, "/runner/mcp", RUNNER, "tools/list", Origin="http://evil.example").status_code == 403


def test_the_agent_token_stops_working_once_finish_is_called(client):
    c, store = client
    item, token = store.open("x", {})
    hold = {"outcome": "hold", "remaining_issues": [{"kind": "other", "where": "0:00", "note": "x"}]}
    assert _ok(_call(c, "/agent/mcp", token, "finish", hold)) == {"ok": True}
    assert _rpc(c, "/agent/mcp", token, "tools/list").status_code == 401
    assert store.load_state(item)["status"] == "finished"


# -- errors: refusals reach the agent, crashes don't --

def test_engine_errors_reach_the_agent_with_their_message(tmp_path):
    class LongAudioVerifier:  # what verify_audio raises past MAX_SINGLE_CALL_MINUTES
        def check(self, audio_path, text):
            raise ValueError("This audio is 17:30 long, but reliable single-call verification "
                             "tops out at ~15 minutes. Add [BREAK] markers to the text and "
                             "regenerate the audio so verification can be chunked.")

    app, store = _build(tmp_path, verifier=LongAudioVerifier())
    _, token = store.open("x", {})
    with TestClient(app) as c:
        assert _call(c, "/agent/mcp", token, "record")["isError"] is False
        result = _call(c, "/agent/mcp", token, "check")
    assert result["isError"] is True
    assert "Add [BREAK] markers" in _text(result)


def test_refusals_reach_the_agent_with_their_message(client):
    c, store = client
    _, token = store.open("x", {})
    result = _call(c, "/agent/mcp", token, "check")  # agent_tools.ToolError
    assert result["isError"] is True and "call record first" in _text(result)
    edit = _call(c, "/agent/mcp", token, "replace_text", {"old": "nope", "new": "y"})
    assert edit["isError"] is True and "exactly once" in _text(edit)


def test_an_unexpected_exception_is_not_passed_off_as_a_refusal(tmp_path):
    class BrokenTTS:  # a bug, not a refusal: its text must not reach the agent
        def record(self, text, out_path, voice, speed):
            raise KeyError("/Users/someone/secret/path")

    app, store = _build(tmp_path, tts=BrokenTTS())
    _, token = store.open("x", {})
    with TestClient(app) as c:
        result = _call(c, "/agent/mcp", token, "record")
    assert result["isError"] is True
    assert _text(result) == "Error executing tool record"


def test_a_premature_finalize_is_refused_with_its_reason(client):
    c, store = client
    item, token = store.open("x", {})
    r = _call(c, "/runner/mcp", RUNNER, "finalize", {"item_id": item})
    assert r["isError"] is True and "still open" in _text(r)
    assert store.item_for_token(token) == item  # nothing was revoked


# -- the runner's tools --

def test_runner_open_status_and_forced_finalize(client):
    c, store = client
    opened = _ok(_call(c, "/runner/mcp", RUNNER, "open",
                       {"article_text": "Hello world.", "metadata": {"title": "T"}}))
    item, token = opened["item_id"], opened["agent_token"]
    assert _ok(_call(c, "/agent/mcp", token, "get_text"))["prepared_text"] == "Hello world."
    status = _ok(_call(c, "/runner/mcp", RUNNER, "status", {"item_id": item}))
    assert status["status"] == "open" and status["prepared_matches_recording"] is False
    forced = _ok(_call(c, "/runner/mcp", RUNNER, "finalize", {"item_id": item, "force": True}))
    assert forced["action"] == "retry" and forced["mp3"] is False
    assert _ok(_call(c, "/runner/mcp", RUNNER, "status", {"item_id": item}))["status"] == "abandoned"
    assert _rpc(c, "/agent/mcp", token, "tools/list").status_code == 401


def test_runner_refusals_and_unknown_items(client):
    c, _ = client
    bad = _call(c, "/runner/mcp", RUNNER, "open", {"article_text": "  ", "metadata": {}})
    assert bad["isError"] is True and "non-empty" in _text(bad)  # run() wraps open too
    assert _ok(_call(c, "/runner/mcp", RUNNER, "status", {"item_id": "0123456789ab"})) == {"exists": False}
    for odd in ("0123456789ab", "../../etc"):
        r = _call(c, "/runner/mcp", RUNNER, "finalize", {"item_id": odd})
        assert r["isError"] is True and "no such work item" in _text(r)


def test_a_session_from_open_to_download(client):
    c, store = client
    opened = _ok(_call(c, "/runner/mcp", RUNNER, "open",
                       {"article_text": "Hello world, again.", "metadata": {"title": "Greeting"}}))
    item, token = opened["item_id"], opened["agent_token"]
    agent = lambda name, args=None: _ok(_call(c, "/agent/mcp", token, name, args))
    assert agent("get_text")["records_left"] == 4
    assert agent("replace_text", {"old": "world, again", "new": "world , again"}) == {"ok": True}
    recorded = agent("record", {"voice": "af_heart", "speed": 1.1})
    assert recorded["round"] == 1 and recorded["speech_warnings"] == []
    assert agent("check")["precheck_warnings"] == []
    assert agent("finish", {"outcome": "clean"}) == {"ok": True}
    final = _ok(_call(c, "/runner/mcp", RUNNER, "finalize", {"item_id": item}))
    assert final["action"] == "publish" and final["mp3"] is True, final
    r = c.get(f"/runner/files/{item}/episode.mp3", headers={"Authorization": f"Bearer {RUNNER}"})
    assert r.status_code == 200 and r.content == b"ID3 fake mp3"


# -- schemas: the agent's first line of feedback, built from agent_tools' constants --

def test_finish_schema_closes_the_issue_fields(client):
    # kind and where are what the episode notes publish, so the agent's schema pins
    # them down, and a value off the list is refused with the reason.
    c, store = client
    _, token = store.open("x", {})
    schema = _tools(c, "/agent/mcp", token)["finish"]["inputSchema"]
    props = schema["properties"]
    assert props["outcome"]["enum"] == list(OUTCOMES)
    assert props["remaining_issues"]["maxItems"] == MAX_ISSUES
    assert props["substitutions"]["maxItems"] == MAX_SUBSTITUTIONS
    issue = schema["$defs"]["Issue"]["properties"]
    assert issue["kind"]["enum"] == list(ISSUE_KINDS)
    assert issue["where"]["pattern"] == WHERE_RE.pattern
    assert issue["note"]["maxLength"] == MAX_NOTE
    bad = {"outcome": "minor", "remaining_issues": [{"kind": "garbled", "where": "3:00", "note": ""}]}
    r = _call(c, "/agent/mcp", token, "finish", bad)
    assert r["isError"] is True and "'mispronunciation'" in _text(r)
    late = {"outcome": "minor", "remaining_issues": [{"kind": "pacing", "where": "3:60"}]}
    r = _call(c, "/agent/mcp", token, "finish", late)
    assert r["isError"] is True and WHERE_RE.pattern in _text(r)


def test_substitution_schema_states_and_enforces_the_plain_text_rule(client):
    c, store = client
    item, token = store.open("x", {})
    schema = _tools(c, "/agent/mcp", token)["finish"]["inputSchema"]
    sub = schema["$defs"]["Substitution"]["properties"]
    for field in ("original", "spoken", "why"):
        assert sub[field]["maxLength"] == MAX_FIELD
        assert "one line of plain text" in sub[field]["description"].lower()
    with pytest.raises(ToolError) as rule:
        _plain("a*b", "spoken")
    hold = {"outcome": "hold", "remaining_issues": [{"kind": "other", "where": "0:00"}]}
    for spoken in ("a*b", "two\nlines", "x" * (MAX_FIELD + 1)):
        args = {**hold, "substitutions": [{"original": "x", "spoken": spoken}]}
        r = _call(c, "/agent/mcp", token, "finish", args)
        assert r["isError"] is True, spoken
    r = _call(c, "/agent/mcp", token, "finish",
              {**hold, "substitutions": [{"original": "x", "spoken": "a*b"}]})
    assert str(rule.value) in _text(r)  # the same message agent_tools gives
    assert store.load_state(item)["status"] == "open"  # refused before finish ran
    ok = {**hold, "substitutions": [{"original": "x", "spoken": "ex", "why": ""}]}
    assert _ok(_call(c, "/agent/mcp", token, "finish", ok)) == {"ok": True}


def test_the_other_agent_schemas_carry_agent_tools_limits(client):
    c, store = client
    _, token = store.open("x", {})
    tools = _tools(c, "/agent/mcp", token)
    edit = tools["replace_text"]["inputSchema"]["properties"]
    assert edit["old"]["maxLength"] == edit["new"]["maxLength"] == MAX_EDIT_TEXT
    assert tools["phonemes"]["inputSchema"]["properties"]["word"]["maxLength"] == MAX_WORD
    record = tools["record"]["inputSchema"]["properties"]
    assert record["voice"]["pattern"] == VOICE_RE.pattern and record["voice"]["default"] == "af_heart"
    low, high = record["speed"]["minimum"], record["speed"]["maximum"]
    assert _speed(low) == low and _speed(high) == high  # the same bounds as _speed
    for outside in (low - 0.01, high + 0.01):
        with pytest.raises(ToolError):
            _speed(outside)
    for args in ({"speed": True}, {"speed": high + 0.5}, {"voice": "AF_HEART"}, {"voice": "af_heart\n"}):
        r = _call(c, "/agent/mcp", token, "record", args)
        assert r["isError"] is True, args
    assert store.load_state(store.item_for_token(token))["records"] == 0  # no round spent


# -- what the agent and the runner are told --

def test_the_agent_is_told_how_to_work(client):
    c, store = client
    _, token = store.open("x", {})
    init = _rpc(c, "/agent/mcp", token, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}}).json()["result"]
    assert init["instructions"] == AGENT_INSTRUCTIONS
    for phrase in ("one tool at a time", "replace_text", "record again", "substitution",
                   "phonemes", "speech_warnings"):
        assert phrase in AGENT_INSTRUCTIONS, phrase
    tools = {name: t["description"] for name, t in _tools(c, "/agent/mcp", token).items()}
    assert "record again" in tools["replace_text"]
    assert "speech_warnings" in tools["record"]
    assert "by hand" in tools["phonemes"] and "substitution" in tools["phonemes"]
    assert "one call at a time" in tools["phonemes"]  # the tool most likely called in parallel
    assert "respelling" in tools["finish"]
    finish = " ".join(tools["finish"].split())
    assert "not only add" in finish and "address" in finish  # what finish refuses (I2)


def test_the_limits_in_the_prose_are_the_limits_enforced(client, monkeypatch):
    import importlib
    from narration import app as app_module
    c, store = client
    _, token = store.open("x", {})
    tools = {name: t["description"] for name, t in _tools(c, "/agent/mcp", token).items()}
    for text in (AGENT_INSTRUCTIONS, tools["record"], tools["check"]):
        assert "four" not in text and "eight" not in text  # never spelled out apart
    assert (f"at most {agent_tools.MAX_RECORDS} record rounds and {agent_tools.MAX_CHECKS} "
            "checks") in AGENT_INSTRUCTIONS
    assert f"At most {agent_tools.MAX_RECORDS} rounds per article" in tools["record"]
    assert f"At most {agent_tools.MAX_CHECKS} checks per article" in tools["check"]
    # Built from the constants: other limits, other prose.
    monkeypatch.setattr(agent_tools, "MAX_RECORDS", 7)
    monkeypatch.setattr(agent_tools, "MAX_CHECKS", 11)
    try:
        patched = importlib.reload(app_module)
        assert "at most 7 record rounds and 11 checks" in patched.AGENT_INSTRUCTIONS
        assert "At most 7 rounds per article" in patched.RECORD_DESCRIPTION
        assert "At most 11 checks per article" in patched.CHECK_DESCRIPTION
    finally:
        monkeypatch.undo()
        importlib.reload(app_module)


def test_the_check_description_names_the_real_precheck_warnings(client):
    import verify_audio
    c, store = client
    _, token = store.open("x", {})
    check = " ".join(_tools(c, "/agent/mcp", token)["check"]["description"].split())
    assert "pace outside 100-250 words per minute" in check and "silences over 5 seconds" in check
    assert "truncation" not in check
    # what verify_audio.precheck_warnings checks
    assert "pace outside 100-250 WPM, or silences over 5 s" in verify_audio.precheck_warnings.__doc__


def test_the_runner_is_told_when_to_force_and_what_status_returns(client):
    c, _ = client
    tools = {name: t["description"] for name, t in _tools(c, "/runner/mcp", RUNNER).items()}
    assert "force=True" in tools["finalize"] and "timeout" in tools["finalize"]
    for word in ("open", "finished", "finalized", "abandoned", "prepared_matches_recording",
                 "records", "recorded_round", "checks_used", "finish", "mp3", "exists"):
        assert word in tools["status"], word


# -- python -m narration --

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A clean configuration: a token file and a data folder; nothing else set."""
    for name in ("NARRATION_HOST", "NARRATION_PORT", "NARRATION_ALLOWED_HOSTS", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    token_file = tmp_path / "runner-token"
    token_file.write_text("runner-secret\n")
    monkeypatch.setenv("NARRATION_RUNNER_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("NARRATION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)  # ignore any .env in the checkout
    served = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.append((app, kw)))
    return token_file, served


def test_real_engines_need_a_gemini_key_at_startup(env):
    from narration.__main__ import main
    _, served = env
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        main([])
    assert not served
    main(["--fake-engines"])  # the smoke path needs no key
    assert served and served[0][1]["port"] == 8770 and served[0][1]["host"] == "127.0.0.1"
    assert "root_path" not in served[0][1]  # BearerAuth matches absolute paths
    assert served[0][1]["lifespan"] == "on"  # no serving without the session managers


def test_the_served_app_has_the_configured_hosts_and_token(env, tmp_path, monkeypatch):
    from narration.__main__ import main
    _, served = env
    monkeypatch.setenv("NARRATION_PORT", "9000")
    monkeypatch.setenv("NARRATION_ALLOWED_HOSTS", " mac.tailnet:9000 , localhost:9000,")
    main(["--fake-engines"])
    app, kw = served[0]
    assert kw["port"] == 9000
    _, token = WorkItemStore(tmp_path / "data").open("x", {})
    with TestClient(app) as c:
        on_list = {"Host": "mac.tailnet:9000"}
        assert _rpc(c, "/agent/mcp", token, "tools/list", **on_list).status_code == 200
        assert _rpc(c, "/runner/mcp", RUNNER, "tools/list", **on_list).status_code == 200  # newline stripped
        assert _rpc(c, "/agent/mcp", token, "tools/list").status_code == 421  # Host testserver


@pytest.mark.parametrize("content, rule", [
    ("runner secret\n", "whitespace"),
    (" runner-secret\n", "leading or trailing whitespace"),
    ("\n", "blank"),
    ("rünner-secret", "ASCII"),
])
def test_a_bad_runner_token_file_is_one_readable_line(env, content, rule):
    from narration.__main__ import main
    token_file, served = env
    token_file.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        main(["--fake-engines"])
    message = e.value.code
    assert isinstance(message, str) and "\n" not in message
    assert str(token_file) in message and rule in message
    assert not served


def test_a_missing_runner_token_file_is_one_readable_line(env, monkeypatch, tmp_path):
    from narration.__main__ import main
    _, served = env
    missing = tmp_path / "no-such-file"
    monkeypatch.setenv("NARRATION_RUNNER_TOKEN_FILE", str(missing))
    with pytest.raises(SystemExit) as e:
        main(["--fake-engines"])
    assert isinstance(e.value.code, str) and "\n" not in e.value.code and str(missing) in e.value.code
    monkeypatch.delenv("NARRATION_RUNNER_TOKEN_FILE")
    with pytest.raises(SystemExit, match="NARRATION_RUNNER_TOKEN_FILE"):
        main(["--fake-engines"])
    assert not served


@pytest.mark.parametrize("value", ["", " , ,"])
def test_an_empty_host_allow_list_refuses_to_start(env, monkeypatch, value):
    from narration.__main__ import main
    _, served = env
    monkeypatch.setenv("NARRATION_ALLOWED_HOSTS", value)
    with pytest.raises(SystemExit, match="NARRATION_ALLOWED_HOSTS"):
        main(["--fake-engines"])
    assert not served


def test_a_bad_port_is_one_readable_line(env, monkeypatch):
    from narration.__main__ import main
    monkeypatch.setenv("NARRATION_PORT", "eighty")
    with pytest.raises(SystemExit, match="NARRATION_PORT"):
        main(["--fake-engines"])


def test_help_needs_no_configuration(monkeypatch, capsys):
    from narration.__main__ import main
    for name in ("NARRATION_RUNNER_TOKEN_FILE", "NARRATION_DATA_DIR", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for name in ("--fake-engines", "NARRATION_RUNNER_TOKEN_FILE", "NARRATION_ALLOWED_HOSTS",
                 "GEMINI_API_KEY"):
        assert name in out, name
