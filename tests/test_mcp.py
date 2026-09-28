"""Agent tool contract tests against the real local entitlement ledger."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
import unittest
from uuid import uuid4


class McpToolTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ.get("AWARDLINE_TEST_DATABASE_URL")
        if not cls.dsn:
            raise unittest.SkipTest("AWARDLINE_TEST_DATABASE_URL is not set")
        try:
            import httpx
            import mcp
            import psycopg
        except ImportError as exc:
            raise unittest.SkipTest("MCP integration dependencies are not installed") from exc
        cls.httpx = httpx
        cls.psycopg = psycopg
        with psycopg.connect(cls.dsn) as connection:
            if not connection.info.dbname.startswith("awardline_test"):
                raise RuntimeError("MCP integration tests require a database named awardline_test*")

    def setUp(self):
        from awardline.api import create_app
        from awardline.mcp_server import create_mcp_server
        from psycopg.types.json import Jsonb

        self.dossier_id = uuid4().hex[:20]
        self.revision = uuid4().hex[:20]
        self.category = str(1000 + int(self.dossier_id[:4], 16) % 8000)
        self.token = f"pilot-{uuid4().hex}{uuid4().hex}"
        self.customer_id = uuid4()
        now = datetime.now(timezone.utc)
        payload = {
            "dossier": {"id": self.dossier_id, "revision": self.revision},
            "buyer": {"published_name": "Example Buyer"},
            "opportunity": {"title": "Cloud services", "deadline": (now + timedelta(days=20)).isoformat(), "service_category": self.category, "published_status": "active"},
            "procurement_route": {"kind": "unknown"},
            "related_awards": {"coverage_status": "bounded", "awards": [{"award_id": "private-award"}]},
            "material_changes": {"changes": []},
            "published_contact": {"email": "private@example.org"},
            "provenance": {"data_as_of": now.isoformat()},
        }
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute(
                "INSERT INTO pilot_customers (id, token_sha256, trial_expires_at) VALUES (%s, %s, %s)",
                (self.customer_id, sha256(self.token.encode()).hexdigest(), now + timedelta(days=30)),
            )
            connection.execute(
                "INSERT INTO dossiers (id, revision, payload, eligible, data_as_of) VALUES (%s, %s, %s, true, %s)",
                (self.dossier_id, self.revision, Jsonb(payload), now),
            )
            connection.execute("INSERT INTO dossier_heads (id, revision) VALUES (%s, %s)", (self.dossier_id, self.revision))
        self.api = create_app(self.dsn)
        self.server = create_mcp_server(self.api, self.token)

    async def call(self, name, arguments):
        from mcp import Client

        async with Client(self.server) as client:
            return await client.call_tool(name, arguments)

    def used(self):
        with self.psycopg.connect(self.dsn) as connection:
            return connection.execute("SELECT trial_used FROM pilot_customers WHERE id = %s", (self.customer_id,)).fetchone()[0]

    async def test_preview_exposes_only_free_metadata(self):
        result = await self.call("awardline_preview", {"dossier_id": self.dossier_id})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["dossier_id"], self.dossier_id)
        self.assertNotIn("related_awards", str(result.structured_content))
        self.assertEqual(self.used(), 0)

    async def test_discovery_returns_free_previews_without_trial_spend(self):
        result = await self.call("awardline_procurement_intelligence", {"category": self.category, "limit": 20})
        self.assertFalse(result.is_error)
        self.assertIn(self.dossier_id, {item["dossier_id"] for item in result.structured_content["items"]})
        self.assertNotIn("private-award", str(result.structured_content))
        self.assertEqual(self.used(), 0)

    async def test_tool_surface_has_no_payment_or_token_parameter(self):
        from mcp import Client

        async with Client(self.server) as client:
            listed = await client.list_tools()
        self.assertEqual(
            {tool.name for tool in listed.tools},
            {"awardline_procurement_intelligence", "awardline_preview", "awardline_unlock_trial", "awardline_operation_status", "awardline_buyer_history"},
        )
        for tool in listed.tools:
            self.assertNotIn("token", str(tool.input_schema).lower())
            self.assertNotIn(self.token, str(tool))

    async def test_trial_unlock_and_same_operation_retry_use_one_credit(self):
        arguments = {"dossier_id": self.dossier_id, "revision": self.revision, "idempotency_key": str(uuid4())}
        first = await self.call("awardline_unlock_trial", arguments)
        second = await self.call("awardline_unlock_trial", arguments)
        self.assertFalse(first.is_error)
        self.assertEqual(first.structured_content["payload"], second.structured_content["payload"])
        self.assertEqual(first.structured_content["operation_id"], second.structured_content["operation_id"])
        self.assertEqual(self.used(), 1)

    async def test_status_is_available_after_unlock(self):
        first = await self.call("awardline_unlock_trial", {
            "dossier_id": self.dossier_id, "revision": self.revision, "idempotency_key": str(uuid4())
        })
        status = await self.call("awardline_operation_status", {"operation_id": first.structured_content["operation_id"]})
        self.assertFalse(status.is_error)
        self.assertEqual(status.structured_content["status"], "fulfilled")

    async def test_invalid_key_never_consumes_trial(self):
        result = await self.call("awardline_unlock_trial", {
            "dossier_id": self.dossier_id, "revision": self.revision, "idempotency_key": "bad-key"
        })
        self.assertEqual(result.structured_content["error"], "invalid_idempotency_key")
        self.assertEqual(self.used(), 0)

    async def test_buyer_history_tool_uses_same_http_action(self):
        from awardline.api import create_app
        from awardline.mcp_server import create_mcp_server

        source_release = {
            "ocid": "ocds-test-history", "id": "release-1", "date": "2026-09-20T12:00:00Z",
            "buyer": {"name": "Example Trust", "identifier": {"scheme": "GB-NHS", "id": "123"}},
            "tender": {"items": [{"classification": {"scheme": "CPV", "id": "72200000"}}]},
            "awards": [{"id": "a1", "status": "active", "date": "2026-09-20T12:00:00Z",
                        "suppliers": [{"id": "GB-COH-456", "name": "Supplier"}]}],
        }

        class FixtureRepository:
            def releases_for_buyer(self, buyer_id):
                return [source_release]

        api = create_app(self.dsn, history_repository=FixtureRepository(), research_demo=True)
        server = create_mcp_server(api, self.token)
        from mcp import Client
        async with Client(server) as client:
            result = await client.call_tool("awardline_buyer_history", {"buyer_id": "GB-NHS:123", "category": "7220"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["awards"][0]["suppliers"][0]["id"], "GB-COH-456")
        self.assertEqual(self.used(), 0)
