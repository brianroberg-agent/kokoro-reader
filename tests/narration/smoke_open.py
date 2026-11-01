"""Open a work item through the runner endpoint; print the result as one JSON line.

Used by smoke_claude.sh: python smoke_open.py RUNNER_TOKEN [--url http://127.0.0.1:8771/runner/mcp]
"""
import argparse
import asyncio
import sys

from mcp.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

DEFAULT_URL = "http://127.0.0.1:8771/runner/mcp"


async def open_item(url: str, token: str) -> str:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(url, http_client=http)) as client:
        result = await client.call_tool("open", {"article_text": "King Ivo XIV met the family.",
                                                 "metadata": {"title": "Smoke"}})
        if result.is_error:
            sys.exit(f"open failed: {result.content[0].text}")
        # A -> dict tool result is JSON text in content[0].text (structured_content is None).
        return result.content[0].text.replace("\n", " ")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("token", help="the runner's bearer token")
    p.add_argument("--url", default=DEFAULT_URL, help=f"the runner MCP endpoint (default {DEFAULT_URL})")
    args = p.parse_args(argv)
    print(asyncio.run(open_item(args.url, args.token)))


if __name__ == "__main__":
    main()
