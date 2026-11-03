"""The narration-agent's session service: one locked-down Claude Code session per request.

The runner (narration_pipeline.handoff) drops requests/<item_id>.json into the shared
handoff directory. This service takes one request at a time: it moves the request to
running/, runs `claude -p` with only the narration server's six agent tools, writes
results/<item_id>.json, and removes the request. On startup, a request left in running/
gets a result saying the service restarted; the runner's finalize then decides the item.
The service touches <handoff>/heartbeat every POLL_S seconds, while a session runs too,
so the runner can tell it is running; a request older than the runner waits for it
(its timeout_s + RESULT_GRACE_S) gets a "stale request" result and no session.

Standard library only, in one file: the agent image runs it as
`python3 session_service.py` (Debian's Python 3.11) with nothing installed.

The session's whole process group is killed when its init event (its first event
other than another system event) lists any tool but the six, or the narration server
isn't connected, and when it runs past the request's timeout. Each result has a
category: "login" when Claude Code couldn't authenticate (its error is then LOGIN_ERROR),
"api" when Claude Code reported any other failed API request, and "session" otherwise,
so the runner can tell a failure outside the article from the article's own. A request
whose item id is valid gets a result, failures included;
one without a valid id is moved to rejected/ instead. The MCP config file holds the
item's agent token: it lives in a private temporary directory, which is removed when
the session ends, however it ends.
"""

from __future__ import annotations

import collections
import json
import logging
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("session_service")

PREFIX = "mcp__narration__"
TOOL_NAMES = ("get_text", "replace_text", "record", "check", "phonemes", "finish")
EXPECTED_TOOLS = frozenset(PREFIX + name for name in TOOL_NAMES)

ITEM_ID_RE = re.compile(r"[0-9a-f]{12}")     # the narration server's secrets.token_hex(6)
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32); both matched with fullmatch
MAX_RULES = 20
MAX_RULE_CHARS = 300
MAX_BUDGET_USD = 5.00
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 60, 7200

# Claude Code's per-call limit for the narration server, in ms (docs/narration-server.md:
# a check of a long recording takes longer than its default of about 60 s).
MCP_TIMEOUT_MS = 900_000
OAUTH_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
# What the session inherits from the service's environment, when the service has it.
# USER and LOGNAME: on macOS the CLI's keychain login needs them.
PASSED_THROUGH = ("PATH", "HOME", "USER", "LOGNAME", OAUTH_VAR)
SESSION_FLAGS = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
LOGIN_ERROR = "Claude Code could not authenticate (CLAUDE_CODE_OAUTH_TOKEN missing, expired or invalid)"
NOT_LOGGED_IN = "Not logged in"  # how Claude Code's own message starts when there is no token

TASK = ("Record the article in this work item with the narration tools, following your "
        "instructions for recording articles. Start with get_text and end with finish.")

POLL_S = 2.0          # how often the service looks for a request, and touches its heartbeat
HEARTBEAT = "heartbeat"
HEARTBEAT_STALE_S = 60  # the runner starts no session when the heartbeat is older than this
RESULT_GRACE_S = 300    # the runner waits this long past a request's timeout_s, then withdraws it
EXIT_GRACE_S = 10.0  # how long claude may take to exit after its result event or closing stdout
STDERR_LINES = 20    # the last lines of claude's stderr kept for a failure's error
STDERR_LINE_CHARS = 200
MAX_ERROR = 5000

HERE = Path(__file__).resolve().parent
DEFAULT_PROMPT = Path("/opt/narration/agent_prompt.md")


@dataclass(frozen=True)
class Request:
    item_id: str
    agent_token: str = field(repr=False)  # kept out of logs and tracebacks
    text_rules: tuple[str, ...]
    budget_usd: float
    timeout_s: int


