"""Buyer-history MCP adapter with a free preview and one research action.

The Streamable HTTP option binds to localhost only. There is no remotely
deployed or billable endpoint in this prototype.
"""

from __future__ import annotations

import argparse
import os
from typing import Any
from urllib.parse import urlencode

import httpx
from mcp.server import MCPServer

from .api import create_app


def create_history_mcp_server(api_app) -> MCPServer:
    server = MCPServer(
        name="awardline-buyer-history",
        instructions="Research one exact published buyer ID and four-digit CPV category. Awards are observed source evidence; do not infer incumbency, legal-entity identity or future renewal. The local demo does not charge.",
    )

    async def request(path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://awardline.local") as client:
            try:
                response = await client.get(path)
            except httpx.HTTPError:
                return {"error": "api_unavailable", "retryable": True}
        if response.status_code >= 400:
            return {"error": response.json().get("detail", "api_error"), "http_status": response.status_code, "retryable": response.status_code >= 500}
        return response.json()

    @server.tool(
        name="awardline_buyer_history_preview",
        description="Free buyer/category availability, count and coverage; no supplier IDs, values or trial/payment spend.",
        structured_output=True,
    )
    async def buyer_history_preview(buyer_id: str, category: str) -> dict[str, Any]:
        return await request("/v1/buyer-history/preview?" + urlencode({"buyer_id": buyer_id, "category": category}))

    @server.tool(
        name="awardline_buyer_history",
        description="Get bounded Contracts Finder award history for one exact buyer ID and four-digit CPV category, with supplier IDs, values, dates, and source links.",
        structured_output=True,
    )
    async def buyer_history(buyer_id: str, category: str, limit: int = 20) -> dict[str, Any]:
        return await request("/v1/buyer-history?" + urlencode({"buyer_id": buyer_id, "category": category, "limit": limit}))

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Awardline buyer-history MCP server locally")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--research-demo", action="store_true", help="Allow local, no-charge buyer-history reads")
    args = parser.parse_args()
    dsn = os.environ.get("AWARDLINE_DATABASE_URL")
    if not dsn:
        parser.error("AWARDLINE_DATABASE_URL is required")
    server = create_history_mcp_server(create_app(dsn, research_demo=args.research_demo))
    if args.transport == "streamable-http":
        server.run(transport="streamable-http", host="127.0.0.1", port=args.port, streamable_http_path="/mcp")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
