"""The narration HTTP app: two MCP servers behind one bearer-token gate.

/agent/mcp serves the six agent tools, each resolving its work item from the
caller's per-item token; /runner/mcp serves open, finalize and status, and
/runner/files/{item_id}/episode.mp3 the finished MP3, both for the runner token;
/health is open. BearerAuth checks every token before a request reaches a route,
and matches absolute paths, so the app must not be served under a root path.

The tool schemas are the agent's first feedback, so they are built from
agent_tools' own rules (ISSUE_KINDS, WHERE_RE, MAX_*, VOICE_RE, _plain) rather
than copies of them; agent_tools enforces the same rules again on every call.
"""
from __future__ import annotations

import contextlib
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError as SDKToolError
from mcp.server.transport_security import TransportSecuritySettings

from narration import agent_tools as rules
from narration.agent_tools import (ISSUE_KINDS, MAX_CHECKS, MAX_EDIT_TEXT, MAX_FIELD, MAX_ISSUES,
                                   MAX_NOTE, MAX_RECORDS, MAX_WORD, OUTCOMES, VOICE_RE, WHERE_RE,
                                   ToolError)
from narration.auth import BearerAuth, bearer
from narration.words import MAX_SUBSTITUTIONS

AGENT_INSTRUCTIONS = (
    "Tools for recording one article. Call one tool at a time: a call made while another "
    "is still running is refused. Start with get_text. Edit the prepared text only with "
    "replace_text, and never change what the article says: declare every word change as a "
    "substitution in finish. To fix a pronunciation, call phonemes(word) and write the "
    "override [word](/phonemes/) with exactly what it returned; never write phonemes by "
    "hand. If you look up a respelling of a word instead, declare it as a substitution "
    "(original: the word, spoken: the respelling). Record, read its speech_warnings, then "
    f"check: at most {MAX_RECORDS} record rounds and {MAX_CHECKS} checks. The recorded text "
    "is hashed, so after any replace_text, record again before check or finish. End with "
    "finish."
)
# The record and check tools' descriptions, with the limits agent_tools enforces.
RECORD_DESCRIPTION = (
    f"Generate audio from the prepared text as it is now. At most {MAX_RECORDS} rounds per "
    "article; a round counts even if the engine fails. Refused once no checks are left, "
    "since a new round could not be checked. Returns round, duration_seconds, records_left "
    "and speech_warnings: places where the engine would say something the article doesn't "
    "(or one entry saying nothing was compared, when the words differ from the article's: "
    "finish compares those with your declared substitutions). Read speech_warnings before "
    "spending a check: finish refuses the same mismatches, so fix them with replace_text "
    "and record again. speed: 0.5 to 2.0."
)
# verify_audio.precheck_warnings: pace outside 100-250 WPM, or silences over 5 s.
CHECK_DESCRIPTION = (
    "Verify the latest recording against the prepared text; returns the verifier's report "
    "and precheck_warnings: a pace outside 100-250 words per minute (audio cut short or "
    "with extra content) and silences over 5 seconds. finish needs a check with no "
    f"precheck_warnings. At most {MAX_CHECKS} checks per article, a failed one included. "
    "Refused if the prepared text changed since the latest record: record again first."
)

# Literal over the tuples themselves: X[a, b] is X[(a, b)], so this is
# Literal["mispronunciation", ...] without retyping the list.
IssueKind = Literal[ISSUE_KINDS]
Outcome = Literal[OUTCOMES]
# Pydantic searches with its pattern; WHERE_RE and VOICE_RE are anchored with ^...$,
# and the schema's regex engine (Rust's) takes $ as the end of the text only, as
# agent_tools' fullmatch does.
Voice = Annotated[str, Field(pattern=VOICE_RE.pattern,
                             description="an engine voice: two letters, an underscore, a name")]
# _speed's bounds (0.5 to 2.0). strict: a number only, so true is refused as _speed refuses it.
Speed = Annotated[float, Field(ge=0.5, le=2.0, strict=True)]

PLAIN_RULE = (f"One line of plain text: at most {MAX_FIELD} characters, with a letter or "
              "digit, no line breaks or control characters, and none of "
              + " ".join(sorted(rules.NOT_PLAIN)) + ". Published in the episode notes as written.")