@dataclass(frozen=True)
class ServiceConfig:
    narration_url: str  # the narration server's agent endpoint, http://<mac>:8770/agent/mcp
    prompt_path: Path   # where the agent prompt was read from
    prompt: str = field(repr=False)  # its text, the session's appended system prompt

    @classmethod
    def from_env(cls, environ) -> ServiceConfig:
        """The configuration, or SystemExit with a message (exit status 1) when the URL is
        unset or the prompt is missing, unreadable or empty: read once, at startup, so a
        misdeployed image stops at once instead of failing every article."""
        url = environ.get("NARRATION_AGENT_URL")
        if not url:
            raise SystemExit("NARRATION_AGENT_URL is not set (the narration server's agent "
                             "endpoint, like http://<mac>:8770/agent/mcp)")
        if environ.get("AGENT_PROMPT"):
            path = Path(environ["AGENT_PROMPT"])
        elif DEFAULT_PROMPT.exists():
            path = DEFAULT_PROMPT
        else:
            path = HERE / "agent_prompt.md"
        try:
            prompt = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise SystemExit(f"cannot read the agent prompt {path}: {e}") from None
        if not prompt.strip():
            raise SystemExit(f"the agent prompt {path} is empty")
        return cls(url, path, prompt)


# -- pure logic --

def valid_item_id(value) -> bool:
    return isinstance(value, str) and ITEM_ID_RE.fullmatch(value) is not None


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_request(obj) -> Request:
    """The request, or ValueError naming the first field that's wrong (never its value)."""
    if not isinstance(obj, dict):
        raise ValueError("the request is not a JSON object")
    rules, budget, timeout = obj.get("text_rules"), obj.get("budget_usd"), obj.get("timeout_s")
    if not valid_item_id(obj.get("item_id")):
        raise ValueError("item_id is not 12 lowercase hex digits")
    token = obj.get("agent_token")
    if not (isinstance(token, str) and TOKEN_RE.fullmatch(token)):
        raise ValueError("agent_token is not 43 URL-safe characters")
    if not (isinstance(rules, list) and len(rules) <= MAX_RULES
            and all(isinstance(r, str) and len(r) <= MAX_RULE_CHARS for r in rules)):
        raise ValueError(f"text_rules is not a list of at most {MAX_RULES} strings "
                         f"of at most {MAX_RULE_CHARS} characters")
    if not (_number(budget) and 0 < budget <= MAX_BUDGET_USD):  # NaN fails both comparisons
        raise ValueError(f"budget_usd is not a number above 0 and at most {MAX_BUDGET_USD:.2f}")
    if not (_integer(timeout) and MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S):
        raise ValueError(f"timeout_s is not an integer from {MIN_TIMEOUT_S} to {MAX_TIMEOUT_S}")
    return Request(obj["item_id"], token, tuple(rules), float(budget), timeout)


def mcp_config(narration_url: str, token: str) -> dict:
    """The session's --mcp-config, as docs/narration-server.md gives it."""
    return {"mcpServers": {"narration": {"type": "http", "url": narration_url, "timeout": MCP_TIMEOUT_MS,
                                         "headers": {"Authorization": f"Bearer {token}"}}}}


def claude_argv(mcp_config_path, prompt: str, budget_usd: float, task: str) -> list[str]:
    """The S1 flag set (tests/narration/smoke_claude.sh), with stream-json output so the
    init event can be read, and the agent prompt's text as the appended system prompt.
    The task comes right after -p: --mcp-config and --allowedTools each take several
    values, so a prompt after them would be read as one more."""
    return ["claude", "-p", task,
            "--tools", "", "--strict-mcp-config", "--mcp-config", str(mcp_config_path),
            "--allowedTools", " ".join(PREFIX + name for name in TOOL_NAMES),
            "--permission-mode", "dontAsk", "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
            "--no-session-persistence", "--max-budget-usd", str(budget_usd),
            "--append-system-prompt", prompt,
            "--output-format", "stream-json", "--verbose"]


def session_env(environ) -> dict[str, str]:
    """The session's whole environment: PASSED_THROUGH, when present, and SESSION_FLAGS."""
    env = {name: environ[name] for name in PASSED_THROUGH if name in environ}
    env.update(SESSION_FLAGS)
    return env


def session_prompt(text_rules) -> str:
    """The -p prompt: the task, and the source's rules as one bullet each."""
    rules = [" ".join(rule.split()) for rule in text_rules]
    rules = [rule for rule in rules if rule]
    if not rules:
        return TASK
    return TASK + "\n\nNotes for this source:\n" + "\n".join(f"- {rule}" for rule in rules)


