"""The session service, against a fake `claude` on PATH (never the real one; no network)."""

import ast
import dataclasses
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from narration_pipeline import handoff
from narration_pipeline import session_service as ss
from narration_pipeline.config import Settings

SIX = {f"mcp__narration__{n}" for n in ("get_text", "replace_text", "record", "check", "phonemes", "finish")}
ITEM = "0123456789ab"
TOKEN = "synthetic-agent-token-" + "x" * 21  # 43 characters, the shape of token_urlsafe(32)
URL = "http://mac.example:8770/agent/mcp"


def init_event(tools=SIX, status="connected"):
    return {"type": "system", "subtype": "init", "cwd": "/somewhere", "session_id": "s-1",
            "tools": sorted(tools), "mcp_servers": [{"name": "narration", "status": status}],
            "model": "a-model", "permissionMode": "dontAsk"}


RESULT = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1234,
          "num_turns": 7, "result": "Finished with outcome clean.", "total_cost_usd": 0.42,
          "usage": {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 40, "server_tool_use": {"web_search_requests": 0}},
          "terminal_reason": "completed"}

# The fake claude: records what it was started with, then follows plan.json beside it.
FAKE = '''
import json, os, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
argv = sys.argv[1:]
seen = {"argv": argv, "env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid()}
if "--mcp-config" in argv:
    p = Path(argv[argv.index("--mcp-config") + 1])
    seen.update(mcp_path=str(p), mcp=json.loads(p.read_text()), mcp_mode=p.stat().st_mode & 0o777,
                dir_mode=p.parent.stat().st_mode & 0o777)
with open(here / "log", "a") as f:
    f.write(f"start {os.getpid()}\\n")
tmp = here / ".seen.tmp"
tmp.write_text(json.dumps(seen))
os.replace(tmp, here / "seen.json")
for step in json.loads((here / "plan.json").read_text()):
    if "child" in step:  # a grandchild in the same process group, holding the pipes
        c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (here / "child.pid").write_text(str(c.pid))
    if "out" in step:
        out = step["out"]
        print(out if isinstance(out, str) else json.dumps(out), flush=True)
    if "err" in step:
        sys.stderr.write(step["err"] * step.get("times", 1))
        sys.stderr.flush()
    if "sleep" in step:
        time.sleep(step["sleep"])
    if "exit" in step:
        sys.exit(step["exit"])
with open(here / "log", "a") as f:
    f.write(f"end {os.getpid()}\\n")
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE}")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def install(*steps):
        (bindir / "plan.json").write_text(json.dumps(list(steps)))
        return bindir
    return install


@pytest.fixture
def cfg(tmp_path):
    prompt = tmp_path / "agent_prompt.md"
    prompt.write_text("# The system prompt\n")
    return ss.ServiceConfig(narration_url=URL, prompt_path=prompt, prompt="# The system prompt\n")


def request(**changes):
    req = ss.Request(item_id=ITEM, agent_token=TOKEN, text_rules=(), budget_usd=2.0, timeout_s=60)
    return dataclasses.replace(req, **changes)


def gone(pid, within=3.0):
    """Whether the process has exited and been reaped (an orphan is reaped by init)."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def wait_for(path, within=15.0):
    deadline = time.monotonic() + within
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} did not appear"
        time.sleep(0.05)
    return path


def valid_body(**changes):
    body = {"item_id": ITEM, "agent_token": TOKEN, "text_rules": [], "budget_usd": 2.0, "timeout_s": 5400}
    body.update(changes)
    return body


# -- check_init --

def test_check_init_accepts_the_six_tools():
    assert ss.check_init(init_event()) is None


def test_expected_tools_are_the_six():
    assert ss.EXPECTED_TOOLS == frozenset(SIX)


def test_check_init_refuses_an_extra_tool():
    error = ss.check_init(init_event(SIX | {"Bash"}))
    assert error is not None and "Bash" in error


def test_check_init_refuses_a_missing_tool():
    error = ss.check_init(init_event(SIX - {"mcp__narration__finish"}))
    assert error is not None and "mcp__narration__finish" in error


def test_check_init_refuses_a_disconnected_server():
    error = ss.check_init(init_event(status="failed"))
    assert error is not None and "narration" in error


def test_check_init_refuses_a_missing_server():
    event = init_event()
    event["mcp_servers"] = [{"name": "other", "status": "connected"}]
    assert ss.check_init(event) is not None


@pytest.mark.parametrize("event", [
    {"type": "assistant", "message": {}},
    {"type": "system", "subtype": "status"},
    {"type": "system", "subtype": "init", "mcp_servers": [{"name": "narration", "status": "connected"}]},
    {"type": "system", "subtype": "init", "tools": "mcp__narration__get_text",
     "mcp_servers": [{"name": "narration", "status": "connected"}]},
])
def test_check_init_refuses_anything_but_an_init_event_with_a_tool_list(event):
    assert ss.check_init(event) is not None


# -- parse_result --

def test_parse_result_reads_a_successful_session():
    assert ss.parse_result(RESULT) == {"finished_ok": True, "cost_usd": 0.42, "num_turns": 7,
                                       "terminal_reason": "completed", "error": None, "tokens": 100,
                                       "category": "session"}


def test_parse_result_reads_an_error_subtype():
    out = ss.parse_result({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                           "total_cost_usd": 2.01, "num_turns": 30})
    assert out["finished_ok"] is False and "error_max_budget_usd" in out["error"]
    assert out["cost_usd"] == 2.01 and out["num_turns"] == 30 and out["tokens"] is None


def test_parse_result_reads_an_api_error():
    out = ss.parse_result({"type": "result", "subtype": "success", "is_error": True,
                           "result": "API Error: 529 overloaded", "api_error_status": 529})
    assert out["finished_ok"] is False and "API Error: 529" in out["error"]


def test_parse_result_tolerates_missing_and_odd_keys():
    out = ss.parse_result({"type": "result", "usage": {"input_tokens": 5, "output_tokens": "x"},
                           "total_cost_usd": True, "num_turns": "7"})
    assert out == {"finished_ok": True, "cost_usd": None, "num_turns": None, "terminal_reason": None,
                   "error": None, "tokens": 5, "category": "session"}


# -- validate_request --

def test_validate_request_accepts_what_the_runner_writes(tmp_path):
    item_id, token, s = secrets.token_hex(6), secrets.token_urlsafe(32), Settings()
    handoff.write_request(tmp_path, item_id, token, ["Drop the newsletter box."],
                          s.session_budget_usd, s.session_timeout_s)
    body = json.loads((tmp_path / "requests" / f"{item_id}.json").read_text())
    assert ss.validate_request(body) == ss.Request(item_id, token, ("Drop the newsletter box.",),
                                                   s.session_budget_usd, s.session_timeout_s)


@pytest.mark.parametrize("change", [
    {"item_id": "../../etc/passwd"},
    {"agent_token": "a/" + "b" * 41},
    {"text_rules": ["a rule"] * 21},
    {"text_rules": ["x" * 301]},
    {"budget_usd": 50},
    {"timeout_s": 10},
    {"item_id": "0123456789AB"},
    {"agent_token": TOKEN + "x"},
    {"text_rules": "a rule"},
    {"text_rules": [5]},
    {"budget_usd": 0},
    {"budget_usd": True},
    {"budget_usd": float("nan")},
    {"timeout_s": 600.0},
    {"timeout_s": 7201},
])
def test_validate_request_refuses(change):
    with pytest.raises(ValueError):
        ss.validate_request(valid_body(**change))


@pytest.mark.parametrize("body", [None, [], "x", {k: v for k, v in valid_body().items() if k != "agent_token"}])
def test_validate_request_refuses_a_non_request(body):
    with pytest.raises(ValueError):
        ss.validate_request(body)


def test_validate_request_never_echoes_the_token():
    with pytest.raises(ValueError) as e:
        ss.validate_request(valid_body(agent_token=TOKEN[:-1] + "/"))
    assert TOKEN[:-1] not in str(e.value)


# -- the session's configuration --

def test_mcp_config_is_the_documented_shape():
    assert ss.mcp_config(URL, TOKEN) == {"mcpServers": {"narration": {
        "type": "http", "url": URL, "timeout": 900000, "headers": {"Authorization": f"Bearer {TOKEN}"}}}}


def test_claude_argv_is_the_s1_flag_set():
    argv = ss.claude_argv(Path("/private/dir/mcp.json"), "# How to record\n", 2.0, "Record the article.")
    assert argv == [
        "claude", "-p", "Record the article.",
        "--tools", "", "--strict-mcp-config", "--mcp-config", "/private/dir/mcp.json",
        "--allowedTools", "mcp__narration__get_text mcp__narration__replace_text mcp__narration__record "
                          "mcp__narration__check mcp__narration__phonemes mcp__narration__finish",
        "--permission-mode", "dontAsk", "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
        "--no-session-persistence", "--max-budget-usd", "2.0", "--append-system-prompt", "# How to record\n",
        "--output-format", "stream-json", "--verbose"]
    assert "--bare" not in argv


def test_session_env_is_exactly_the_allowed_variables():
    environ = {"PATH": "/usr/bin:/bin", "HOME": "/home/agent", "USER": "agent", "LOGNAME": "agent",
               "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-oauth", "NARRATION_AGENT_URL": URL,
               "HANDOFF_DIR": "/handoff", "SOME_SECRET": "s", "LANG": "C.UTF-8",
               "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0", "ANTHROPIC_API_KEY": "k"}
    assert ss.session_env(environ) == {
        "PATH": "/usr/bin:/bin", "HOME": "/home/agent", "USER": "agent", "LOGNAME": "agent",
        "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-oauth", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1"}


def test_session_env_leaves_out_what_the_service_lacks():
    assert ss.session_env({"PATH": "/bin", "HOME": "/h"}) == {
        "PATH": "/bin", "HOME": "/h", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1"}


def test_session_prompt_lists_the_sources_rules():
    prompt = ss.session_prompt(("Drop the newsletter sign-up box.", "Read the\nmasthead once."))
    assert prompt.startswith("Record the article")
    assert "\n\nNotes for this source:\n- Drop the newsletter sign-up box.\n- Read the masthead once." in prompt


def test_session_prompt_without_rules_has_no_notes():
    assert "Notes for this source" not in ss.session_prompt(())


# -- run_session --

def test_run_session_reads_a_finished_session(fake_claude, cfg):
    bindir = fake_claude({"out": init_event()}, {"out": {"type": "assistant", "message": {}}},
                         {"out": "not json"}, {"out": RESULT})
    assert ss.run_session(request(text_rules=("Drop the box.",)), cfg) == ss.parse_result(RESULT)
    seen = json.loads((bindir / "seen.json").read_text())
    assert seen["argv"] == ss.claude_argv(Path(seen["mcp_path"]), cfg.prompt, 2.0,
                                          ss.session_prompt(("Drop the box.",)))[1:]
    assert seen["mcp"] == ss.mcp_config(URL, TOKEN)
    assert seen["mcp_mode"] == 0o600 and seen["dir_mode"] == 0o700
    assert seen["cwd"] == str(Path(seen["mcp_path"]).parent.resolve())
    assert not Path(seen["mcp_path"]).parent.exists()  # the token file is gone with its directory


def test_run_session_passes_only_the_allowed_environment(fake_claude, cfg, monkeypatch):
    bindir = fake_claude({"out": init_event()}, {"out": RESULT})
    monkeypatch.setenv("NARRATION_PROBE_SECRET", "must-not-pass")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-oauth")
    ss.run_session(request(), cfg)
    env = json.loads((bindir / "seen.json").read_text())["env"]
    assert "NARRATION_PROBE_SECRET" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-oauth"
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1" and env["PATH"] == os.environ["PATH"]


def test_run_session_kills_on_bad_init(fake_claude, cfg):
    bindir = fake_claude({"child": True}, {"out": init_event(SIX | {"Bash"})}, {"sleep": 60}, {"out": RESULT})
    started = time.monotonic()
    out = ss.run_session(request(), cfg)
    assert time.monotonic() - started < 5
    seen = json.loads((bindir / "seen.json").read_text())
    assert gone(seen["pid"]) and gone(int((bindir / "child.pid").read_text()))
    assert out["finished_ok"] is False and out["error"] == "tool check failed: extra tools: Bash"
    assert not Path(seen["mcp_path"]).exists()


def test_run_session_refuses_a_first_event_that_is_not_init(fake_claude, cfg):
    fake_claude({"out": {"type": "assistant", "message": {}}}, {"sleep": 60})
    out = ss.run_session(request(), cfg)
    assert out["finished_ok"] is False and out["error"].startswith("tool check failed: ")


# Claude Code 2.1.316's stream-json, with the service's flags (captured in the agent
# container, 2026-11-02): a ui_invalidate event can come before init, and when it can't
# authenticate it writes its own assistant message (model <synthetic>, event-level
# "error": "authentication_failed", nothing spent), with no token and with a bad one.
UI_INVALIDATE = {"type": "system", "subtype": "ui_invalidate", "event": "ui.render", "uuid": "u-1",
                 "session_id": "s-1"}
NOT_LOGGED_IN = "Not logged in \u00b7 Please run /login"
BAD_TOKEN_TEXT = "Failed to authenticate. API Error: 401 OAuth access token is invalid."
ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0}


def synthetic(text, **event):
    return {"type": "assistant", "session_id": "s-1", "parent_tool_use_id": None, **event,
            "message": {"model": "<synthetic>", "role": "assistant", "type": "message",
                        "stop_reason": "stop_sequence", "usage": ZERO_USAGE,
                        "content": [{"type": "text", "text": text}]}}


def auth_result(text, **event):
    return {"type": "result", "subtype": "success", "is_error": True, "result": text, "num_turns": 1,
            "total_cost_usd": 0, "duration_api_ms": 0, "terminal_reason": "api_error",
            "usage": ZERO_USAGE, **event}


LOGIN_ASSISTANT = synthetic(NOT_LOGGED_IN, error="authentication_failed", is_api_error_message=True)
LOGIN_RESULT = auth_result(NOT_LOGGED_IN, api_error_status=None)
API_RETRY = {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10, "retry_delay_ms": 504,
             "error_status": 401, "error": "authentication_failed", "session_id": "s-1"}
BAD_TOKEN_ASSISTANT = synthetic(BAD_TOKEN_TEXT, error="authentication_failed", is_api_error_message=True,
                                api_error_status=401, api_error_origin="first_party", request_id="req_1")
BAD_TOKEN_RESULT = auth_result(BAD_TOKEN_TEXT, api_error_status=401)
LOGIN_ERROR = "Claude Code could not authenticate (CLAUDE_CODE_OAUTH_TOKEN missing, expired or invalid)"


def test_run_session_reads_past_system_events_before_init(fake_claude, cfg):
    fake_claude({"out": UI_INVALIDATE}, {"out": init_event()}, {"out": RESULT})
    assert ss.run_session(request(), cfg) == ss.parse_result(RESULT)


def test_run_session_still_checks_an_init_that_follows_system_events(fake_claude, cfg):
    bindir = fake_claude({"out": UI_INVALIDATE}, {"out": init_event(SIX | {"Bash"})}, {"sleep": 60}, {"out": RESULT})
    started = time.monotonic()
    out = ss.run_session(request(), cfg)
    assert time.monotonic() - started < 5
    assert gone(json.loads((bindir / "seen.json").read_text())["pid"])
    assert out["finished_ok"] is False and out["error"] == "tool check failed: extra tools: Bash"


@pytest.mark.parametrize("before", [{"type": "assistant", "message": {}}, RESULT],
                         ids=["assistant", "result"])
def test_run_session_refuses_a_non_system_event_before_init(fake_claude, cfg, before):
    fake_claude({"out": UI_INVALIDATE}, {"out": before}, {"out": init_event()}, {"sleep": 60})
    out = ss.run_session(request(), cfg)
    assert out["finished_ok"] is False and out["error"].startswith("tool check failed: ")


def test_run_session_without_an_init_fails(fake_claude, cfg):
    fake_claude({"out": UI_INVALIDATE}, {"exit": 1})
    out = ss.run_session(request(), cfg)
    assert out["finished_ok"] is False and out["error"].startswith("no result event")


@pytest.mark.parametrize("is_error", [True, False])  # True as captured; False as first described
def test_run_session_reports_a_missing_login(fake_claude, cfg, is_error):
    fake_claude({"out": UI_INVALIDATE}, {"out": init_event()}, {"out": LOGIN_ASSISTANT},
                {"out": {**LOGIN_RESULT, "is_error": is_error}})
    out = ss.run_session(request(), cfg)
    assert out["finished_ok"] is False and out["error"] == LOGIN_ERROR == ss.LOGIN_ERROR
    assert out["cost_usd"] == 0.0 and out["num_turns"] == 1


def test_run_session_reports_an_invalid_token(fake_claude, cfg):
    fake_claude({"out": UI_INVALIDATE}, {"out": init_event()}, {"out": API_RETRY}, {"out": {**API_RETRY, "attempt": 2}},
                {"out": BAD_TOKEN_ASSISTANT}, {"out": BAD_TOKEN_RESULT})
    out = ss.run_session(request(), cfg)
    assert out["finished_ok"] is False and out["error"] == LOGIN_ERROR


def test_run_session_reads_not_logged_in_text_from_a_synthetic_message(fake_claude, cfg):
    # the second route: Claude Code's own "Not logged in" message, without the error field
    fake_claude({"out": init_event()}, {"out": synthetic(NOT_LOGGED_IN)},
                {"out": {**LOGIN_RESULT, "result": "", "is_error": False}})
    out = ss.run_session(request(), cfg)
    assert out["finished_ok"] is False and out["error"] == LOGIN_ERROR


@pytest.mark.parametrize("text", [NOT_LOGGED_IN, BAD_TOKEN_TEXT])
def test_a_model_writing_an_auth_failure_is_not_one(fake_claude, cfg, text):
    # An article could steer the agent into these words; its message has a real model
    # (only Claude Code writes <synthetic> ones), so the session ends as any other does.
    steered = {**LOGIN_ASSISTANT, "message": {**LOGIN_ASSISTANT["message"], "model": "a-model"}}
    steered["message"]["content"] = [{"type": "text", "text": text}]
    result = {**RESULT, "result": text}
    fake_claude({"out": init_event()}, {"out": steered}, {"out": result})
    assert ss.run_session(request(), cfg) == ss.parse_result(result)
    assert ss.parse_result(result)["finished_ok"] is True


def test_auth_failure_is_read_only_from_claude_codes_own_messages():
    assert ss.authentication_failed(LOGIN_ASSISTANT) and ss.authentication_failed(BAD_TOKEN_ASSISTANT)
    assert ss.authentication_failed(synthetic(NOT_LOGGED_IN))                 # the text route
    assert ss.authentication_failed(synthetic("anything", error="authentication_failed"))  # the error route
    assert not ss.authentication_failed(synthetic("Rate limited", error="rate_limit"))
    model = {"model": "a-model"}
    assert not ss.authentication_failed({**LOGIN_ASSISTANT, "message": {**LOGIN_ASSISTANT["message"], **model}})
    assert not ss.authentication_failed({**BAD_TOKEN_ASSISTANT, "message": {**BAD_TOKEN_ASSISTANT["message"], **model}})
    assert not ss.authentication_failed({"type": "assistant", "message": {"model": "<synthetic>", "content": "x"}})
    for event in (LOGIN_RESULT, BAD_TOKEN_RESULT, API_RETRY, UI_INVALIDATE):
        assert not ss.authentication_failed(event)


# Claude Code's own message that an API request failed for another reason (overloaded,
# rate-limited, unreachable). Built in the shape of the captured authentication events
# above; the error codes Claude Code uses for these were not captured, so the tests
# take any code but authentication_failed, as the service does.
API_TEXT = "API Error: Repeated 529 Overloaded errors"
API_ASSISTANT = synthetic(API_TEXT, error="server_error", is_api_error_message=True)
API_RESULT = auth_result(API_TEXT, api_error_status=529)


@pytest.mark.parametrize("ending", ["result", "no result", "timeout"])
@pytest.mark.parametrize("failure, category", [(LOGIN_ASSISTANT, "login"), (BAD_TOKEN_ASSISTANT, "login"),
                                               (API_ASSISTANT, "api")], ids=["no-login", "bad-token", "api"])
def test_claude_codes_own_failure_is_categorized_on_every_exit_path(fake_claude, cfg, failure, category, ending):
    end = {"result": [{"out": API_RESULT if category == "api" else LOGIN_RESULT}],
           "no result": [{"exit": 1}], "timeout": [{"sleep": 60}]}[ending]
    fake_claude({"out": UI_INVALIDATE}, {"out": init_event()}, {"out": failure}, *end)
    out = ss.run_session(request(timeout_s=2), cfg)
    assert out["category"] == category and out["finished_ok"] is False
    if category == "login":
        assert out["error"] == LOGIN_ERROR
    else:
        assert out["error"]


def test_an_api_failure_keeps_the_sessions_own_error(fake_claude, cfg):
    fake_claude({"out": init_event()}, {"out": API_ASSISTANT}, {"out": API_RESULT})
    out = ss.run_session(request(), cfg)
    assert out == {**ss.parse_result(API_RESULT), "category": "api"}
    assert "API error status 529" in out["error"]


@pytest.mark.parametrize("ending", ["no result", "timeout", "success"])
def test_an_api_failure_without_an_error_of_its_own_says_what_claude_code_said(fake_claude, cfg, ending):
    end = {"no result": {"exit": 1}, "timeout": {"sleep": 60},
           "success": {"out": {**API_RESULT, "is_error": False, "result": ""}}}[ending]
    fake_claude({"out": init_event()}, {"out": API_ASSISTANT}, end)
    out = ss.run_session(request(timeout_s=2), cfg)
    assert out["category"] == "api" and out["finished_ok"] is False
    if ending == "success":  # nothing else to go on: Claude Code's message is the error
        assert "server_error" in out["error"] and API_TEXT in out["error"]
    else:                    # the session's own error, as for any other session
        assert out["error"].startswith(("no result event", "timed out after 2 s"))


def test_api_failure_is_read_only_from_claude_codes_own_messages():
    assert ss.api_failure(API_ASSISTANT) and API_TEXT in ss.api_failure(API_ASSISTANT)
    assert ss.api_failure(synthetic("Rate limited", error="rate_limit"))
    assert ss.api_failure(synthetic("API Error: Connection error.", is_api_error_message=True))  # flag, no code
    for event in (LOGIN_ASSISTANT, BAD_TOKEN_ASSISTANT,                  # those are login failures
                  synthetic("No response requested."),                   # Claude Code's, but no error
                  {**API_ASSISTANT, "message": {**API_ASSISTANT["message"], "model": "a-model"}},  # the model's
                  API_RESULT, API_RETRY, UI_INVALIDATE):
        assert ss.api_failure(event) is None


def test_a_login_failure_outranks_an_api_failure(fake_claude, cfg):
    fake_claude({"out": init_event()}, {"out": API_ASSISTANT}, {"out": BAD_TOKEN_ASSISTANT}, {"out": BAD_TOKEN_RESULT})
    out = ss.run_session(request(), cfg)
    assert (out["category"], out["error"]) == ("login", LOGIN_ERROR)


@pytest.mark.parametrize("steps", [
    [{"out": RESULT}],                                                        # success
    [{"out": {"type": "result", "subtype": "error_max_budget_usd", "is_error": True}}],  # an error result
    [{"exit": 3}],                                                            # no result event
    [{"sleep": 60}],                                                          # timeout
    [{"out": {**API_ASSISTANT, "message": {**API_ASSISTANT["message"], "model": "a-model"}}}, {"out": RESULT}],
], ids=["success", "error-result", "no-result", "timeout", "model-wrote-it"])
def test_every_other_session_is_category_session(fake_claude, cfg, steps):
    fake_claude({"out": init_event()}, *steps)
    assert ss.run_session(request(timeout_s=2), cfg)["category"] == "session"


def test_results_the_service_writes_itself_are_category_session():
    assert ss.failure("service restarted during the session")["category"] == "session"


def test_run_session_timeout_kills(fake_claude, cfg):
    bindir = fake_claude({"child": True}, {"out": init_event()}, {"sleep": 60}, {"out": RESULT})
    started = time.monotonic()
    out = ss.run_session(request(timeout_s=1), cfg)
    assert time.monotonic() - started < 5
    seen = json.loads((bindir / "seen.json").read_text())
    assert gone(seen["pid"]) and gone(int((bindir / "child.pid").read_text()))
    assert out["finished_ok"] is False and out["error"].startswith("timed out after 1 s")
    assert not Path(seen["mcp_path"]).exists()


def test_run_session_without_a_result_event_reports_stderr(fake_claude, cfg):
    fake_claude({"out": init_event()}, {"err": "line one\n"}, {"err": f"Authorization: Bearer {TOKEN}\n"},
                {"err": "the last line\n"}, {"exit": 3})
    out = ss.run_session(request(), cfg)
    assert out["finished_ok"] is False and out["error"].startswith("no result event")
    assert "3" in out["error"] and "the last line" in out["error"]
    assert TOKEN not in out["error"]


def leaked(secret, text):
    """Whether any 6 characters of the secret appear in the text."""
    return any(secret[i:i + 6] in text for i in range(len(secret) - 5))


def test_run_session_redacts_before_clipping_stderr(fake_claude, cfg, monkeypatch):
    oauth = "synthetic-oauth-token-" + "y" * 30
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", oauth)
    fake_claude({"out": init_event()}, {"err": "a" * 188 + TOKEN + "\n"}, {"err": "c" * 190 + oauth + "\n"},
                {"exit": 3})
    error = ss.run_session(request(), cfg)["error"]
    assert error.startswith("no result event") and "aaaa" in error and "cccc" in error
    assert not leaked(TOKEN, error) and not leaked(oauth, error)


def test_run_session_redacts_before_clipping_a_result(fake_claude, cfg):
    fake_claude({"out": init_event()},
                {"out": {"type": "result", "subtype": "success", "is_error": True, "result": "b" * 290 + TOKEN}})
    error = ss.run_session(request(), cfg)["error"]
    assert "bbbb" in error and not leaked(TOKEN, error)


def test_run_session_survives_a_chatty_stderr(fake_claude, cfg):
    fake_claude({"err": "noise " * 20 + "\n", "times": 20000}, {"out": init_event()}, {"out": RESULT})
    started = time.monotonic()
    assert ss.run_session(request(timeout_s=30), cfg)["finished_ok"] is True
    assert time.monotonic() - started < 20


def test_run_session_without_claude(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    out = ss.run_session(request(), cfg)
    assert out == {"finished_ok": False, "cost_usd": None, "num_turns": None, "terminal_reason": None,
                   "error": "claude not found", "tokens": None, "category": "session"}


# -- serve --

def test_serve_writes_a_result_and_removes_the_request(fake_claude, cfg, tmp_path):
    bindir = fake_claude({"out": init_event()}, {"out": RESULT})
    hand, other = tmp_path / "handoff", "ba9876543210"
    handoff.write_request(hand, ITEM, TOKEN, [], 2.0, 600)
    handoff.write_request(hand, other, TOKEN, [], 2.0, 600)
    results = hand / "results"
    ss.serve(hand, cfg, poll_s=0.05,
             until=lambda: (results / f"{ITEM}.json").exists() and (results / f"{other}.json").exists())
    for item_id in (ITEM, other):
        path = results / f"{item_id}.json"
        assert json.loads(path.read_text()) == ss.parse_result(RESULT)
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list((hand / "requests").iterdir()) and not list((hand / "running").iterdir())
    assert not [p for p in results.iterdir() if p.name.startswith(".")]
    log = (bindir / "log").read_text().split()
    assert log[0::2] == ["start", "end", "start", "end"]  # one session at a time


def test_serve_once_moves_the_request_to_running_during_the_session(fake_claude, cfg, tmp_path, monkeypatch):
    hand = tmp_path / "handoff"
    handoff.write_request(hand, ITEM, TOKEN, [], 2.0, 600)
    seen = {}

    def session(req, cfg):
        seen["requests"] = sorted(p.name for p in (hand / "requests").iterdir())
        seen["running"] = sorted(p.name for p in (hand / "running").iterdir())
        return ss.parse_result(RESULT)
    monkeypatch.setattr(ss, "run_session", session)
    assert ss.serve_once(hand, cfg) is True
    assert seen == {"requests": [], "running": [f"{ITEM}.json"]}
    assert ss.serve_once(hand, cfg) is False


def test_serve_once_writes_a_result_when_the_session_raises(cfg, tmp_path, monkeypatch):
    hand = tmp_path / "handoff"
    handoff.write_request(hand, ITEM, TOKEN, [], 2.0, 600)

    def broken(req, cfg):
        raise RuntimeError("a bug")
    monkeypatch.setattr(ss, "run_session", broken)
    ss.serve_once(hand, cfg)
    out = json.loads((hand / "results" / f"{ITEM}.json").read_text())
    assert out["finished_ok"] is False and "a bug" in out["error"]
    assert not list((hand / "running").iterdir())


def test_an_invalid_request_with_a_valid_id_gets_a_result(cfg, tmp_path, monkeypatch):
    hand = tmp_path / "handoff"
    handoff.write_request(hand, ITEM, TOKEN, [], 50.0, 600)
    monkeypatch.setattr(ss, "run_session", lambda req, cfg: pytest.fail("no session for an invalid request"))
    ss.serve_once(hand, cfg)
    out = json.loads((hand / "results" / f"{ITEM}.json").read_text())
    assert out["finished_ok"] is False and out["error"].startswith("invalid request: ")
    assert TOKEN not in out["error"]
    assert not list((hand / "running").iterdir())


@pytest.mark.parametrize("text", [json.dumps(valid_body(item_id="../../x")), "{not json", json.dumps([1])])
def test_a_request_without_a_valid_id_is_rejected(cfg, tmp_path, monkeypatch, text):
    hand = tmp_path / "handoff"
    handoff.write_request(hand, ITEM, TOKEN, [], 2.0, 600)
    (hand / "requests" / f"{ITEM}.json").write_text(text)
    monkeypatch.setattr(ss, "run_session", lambda req, cfg: pytest.fail("no session for an invalid request"))
    ss.serve_once(hand, cfg)
    assert [p.name for p in (hand / "rejected").iterdir()] == [f"{ITEM}.json"]
    assert not list((hand / "results").iterdir()) and not list((hand / "running").iterdir())


def test_serve_once_ignores_temp_files(cfg, tmp_path):
    hand = tmp_path / "handoff"
    (hand / "requests").mkdir(parents=True)
    (hand / "requests" / f".{ITEM}.tmp").write_text("{")
    assert ss.serve_once(hand, cfg) is False


def test_recover_gives_a_request_left_running_a_result(tmp_path):
    hand = tmp_path / "handoff"
    handoff.write_request(hand, ITEM, TOKEN, [], 2.0, 600)
    os.replace(hand / "requests" / f"{ITEM}.json", hand / "running" / f"{ITEM}.json")
    ss.recover(hand)
    out = json.loads((hand / "results" / f"{ITEM}.json").read_text())
    assert out["finished_ok"] is False and out["error"] == "service restarted during the session"
    assert not list((hand / "running").iterdir())


def test_recover_keeps_a_result_already_written(tmp_path):
    hand = tmp_path / "handoff"
    handoff.write_request(hand, ITEM, TOKEN, [], 2.0, 600)
    os.replace(hand / "requests" / f"{ITEM}.json", hand / "running" / f"{ITEM}.json")
    ss.write_result(hand, ITEM, ss.parse_result(RESULT))
    ss.recover(hand)
    assert json.loads((hand / "results" / f"{ITEM}.json").read_text()) == ss.parse_result(RESULT)
    assert not list((hand / "running").iterdir())


# -- the service as a script --

def test_service_config_from_env(tmp_path, monkeypatch):
    prompt = tmp_path / "a.md"
    prompt.write_text("# A prompt\n")
    assert ss.ServiceConfig.from_env({"NARRATION_AGENT_URL": URL, "AGENT_PROMPT": str(prompt)}) == \
        ss.ServiceConfig(URL, prompt, "# A prompt\n")
    monkeypatch.setattr(ss, "DEFAULT_PROMPT", tmp_path / "absent.md")
    beside = Path(ss.__file__).resolve().with_name("agent_prompt.md")
    found = ss.ServiceConfig.from_env({"NARRATION_AGENT_URL": URL})
    assert found.prompt_path == beside and found.prompt == beside.read_text()
    monkeypatch.setattr(ss, "DEFAULT_PROMPT", prompt)
    assert ss.ServiceConfig.from_env({"NARRATION_AGENT_URL": URL}).prompt_path == prompt
    with pytest.raises(SystemExit) as e:
        ss.ServiceConfig.from_env({})
    assert "NARRATION_AGENT_URL" in str(e.value)


@pytest.mark.parametrize("content", [None, "", " \n\n", b"\xff\xfe not UTF-8"])
def test_the_service_does_not_start_without_a_usable_prompt(tmp_path, monkeypatch, content):
    monkeypatch.setattr(ss, "serve", lambda *a, **k: pytest.fail("the service started serving"))
    path = tmp_path / "agent_prompt.md"
    if isinstance(content, str):
        path.write_text(content)
    elif content is not None:
        path.write_bytes(content)
    with pytest.raises(SystemExit) as e:
        ss.main({"NARRATION_AGENT_URL": URL, "AGENT_PROMPT": str(path), "HANDOFF_DIR": str(tmp_path / "handoff")})
    assert isinstance(e.value.code, str) and str(path) in e.value.code  # a message: exit status 1


def test_the_script_exits_non_zero_without_its_prompt(tmp_path):
    missing = tmp_path / "absent.md"
    done = subprocess.run([sys.executable, ss.__file__], capture_output=True, text=True, timeout=30,
                          env={"PATH": os.environ["PATH"], "NARRATION_AGENT_URL": URL, "AGENT_PROMPT": str(missing),
                               "HANDOFF_DIR": str(tmp_path / "handoff")})
    assert done.returncode == 1 and str(missing) in done.stderr


def test_service_imports_only_stdlib():
    tree = ast.parse(Path(ss.__file__).read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import needs the package"
            found.add(node.module.split(".")[0])
    assert found and found <= sys.stdlib_module_names, found - sys.stdlib_module_names


def test_the_script_stops_its_session_on_sigterm_and_recovers_on_restart(fake_claude, cfg, tmp_path):
    bindir = fake_claude({"child": True}, {"out": init_event()}, {"sleep": 60})
    hand, scratch = tmp_path / "handoff", tmp_path / "tmp"
    scratch.mkdir()
    handoff.write_request(hand, ITEM, TOKEN, [], 2.0, 600)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(scratch),
           "HANDOFF_DIR": str(hand), "NARRATION_AGENT_URL": URL, "AGENT_PROMPT": str(cfg.prompt_path)}

    def start():
        log = open(tmp_path / "service.log", "a")
        return subprocess.Popen([sys.executable, ss.__file__], env=env, stdout=log, stderr=log)

    def stop(svc):
        svc.send_signal(signal.SIGTERM)
        try:
            return svc.wait(timeout=10)
        finally:
            if svc.poll() is None:
                svc.kill()
                svc.wait()

    svc = start()
    try:
        wait_for(bindir / "child.pid")
        seen = json.loads(wait_for(bindir / "seen.json").read_text())
    finally:
        stop(svc)
    assert gone(seen["pid"]) and gone(int((bindir / "child.pid").read_text()))
    assert not Path(seen["mcp_path"]).parent.exists() and not list(scratch.iterdir())
    assert (hand / "running" / f"{ITEM}.json").exists()

    (bindir / "seen.json").unlink()
    svc = start()
    try:
        out = json.loads(wait_for(hand / "results" / f"{ITEM}.json").read_text())
    finally:
        stop(svc)
    assert out["finished_ok"] is False and out["error"] == "service restarted during the session"
    assert not (hand / "running" / f"{ITEM}.json").exists() and not (bindir / "seen.json").exists()
