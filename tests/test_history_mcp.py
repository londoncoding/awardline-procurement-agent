"""The new product's MCP surface exposes exactly one research action."""

import unittest


class HistoryMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_tool_calls_buyer_history_http_action(self):
        from mcp import Client
        from awardline.api import create_app
        from awardline.history_mcp import create_history_mcp_server

        item = {
            "ocid": "ocds-history-mcp", "id": "r1", "date": "2026-09-20T12:00:00Z",
            "buyer": {"name": "Example Trust", "identifier": {"scheme": "GB-NHS", "id": "123"}},
            "tender": {"classification": {"scheme": "CPV", "id": "72200000"}},
            "awards": [{"id": "a1", "status": "active", "date": "2026-09-20T12:00:00Z",
                        "suppliers": [{"id": "GB-COH-456", "name": "Supplier"}]}],
        }

        class Repository:
            def releases_for_buyer(self, buyer_id):
                return [item]

        server = create_history_mcp_server(create_app("unused", history_repository=Repository(), research_demo=True))
        async with Client(server) as client:
            tools = await client.list_tools()
            result = await client.call_tool("awardline_buyer_history", {"buyer_id": "GB-NHS:123", "category": "7220"})
        self.assertEqual([tool.name for tool in tools.tools], ["awardline_buyer_history"])
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["awards"][0]["suppliers"][0]["id"], "GB-COH-456")


if __name__ == "__main__":
    unittest.main()