def stale_request(age_s: float, timeout_s: int) -> bool:
    """Whether a request this old was given up by the runner: it waits for a result
    for the request's timeout_s plus RESULT_GRACE_S, then withdraws the request."""
    return age_s > timeout_s + RESULT_GRACE_S


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _names(names: list[str]) -> str:
    shown = ", ".join(_clip(name, 64) for name in names[:10])
    return shown + (f" and {len(names) - 10} more" if len(names) > 10 else "")


def before_init(event: dict) -> bool:
    """A system event other than init, which Claude Code may write before it (2.1.316
    writes ui_invalidate first): read past it. Any other event is checked as the init."""
    return event.get("type") == "system" and event.get("subtype") != "init"


def authentication_failed(event: dict) -> bool:
    """Claude Code's own message that it could not authenticate: an assistant message it
    wrote itself (model <synthetic>), marked "error": "authentication_failed" (no token,
    or a bad or expired one) or saying "Not logged in". The model can't write a
    <synthetic> message, so an article that steered the agent into those words doesn't
    count."""
    if event.get("type") != "assistant":
        return False
    message = event.get("message")
    if not isinstance(message, dict) or message.get("model") != "<synthetic>":
        return False
    if event.get("error") == "authentication_failed":
        return True
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        and block["text"].startswith(NOT_LOGGED_IN) for block in content)


def _synthetic(event: dict) -> bool:
    message = event.get("message")
    return (event.get("type") == "assistant" and isinstance(message, dict)
            and message.get("model") == "<synthetic>")


def _text_of(message: dict) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(block["text"].strip() for block in content if isinstance(block, dict)
                    and block.get("type") == "text" and isinstance(block.get("text"), str)).strip()


def api_failure(event: dict) -> str | None:
    """Claude Code's own message (model <synthetic>) that an API request failed for a
    reason other than authentication (overloaded, rate-limited, unreachable): marked
    with an "error" other than authentication_failed, or "is_api_error_message": true.
    What it says, or None. As for a login failure, the model can't write it."""
    if not _synthetic(event) or authentication_failed(event):
        return None
    error = event.get("error")
    code = error if isinstance(error, str) and error else None
    if code is None and event.get("is_api_error_message") is not True:
        return None
    text = _clip(_text_of(event["message"]), 300)
    said = f"Claude Code's API request failed ({_clip(code, 40)})" if code else "Claude Code's API request failed"
    return f"{said}: {text}" if text else said


def outside_failure(event: dict, category: str, detail: str | None) -> tuple[str, str | None]:
    """The session's category and its API failure's description, after this event. A
    login failure outranks an API failure: it needs Brian, and it explains the rest."""
    if authentication_failed(event):
        return "login", None
    if category == "session":
        said = api_failure(event)
        if said:
            return "api", said
    return category, detail


def categorized(result: dict, category: str, detail: str | None = None) -> dict:
    """The result with its category. A login failure's error is LOGIN_ERROR; an API
    failure keeps the session's own error, or else says what Claude Code said. Neither
    finished the session's work."""
    if category == "login":
        return {**result, "finished_ok": False, "error": LOGIN_ERROR, "category": "login"}
    if category == "api":
        return {**result, "finished_ok": False, "error": result["error"] or detail, "category": "api"}
    return {**result, "category": "session"}


def check_init(event: dict) -> str | None:
    """Why the session must be stopped, or None: the event must be the init event, list
    exactly EXPECTED_TOOLS, and show the narration server connected."""
    if event.get("type") != "system" or event.get("subtype") != "init":
        return "the first event is not the init event"
    tools = event.get("tools")
    if not (isinstance(tools, list) and all(isinstance(t, str) for t in tools)):
        return "the init event has no tool list"
    problems = []
    extra, missing = sorted(set(tools) - EXPECTED_TOOLS), sorted(EXPECTED_TOOLS - set(tools))
    if extra:
        problems.append(f"extra tools: {_names(extra)}")
    if missing:
        problems.append(f"missing tools: {_names(missing)}")
    servers = event.get("mcp_servers")
    status = next((s.get("status") for s in servers if isinstance(s, dict) and s.get("name") == "narration"),
                  None) if isinstance(servers, list) else None
    if status != "connected":
        problems.append(f"the narration server is {_clip(status, 40)}" if isinstance(status, str)
                        else "the narration server is not listed")
    return "; ".join(problems) or None


