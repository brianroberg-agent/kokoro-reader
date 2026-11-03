"""Client for the narration server's runner tools (MCP) and its plain HTTP routes."""

import asyncio
import contextlib
import json
from pathlib import Path

import requests
from mcp.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


class NarrationError(Exception):
    pass


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

        result = asyncio.run(go())
        text = result.content[0].text if result.content else ""
        if result.is_error:
            raise NarrationError(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise NarrationError(f"{tool}: reply is not JSON") from e

    def health(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=10)
            return resp.status_code == 200 and resp.json().get("ok") is True
        except (requests.RequestException, ValueError):
            return False

    def open(self, text: str, metadata: dict) -> tuple[str, str]:
        out = self._call("open", {"article_text": text, "metadata": metadata})
        return out["item_id"], out["agent_token"]

    def finalize(self, item_id: str, force: bool = False) -> dict:
        return self._call("finalize", {"item_id": item_id, "force": force})

    def status(self, item_id: str) -> dict:
        return self._call("status", {"item_id": item_id})

    def download_mp3(self, item_id: str, dest: Path) -> Path:
        resp = requests.get(f"{self.base_url}/runner/files/{item_id}/episode.mp3",
                            headers=self._headers(), timeout=120)
        if resp.status_code != 200:
            raise NarrationError(f"download of {item_id} returned {resp.status_code}")
        dest.write_bytes(resp.content)
        return dest