class Issue(BaseModel):
    """Something still wrong in the recording."""
    # kind and where are what the episode notes publish, so both are closed forms;
    # note is free text, kept in result.json only. agent_tools._issue checks the same.
    kind: IssueKind
    where: str = Field(pattern=WHERE_RE.pattern,
                       description="where in the recording, as a timestamp m:ss (like 12:40)")
    note: str = Field(default="", max_length=MAX_NOTE,
                      description="a short note for the editor; never published")


class Substitution(BaseModel):
    """A word change in the prepared text: what the article says, and what is read instead."""
    original: str = Field(max_length=MAX_FIELD, description=f"the article's words. {PLAIN_RULE}")
    spoken: str = Field(max_length=MAX_FIELD, description=f"what is read instead. {PLAIN_RULE}")
    why: str = Field(default="", max_length=MAX_FIELD,
                     description=f"optional: why the change. {PLAIN_RULE}")

    @field_validator("original", "spoken", "why")
    @classmethod
    def plain_text(cls, value: str, info) -> str:
        # agent_tools._plain, so the schema refuses what finish would, with its message.
        try:
            return rules._plain(value, info.field_name, optional=info.field_name == "why")
        except ToolError as e:
            raise ValueError(str(e)) from e


def run(fn, *args):
    """Call a tool function, turning our refusals and the engines' errors into the
    SDK's ToolError so the caller sees the message (the SDK withholds any other
    exception's text, reporting only "Error executing tool <name>"). ValueError
    and RuntimeError are what verify_audio and text_to_speech raise, e.g.
    verify_audio's "add [BREAK] markers" refusal of a single call over 15 minutes,
    and what the engine adapters raise. Nothing else is converted: a bug stays a
    crash, logged with its traceback, not a refusal."""
    try:
        return fn(*args)
    except (ToolError, ValueError, RuntimeError) as e:
        raise SDKToolError(str(e)) from e