def _result_error(event: dict) -> str:
    if event.get("type") != "result":
        return "no result event"
    parts = []
    subtype, status, text = event.get("subtype"), event.get("api_error_status"), event.get("result")
    if isinstance(subtype, str) and subtype != "success":
        parts.append(f"the session ended with {_clip(subtype, 60)}")
    if _integer(status) or isinstance(status, str):
        parts.append(f"API error status {_clip(str(status), 20)}")
    if event.get("is_error") is True and isinstance(text, str) and text.strip():
        parts.append(_clip(text.strip(), 300))
    return "; ".join(parts) or "the session ended with an error"


def parse_result(event: dict) -> dict:
    """The service's result from claude's final `result` event, category "session"
    (categorized() sets another). finished_ok means the session ended normally, not
    that the recording is good: the runner's finalize decides that. tokens is the total
    volume: input_tokens + output_tokens + cache_creation_input_tokens +
    cache_read_input_tokens, those present (spend is cost_usd)."""
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    counts = [usage[key] for key in TOKEN_KEYS if _integer(usage.get(key))]
    cost, turns, reason = event.get("total_cost_usd"), event.get("num_turns"), event.get("terminal_reason")
    ok = (event.get("type") == "result" and event.get("is_error") is not True
          and event.get("subtype") in (None, "success"))
    error = None if ok else _result_error(event)
    return {"finished_ok": ok,
            "cost_usd": float(cost) if _number(cost) and math.isfinite(cost) else None,
            "num_turns": turns if _integer(turns) else None,
            "terminal_reason": _clip(reason, 200) if isinstance(reason, str) else None,
            "error": error,
            "tokens": sum(counts) if counts else None,
            "category": "session"}


def failure(error: str, stderr=()) -> dict:
    """A result for a session that didn't end normally (or never started), category
    "session" (categorized() sets another)."""
    if stderr:
        error += "; stderr: " + " | ".join(stderr)
    return {"finished_ok": False, "cost_usd": None, "num_turns": None, "terminal_reason": None,
            "error": _clip(error, MAX_ERROR), "tokens": None, "category": "session"}


def _redact(text: str | None, secrets) -> str | None:
    if text is None:
        return None
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _event(line: str) -> dict | None:
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


# -- one session --

def _no_beat() -> None:
    pass


def run_session(req: Request, cfg: ServiceConfig, beat=_no_beat) -> dict:
    """Run one session to its end and return its result, calling beat() at least every
    POLL_S seconds while it waits on claude. The private directory (the MCP config with
    the agent token, and the session's working directory) is removed and claude's
    process group killed on the way out, whatever ends the session. The agent and OAuth
    tokens are redacted from claude's output as it is read, before anything clips it, so
    no fragment of one reaches the result."""
    secrets = (req.agent_token, os.environ.get(OAUTH_VAR))
    private = Path(tempfile.mkdtemp(prefix="narration-session-"))
    started: list[subprocess.Popen] = []
    try:
        result = _run(req, cfg, private, started, secrets, beat)
    except Exception as e:  # a valid request always gets a result
        result = failure("session service error: " + _redact(f"{type(e).__name__}: {e}", secrets))
    finally:
        for proc in started:
            _stop(proc)
        shutil.rmtree(private, ignore_errors=True)
    return result


