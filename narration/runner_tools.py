"""Tools for the runner only (never exposed to the agent). A refusal is a ToolError,
as for the agent's tools; the HTTP layer converts it."""
from __future__ import annotations

import json
import os

from narration.agent_tools import ToolError, _checks_used, _sha256
from narration.gate import _meta, album_for, evaluate
from narration.words import unlinked

FINALIZE_WAIT_SECONDS = 600  # longest finalize waits for a running tool call on the item


class RunnerTools:
    def __init__(self, store, converter, g2p):
        # g2p: the engine's grapheme-to-phoneme adapter, for the gate's speech comparison.
        self.store, self.converter, self.g2p = store, converter, g2p

    def open(self, article_text: str, metadata: dict) -> dict:
        """A work item for the article. Its text is kept with every link unwrapped to
        its text and its images dropped (words.unlinked): an article's link to a path
        between slashes would otherwise read as a phonetic override no lookup made."""
        if not isinstance(article_text, str) or not article_text.strip():
            raise ToolError("article_text must be a non-empty string")
        if not isinstance(metadata, dict):
            raise ToolError("metadata must be an object")
        try:  # the store writes it as JSON; refuse before it creates anything
            json.dumps(metadata, allow_nan=False)
        except (TypeError, ValueError) as e:
            raise ToolError(f"metadata must be JSON: {e}") from e
        article_text = unlinked(article_text)
        if not article_text.strip():
            raise ToolError("article_text has no text once its images are dropped")
        item_id, token = self.store.open(article_text, metadata)
        return {"item_id": item_id, "agent_token": token}

    def status(self, item_id: str) -> dict:
        """The item's status (open, finished, finalized or abandoned), its round counts,
        and whether prepared.md is still the text the latest round recorded. A snapshot:
        it doesn't wait for a running tool call."""
        if not self.store.exists(item_id):
            return {"exists": False}
        state = self.store.load_state(item_id)
        prepared = self.store.read_text(item_id, "prepared.md")
        meta = _meta(json.loads(self.store.read_text(item_id, "metadata.json")))
        return {"exists": True, "metadata": meta, "status": state["status"], "records": state["records"],
                "recorded_round": state["recorded_round"], "checks_used": _checks_used(state),
                "checks": len(state["checks"]),
                "prepared_matches_recording": _sha256(prepared) == state.get("recorded_sha256"),
                "finish": state["finish"],
                "mp3": self.store.path(item_id, "episode.mp3").exists()}

    def finalize(self, item_id: str, force: bool = False) -> dict:
        if not self.store.exists(item_id):
            raise ToolError("no such work item")
        lock = self.store.item_lock(item_id)
        # Wait for a running record, check or finish, but not forever: a call stuck in
        # an engine would otherwise keep even finalize(force=True) from abandoning it.
        if not lock.acquire(timeout=FINALIZE_WAIT_SECONDS):
            busy = (f"a tool call is still running on this work item after "
                    f"{FINALIZE_WAIT_SECONDS:g} seconds")
            if not force:
                raise ToolError(f"{busy}; nothing was decided. Call finalize again later.")
            # The stuck call owns the item's state, so leave that alone; revoking the
            # token (under the store's own lock) is enough to end the session's part.
            self.store.revoke(item_id)
            return {"action": "retry", "outcome": None, "notes": "", "mp3": False,
                    "reasons": [f"{busy}; the session's token is revoked and the item is "
                                "left as it was. Record the article again as a new item."]}
        try:
            state = self.store.load_state(item_id)
            if state["status"] == "open":
                if not force:
                    raise ToolError("this work item is still open: the session has not called finish. "
                                    "Pass force=True to abandon it, once the runner's own timeout has passed.")
                state["status"] = "abandoned"  # terminal; the gate below answers retry
                self.store.save_state(item_id, state)
            self.store.revoke(item_id)
            meta = json.loads(self.store.read_text(item_id, "metadata.json"))
            result = evaluate(state, self.store.read_text(item_id, "source.md"),
                              self.store.read_text(item_id, "prepared.md"), meta, self.g2p)
            mp3 = self.store.path(item_id, "episode.mp3")
            if result.action == "publish" and not mp3.exists():
                self._convert(item_id, mp3, meta)
            if state["status"] == "finished":
                state["status"] = "finalized"
                self.store.save_state(item_id, state)
            return {"action": result.action, "outcome": result.outcome, "reasons": result.reasons,
                    "notes": result.notes, "mp3": mp3.exists()}
        finally:
            lock.release()

    def _convert(self, item_id, mp3, meta):
        """recording.wav to episode.mp3, with the tags. episode.mp3 appears only once the
        conversion has succeeded, so a later finalize never takes a partial file for a
        finished one."""
        tmp = mp3.with_name(".tmp-episode.mp3")
        tags = _meta(meta)
        try:
            self.converter.to_mp3(self.store.path(item_id, "recording.wav"), tmp,
                                  title=tags["title"], artist="; ".join(tags["authors"]),
                                  album=album_for(tags))
            os.replace(tmp, mp3)
        finally:
            tmp.unlink(missing_ok=True)