def build_app(store, agent_tools, runner_tools, runner_token, allowed_hosts: list[str]):
    """The whole app, behind BearerAuth. allowed_hosts: the Host values accepted
    (host:port), the DNS-rebinding guard. BearerAuth raises ValueError for an
    unusable runner token."""
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=list(allowed_hosts),
                                         allowed_origins=[])  # no browser calls this
    agent = MCPServer("narration", instructions=AGENT_INSTRUCTIONS)
    runner = MCPServer("narration-runner")

    def item(ctx: Context) -> str:
        """The caller's work item, from its token (BearerAuth has already checked it;
        this also catches a token revoked since)."""
        item_id = store.item_for_token(bearer(ctx.headers) or "")
        if item_id is None:
            raise SDKToolError("unknown or expired work-item token")
        return item_id

    @agent.tool()
    def get_text(ctx: Context) -> dict:
        """The prepared text (what record reads aloud), the article's metadata, and how
        many record rounds are used and left. Start here. Call one tool at a time: a
        call made while another is still running is refused."""
        return run(agent_tools.get_text, item(ctx))

    @agent.tool()
    def replace_text(ctx: Context,
                     old: Annotated[str, Field(min_length=1, max_length=MAX_EDIT_TEXT)],
                     new: Annotated[str, Field(max_length=MAX_EDIT_TEXT)]) -> dict:
        """Replace old, which must appear exactly once in the prepared text, with new.
        For pacing, punctuation and pronunciation overrides. Never change what the
        article says without declaring it as a substitution in finish. The recorded
        text is hashed: after any replace_text, record again before check or finish."""
        return run(agent_tools.replace_text, item(ctx), old, new)

    @agent.tool(description=RECORD_DESCRIPTION)
    def record(ctx: Context, voice: Voice = agent_tools.default_voice, speed: Speed = 1.0) -> dict:
        return run(agent_tools.record, item(ctx), voice, speed)

    @agent.tool(description=CHECK_DESCRIPTION)
    def check(ctx: Context) -> dict:
        return run(agent_tools.check, item(ctx))

    @agent.tool()
    def phonemes(ctx: Context,
                 word: Annotated[str, Field(max_length=MAX_WORD,
                                            description="one word: letters, with apostrophes, "
                                                        "hyphens or periods")]) -> dict:
        """The engine's phonemes for one word; look words up one call at a time (a
        call made while another is running is refused). To fix a pronunciation, write
        the override [word](/phonemes/) in the prepared text with exactly the phonemes
        this returns; never write phonemes by hand: finish refuses an override whose
        phonemes no lookup returned. You may look up a respelling (Pengelly for Pengelley) and use
        its phonemes in the word's override, but then declare it in finish as a
        substitution, original the word and spoken the respelling. The same word in
        other capitals (Nox for NOX) counts as a respelling."""
        return run(agent_tools.phonemes, item(ctx), word)

    @agent.tool()
    def finish(ctx: Context, outcome: Outcome,
               remaining_issues: Annotated[list[Issue], Field(max_length=MAX_ISSUES)] = [],
               substitutions: Annotated[list[Substitution],
                                        Field(max_length=MAX_SUBSTITUTIONS)] = []) -> dict:
        """End the session; your token stops working. outcome: clean (no remaining
        issues), minor (small issues) or hold (content missing or garbled; not
        published); minor and hold list at least one remaining issue. clean and minor
        need the latest round recorded from the prepared text as it is now, and
        checked with no pre-check warnings.
        remaining_issues: each a kind, a where (a timestamp like 12:40) and a short
        note for the editor; the published notes carry only the kind and timestamp.
        substitutions: every word change in the prepared text, as original, spoken
        and why, each declared once, including a respelling whose phonemes an
        override uses. Each must replace some of the article's words, not only add
        to them, and may not add a web or e-mail address, a path or a dotted word the
        article doesn't have. All of them together may add only a few words."""
        return run(agent_tools.finish, item(ctx), outcome,
                   [i.model_dump() for i in remaining_issues],
                   [s.model_dump() for s in substitutions])

    @runner.tool(name="open")
    def open_item(article_text: str, metadata: dict[str, Any]) -> dict:
        """Create a work item from the article's markdown and its metadata (JSON).
        The text is kept with every link unwrapped to its text and its images dropped,
        as the engine would read them (a link to a path is never a phonetic override).
        Returns item_id and agent_token, the bearer token for the agent session's
        /agent/mcp; it stops working at finish or finalize."""
        return run(runner_tools.open, article_text, metadata)

    @runner.tool()
    def finalize(item_id: str, force: bool = False) -> dict:
        """Run the pre-publish gate on a finished item and, on publish, make the MP3
        (then at GET /runner/files/{item_id}/episode.mp3). The gate checks again what
        finish checked: the recording claims, the word check with the declared
        substitutions, and the speech check with the engine's own G2P. Returns
        action (publish, hold or retry), outcome, reasons, notes (the episode notes)
        and mp3. Idempotent. An item still open (no finish yet) is refused: pass
        force=True only once your own session timeout has passed; that revokes the
        agent's token, marks the item abandoned, and returns action retry. An error
        here (say the G2P failed) decides nothing: the item stays as it was; retry."""
        return run(runner_tools.finalize, item_id, force)

    @runner.tool()
    def status(item_id: str) -> dict:
        """A snapshot of a work item: exists (false, and nothing else, for an unknown
        id), metadata, status (open, finished, finalized or abandoned), records
        (record calls so far), recorded_round (the round whose audio is kept),
        checks_used and checks (check calls, and those that returned a report),
        prepared_matches_recording (whether the prepared text is still what the
        latest round recorded), finish (what finish was called with) and mp3
        (whether episode.mp3 exists). It doesn't wait for a running tool call."""
        return run(runner_tools.status, item_id)

    agent_app = agent.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                          json_response=True, transport_security=security)
    runner_app = runner.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                            json_response=True, transport_security=security)

    async def health(request):
        return JSONResponse({"ok": True})

    async def download(request):
        item_id = request.path_params["item_id"]
        # exists() refuses any id that is not 12 lowercase hex, so nothing outside
        # the items folder can be named.
        if not store.exists(item_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        mp3 = store.path(item_id, "episode.mp3")
        if not mp3.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(mp3, media_type="audio/mpeg")

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # A mounted app's own lifespan doesn't run, so this one runs both session managers.
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(agent.session_manager.run())
            await stack.enter_async_context(runner.session_manager.run())
            yield

    inner = Starlette(routes=[
        Route("/health", health),
        Route("/runner/files/{item_id}/episode.mp3", download),  # before the /runner mount
        Mount("/agent", app=agent_app),
        Mount("/runner", app=runner_app),
    ], lifespan=lifespan)
    return BearerAuth(inner, runner_token, store)