def _run(req: Request, cfg: ServiceConfig, private: Path, started: list, secrets, beat) -> dict:
    config = private / "mcp.json"
    _write_private(config, json.dumps(mcp_config(cfg.narration_url, req.agent_token)))
    argv = claude_argv(config, cfg.prompt, req.budget_usd, session_prompt(req.text_rules))
    try:
        proc = subprocess.Popen(argv, cwd=private, env=session_env(os.environ), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", start_new_session=True)
    except FileNotFoundError:
        return failure("claude not found")
    except OSError as e:
        return failure(f"could not start claude: {e}")
    started.append(proc)
    return _watch(proc, req.timeout_s, secrets, beat)


def _watch(proc: subprocess.Popen, timeout_s: int, secrets, beat) -> dict:
    """Read claude's stream-json events until its result, a failed tool check, the end of
    its output, or the deadline. stderr is drained all along, so it can't fill its pipe.
    Claude Code's own failure messages set the result's category on each of those
    endings but the tool check (which comes before any of them)."""
    deadline = time.monotonic() + timeout_s
    lines: queue.Queue = queue.Queue()
    tail: collections.deque = collections.deque(maxlen=STDERR_LINES)
    threading.Thread(target=_pump, args=(proc.stdout, lines, secrets), daemon=True).start()
    drain = threading.Thread(target=_drain, args=(proc.stderr, tail, secrets), daemon=True)
    drain.start()
    checked = False
    category, detail = "session", None
    while True:
        beat()
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise queue.Empty
            line = lines.get(timeout=min(remaining, POLL_S))
        except queue.Empty:
            if deadline - time.monotonic() > 0:
                continue  # no event yet: beat, and wait on
            _stop(proc)
            drain.join(timeout=2)
            return categorized(failure(f"timed out after {timeout_s} s", list(tail)), category, detail)
        if line is None:  # stdout closed: claude is exiting
            break
        event = _event(line)
        if event is None:
            continue
        if not checked:
            if before_init(event):
                continue
            problem = check_init(event)
            if problem:
                _stop(proc)
                return failure(f"tool check failed: {problem}")
            checked = True
        elif event.get("type") == "result":
            _finish(proc)
            return categorized(parse_result(event), category, detail)
        else:
            category, detail = outside_failure(event, category, detail)
    status = _finish(proc)
    drain.join(timeout=2)
    return categorized(failure(f"no result event (claude exited with status {status})", list(tail)),
                       category, detail)


def _pump(stream, lines: queue.Queue, secrets) -> None:
    # Redacted as raw JSON text: both tokens are URL-safe characters, which JSON
    # never escapes, so this catches them inside any string of the event.
    try:
        for line in stream:
            lines.put(_redact(line, secrets))
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


def _drain(stream, tail: collections.deque, secrets) -> None:
    try:
        for line in stream:
            line = _redact(line, secrets).strip()
            if line:
                tail.append(_clip(line, STDERR_LINE_CHARS))
    except (OSError, ValueError):
        pass


def _stop(proc: subprocess.Popen) -> None:
    """Kill claude's process group (claude and anything it started), then reap claude."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # gone already (macOS: EPERM for a group of zombies)
        pass
    proc.wait()


def _finish(proc: subprocess.Popen) -> int:
    """Give claude EXIT_GRACE_S to exit on its own, then stop it. Its exit status."""
    try:
        proc.wait(timeout=EXIT_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    _stop(proc)
    return proc.returncode


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


# -- the handoff directory --

def write_result(handoff: Path, item_id: str, result: dict) -> None:
    """results/<item_id>.json, 0600, written atomically (a temp file, then os.replace),
    as the runner writes its requests."""
    if not valid_item_id(item_id):  # a path is built from it
        raise ValueError("not a work item id")
    results = handoff / "results"
    results.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = results / f".{item_id}.tmp"
    tmp.unlink(missing_ok=True)
    try:
        _write_private(tmp, json.dumps(result))
        os.replace(tmp, results / f"{item_id}.json")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _pending(directory: Path) -> list[Path]:
    """Request files, by name; temp files (.<id>.tmp) are left alone."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.name.endswith(".json") and not p.name.startswith("."))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def _item_id_of(body) -> str | None:
    item_id = body.get("item_id") if isinstance(body, dict) else None
    return item_id if valid_item_id(item_id) else None


def _reject(handoff: Path, path: Path, why: str) -> None:
    log.warning("rejected %r: %s", path.name, why)
    rejected = handoff / "rejected"
    rejected.mkdir(mode=0o700, exist_ok=True)
    os.replace(path, rejected / path.name)


def recover(handoff: Path) -> None:
    """A request left in running/ (the service stopped during its session): the session
    is gone, so it gets a result saying so, unless its result was written already."""
    for path in _pending(handoff / "running"):
        item_id = _item_id_of(_read_json(path))
        if item_id is None:
            _reject(handoff, path, "no valid item_id")
            continue
        if not (handoff / "results" / f"{item_id}.json").exists():
            log.warning("%s: the service restarted during its session", item_id)
            write_result(handoff, item_id, failure("service restarted during the session"))
        path.unlink(missing_ok=True)


class Heartbeat:
    """Touches the heartbeat file. A failure is logged once until a touch works again,
    and never stops the service: the runner, seeing the heartbeat go stale, says so."""

    def __init__(self, path: Path):
        self.path = path
        self.failing = False

    def __call__(self) -> None:
        try:
            self.path.touch()
        except OSError as e:
            if not self.failing:
                log.warning("cannot touch the heartbeat %s: %s", self.path, e)
            self.failing = True
        else:
            self.failing = False


def serve_once(handoff: Path, cfg: ServiceConfig, beat=_no_beat) -> bool:
    """Take the next request, if any, and see it through, calling beat() while its
    session runs. Whether there was one."""
    pending = _pending(handoff / "requests")
    if not pending:
        return False
    running = handoff / "running" / pending[0].name
    running.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.replace(pending[0], running)
    except FileNotFoundError:
        return True
    _handle(handoff, running, cfg, beat)
    return True


def _age(path: Path) -> float:
    """Seconds since the file was written (os.replace keeps its mtime)."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return 0.0


def _handle(handoff: Path, running: Path, cfg: ServiceConfig, beat) -> None:
    age = _age(running)
    body = _read_json(running)
    try:
        req = validate_request(body)
    except ValueError as e:
        item_id = _item_id_of(body)
        if item_id is None:
            _reject(handoff, running, str(e))
            return
        log.warning("%s: invalid request: %s", item_id, e)
        write_result(handoff, item_id, failure(f"invalid request: {e}"))
        running.unlink(missing_ok=True)
        return
    if stale_request(age, req.timeout_s):  # the runner has stopped waiting for it
        log.warning("%s: stale request (%.0f s old)", req.item_id, age)
        write_result(handoff, req.item_id, failure("stale request"))
        running.unlink(missing_ok=True)
        return
    log.info("%s: session started (budget %s USD, timeout %s s)", req.item_id, req.budget_usd, req.timeout_s)
    try:
        result = run_session(req, cfg, beat=beat)
    except Exception as e:  # run_session catches its own; this is the last line
        error = _redact(f"{type(e).__name__}: {e}", (req.agent_token, os.environ.get(OAUTH_VAR)))
        result = failure(f"session service error: {error}")
    write_result(handoff, req.item_id, result)
    running.unlink(missing_ok=True)
    log.info("%s: session ended: finished_ok=%s cost_usd=%s turns=%s error=%s", req.item_id,
             result["finished_ok"], result["cost_usd"], result["num_turns"], result["error"])


def serve(handoff: Path, cfg: ServiceConfig, poll_s: float = POLL_S, until=None) -> None:
    """One request at a time, polling requests/ every poll_s seconds and touching the
    heartbeat each time (and while a session runs). `until` (for tests) is asked before
    each poll whether to stop."""
    for sub in ("requests", "running", "results"):
        (handoff / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    heartbeat = Heartbeat(handoff / HEARTBEAT)
    heartbeat()
    recover(handoff)
    while until is None or not until():
        heartbeat()
        if not serve_once(handoff, cfg, heartbeat):
            time.sleep(poll_s)


def _terminate(signum, frame) -> None:
    # SystemExit unwinds run_session, so its finally kills the session and removes the token file.
    raise SystemExit(128 + signum)


def main(environ=None) -> None:
    environ = os.environ if environ is None else environ
    cfg = ServiceConfig.from_env(environ)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    handoff = Path(environ.get("HANDOFF_DIR") or "/handoff")
    signal.signal(signal.SIGTERM, _terminate)
    log.info("serving %s for %s, prompt %s", handoff, cfg.narration_url, cfg.prompt_path)
    serve(handoff, cfg)


if __name__ == "__main__":
    main()
