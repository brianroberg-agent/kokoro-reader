"""Client for the narration server's runner tools (MCP) and its plain HTTP routes."""

import asyncio
import contextlib
import json
import os
from pathlib import Path

import requests
from mcp.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


class NarrationError(Exception):
    pass


class NarrationUnavailable(NarrationError):
    """The server could not be reached or answered with a transport-level failure."""


@contextlib.asynccontextmanager
async def _mcp_client(url: str, token: str):
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(url, http_client=http)) as client:
        yield client


class NarrationClient:
    def __init__(self, base_url: str, runner_token: str, client_factory=_mcp_client):
        self.base_url = base_url.rstrip("/")
        self._token = runner_token
        self._factory = client_factory

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, tool: str, args: dict) -> dict:
        async def go():
            async with self._factory(f"{self.base_url}/runner/mcp", self._token) as client:
                return await client.call_tool(tool, args)

        try:
            result = asyncio.run(go())
        except Exception as e:  # incl. ExceptionGroup from the MCP/anyio layer
            raise NarrationUnavailable(f"{tool}: {type(e).__name__}") from e
        text = getattr(result.content[0], "text", None) if result.content else None
        if not isinstance(text, str):
            raise NarrationError(f"{tool}: reply has no text")
        if result.is_error:
            raise NarrationError(text)
        try:
            out = json.loads(text)
        except json.JSONDecodeError as e:
            raise NarrationError(f"{tool}: reply is not JSON") from e
        if not isinstance(out, dict):
            raise NarrationError(f"{tool}: reply is not an object")
        return out

    def health(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=10)
            body = resp.json()
            return resp.status_code == 200 and isinstance(body, dict) and body.get("ok") is True
        except (requests.RequestException, ValueError):
            return False

    def open(self, text: str, metadata: dict) -> tuple[str, str]:
        out = self._call("open", {"article_text": text, "metadata": metadata})
        item_id, agent_token = out.get("item_id"), out.get("agent_token")
        if not isinstance(item_id, str) or not isinstance(agent_token, str):
            raise NarrationError("open: reply lacks item_id or agent_token")
        return item_id, agent_token

    def finalize(self, item_id: str, force: bool = False) -> dict:
        return self._call("finalize", {"item_id": item_id, "force": force})

    def status(self, item_id: str) -> dict:
        return self._call("status", {"item_id": item_id})

    def download_mp3(self, item_id: str, dest: Path) -> Path:
        tmp = dest.with_name(f".{dest.name}.part")
        try:
            resp = requests.get(f"{self.base_url}/runner/files/{item_id}/episode.mp3",
                                headers=self._headers(), timeout=120, stream=True)
            try:
                if resp.status_code == 401 or resp.status_code >= 500:
                    raise NarrationUnavailable(f"download of {item_id} returned {resp.status_code}")
                if resp.status_code != 200:
                    raise NarrationError(f"download of {item_id} returned {resp.status_code}")
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)
            finally:
                resp.close()
            os.replace(tmp, dest)
        except requests.RequestException as e:
            raise NarrationUnavailable(f"download of {item_id}: {type(e).__name__}") from e
        finally:
            tmp.unlink(missing_ok=True)
        return dest
