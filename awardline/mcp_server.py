"""Local stdio MCP tools over the same API and PostgreSQL entitlement ledger.

The pilot token stays in the server process environment; the agent never passes it
as a tool argument. This module intentionally offers trial unlocks only.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from mcp.server import MCPServer

from .api import create_app


def create_mcp_server(api_app, pilot_token: str) -> MCPServer:
    if not pilot_token or not pilot_token.strip():
        raise ValueError("AWARDLINE_PILOT_TOKEN is required")
    server = MCPServer(
        name="awardline-pilot",
        instructions=(
            "Preview is free. Unlock requires a revision from a preview and a caller-persisted UUID "
            "idempotency key. Only trial credits are available; do not attempt a paid purchase. "
            "Retry with the same key or inspect operation status after an uncertain response."
        ),
    )

    async def request(method: str, path: str, *, body: dict | None = None, key: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {pilot_token}"}
        if key is not None:
            headers["Idempotency-Key"] = key
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://awardline.local") as client:
            try:
                response = await client.request(method, path, json=body, headers=headers)
            except httpx.HTTPError:
                return {"error": "api_unavailable", "retryable": True}
        if response.status_code >= 400:
            detail = response.json().get("detail", "api_error")
            return {"error": detail, "http_status": response.status_code, "retryable": response.status_code >= 500}
        result = response.json()
        if method == "POST":
            return {
                "payload": result,
                "operation_id": response.headers["X-Awardline-Operation-ID"],
                "access_type": response.headers["X-Awardline-Access-Type"],
                "trial_remaining": int(response.headers["X-Awardline-Trial-Remaining"]),
            }
        return result

    @server.tool(name="awardline_preview", description="Read free metadata for one procurement dossier; does not spend a trial credit.", structured_output=True)
    async def preview(dossier_id: str) -> dict[str, Any]:
        return await request("GET", f"/v1/dossiers/{quote(dossier_id, safe='')}/preview")

    @server.tool(name="awardline_procurement_intelligence", description="Find up to 20 fresh enriched procurement previews in one four-digit CPV category. Free; no trial credit spent.", structured_output=True)
    async def find_dossiers(category: str, limit: int = 20) -> dict[str, Any]:
        return await request("GET", f"/v1/dossiers?{urlencode({'category': category, 'limit': limit})}")

    @server.tool(name="awardline_unlock_trial", description="Unlock an eligible dossier with one of the business's three trial credits. Reuse the same UUID key on retries.", structured_output=True)
    async def unlock_trial(dossier_id: str, revision: str, idempotency_key: str) -> dict[str, Any]:
        return await request(
            "POST",
            f"/v1/dossiers/{quote(dossier_id, safe='')}/unlock",
            body={"revision": revision, "funding": "trial"},
            key=idempotency_key,
        )

    @server.tool(name="awardline_operation_status", description="Check a previous unlock operation after a timeout or uncertain response.", structured_output=True)
    async def operation_status(operation_id: str) -> dict[str, Any]:
        return await request("GET", f"/v1/operations/{quote(operation_id, safe='')}")

    @server.tool(name="awardline_buyer_history", description="Read exact-ID Contracts Finder award history for one buyer and four-digit CPV category. Local research demo only until the paid path is enabled; never infers an incumbent or renewal.", structured_output=True)
    async def buyer_history(buyer_id: str, category: str, limit: int = 20) -> dict[str, Any]:
        return await request("GET", f"/v1/buyer-history?{urlencode({'buyer_id': buyer_id, 'category': category, 'limit': limit})}")

    return server


def main() -> None:
    dsn = os.environ["AWARDLINE_DATABASE_URL"]
    token = os.environ["AWARDLINE_PILOT_TOKEN"]
    create_mcp_server(create_app(dsn, research_demo=os.environ.get("AWARDLINE_RESEARCH_DEMO") == "1"), token).run(transport="stdio")


if __name__ == "__main__":
    main()
