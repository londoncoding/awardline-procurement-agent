"""Phase 3 contract tests against PostgreSQL. No Redis server is required."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
import unittest
from uuid import uuid4


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ.get("AWARDLINE_TEST_DATABASE_URL")
        if not cls.dsn:
            raise unittest.SkipTest("AWARDLINE_TEST_DATABASE_URL is not set")
        try:
            import psycopg
            from fastapi.testclient import TestClient
        except ImportError as exc:
            raise unittest.SkipTest("Phase 3 test dependencies are not installed") from exc
        cls.psycopg = psycopg
        cls.TestClient = TestClient
        with psycopg.connect(cls.dsn) as connection:
            if not connection.info.dbname.startswith("awardline_test"):
                raise RuntimeError("API integration tests require a database named awardline_test*")

    def setUp(self):
        from awardline.api import create_app

        self.client = self.TestClient(create_app(self.dsn))
        self.customer_id = uuid4()
        self.token = f"pilot-{uuid4().hex}{uuid4().hex}"
        self.create_customer()
        self.dossier_id, self.revision = self.create_dossier()

    def create_customer(self, *, limit=20):
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute(
                """INSERT INTO pilot_customers (id, token_sha256, trial_limit, trial_expires_at)
                   VALUES (%s, %s, %s, %s)""",
                (self.customer_id, sha256(self.token.encode()).hexdigest(), limit, datetime.now(timezone.utc) + timedelta(days=30)),
            )

    def create_dossier(self, *, eligible=True, age_hours=0, dossier_id=None, revision=None, category=None):
        from psycopg.types.json import Jsonb

        dossier_id = dossier_id or uuid4().hex[:20]
        revision = revision or uuid4().hex[:20]
        as_of = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        payload = {
            "dossier": {"id": dossier_id, "revision": revision},
            "buyer": {"published_name": "Example Buyer"},
            "opportunity": {"title": "Cloud services", "deadline": (as_of + timedelta(days=20)).isoformat(), "service_category": category, "published_status": "active"},
            "procurement_route": {"kind": "unknown"},
            "related_awards": {"coverage_status": "bounded", "awards": [{"award_id": "secret-award"}] if eligible else []},
            "material_changes": {"changes": []},
            "published_contact": {"email": "secret@example.org"},
            "provenance": {"data_as_of": as_of.isoformat()},
        }
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute("INSERT INTO dossiers (id, revision, payload, eligible, data_as_of) VALUES (%s, %s, %s, %s, %s)", (dossier_id, revision, Jsonb(payload), eligible, as_of))
            connection.execute(
                """INSERT INTO dossier_heads (id, revision) VALUES (%s, %s)
                   ON CONFLICT (id) DO UPDATE SET revision = EXCLUDED.revision""",
                (dossier_id, revision),
            )
        return dossier_id, revision

    def headers(self, key=None):
        return {"Authorization": f"Bearer {self.token}", "Idempotency-Key": key or str(uuid4())}

    def unlock(self, *, key=None, dossier_id=None, revision=None, funding="trial"):
        return self.client.post(
            f"/v1/dossiers/{dossier_id or self.dossier_id}/unlock",
            json={"revision": revision or self.revision, "funding": funding},
            headers=self.headers(key),
        )

    def used(self):
        with self.psycopg.connect(self.dsn) as connection:
            return connection.execute("SELECT trial_used FROM pilot_customers WHERE id = %s", (self.customer_id,)).fetchone()[0]

    def test_preview_excludes_paid_payload_and_needs_no_token(self):
        response = self.client.get(f"/v1/dossiers/{self.dossier_id}/preview")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("related_awards", response.json())
        self.assertNotIn("published_contact", response.json())
        self.assertEqual(response.json()["price_usdc"], "5.00")

    def test_new_customer_default_trial_limit_is_three(self):
        token = f"pilot-{uuid4().hex}{uuid4().hex}"
        customer_id = uuid4()
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute(
                "INSERT INTO pilot_customers (id, token_sha256, trial_expires_at) VALUES (%s, %s, now() + interval '30 days')",
                (customer_id, sha256(token.encode()).hexdigest()),
            )
            self.assertEqual(
                connection.execute("SELECT trial_limit FROM pilot_customers WHERE id = %s", (customer_id,)).fetchone()[0],
                3,
            )

    def test_unenriched_preview_does_not_offer_purchase(self):
        dossier_id, _ = self.create_dossier(eligible=False)
        response = self.client.get(f"/v1/dossiers/{dossier_id}/preview")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enrichment_available"])
        self.assertIsNone(response.json()["price_usdc"])

    def test_stale_preview_does_not_offer_purchase(self):
        dossier_id, _ = self.create_dossier(age_hours=50)
        response = self.client.get(f"/v1/dossiers/{dossier_id}/preview")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enrichment_available"])
        self.assertIsNone(response.json()["price_usdc"])

    def test_free_search_returns_only_fresh_eligible_previews(self):
        category = str(1000 + int(uuid4().hex[:4], 16) % 8000)
        wanted_id, _ = self.create_dossier(category=category)
        self.create_dossier(category=category, eligible=False)
        self.create_dossier(category=category, age_hours=50)
        response = self.client.get("/v1/dossiers", params={"category": category, "limit": 20})
        self.assertEqual(response.status_code, 200)
        ids = {item["dossier_id"] for item in response.json()["items"]}
        self.assertIn(wanted_id, ids)
        self.assertNotIn("related_awards", str(response.json()))
        self.assertNotIn("published_contact", str(response.json()))

    def test_search_requires_a_filter(self):
        response = self.client.get("/v1/dossiers")
        self.assertEqual(response.status_code, 400)

    def test_trial_unlock_and_retry_have_one_entitlement(self):
        key = str(uuid4())
        first = self.unlock(key=key)
        second = self.unlock(key=key)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(set(first.json()), {"dossier", "buyer", "opportunity", "procurement_route", "related_awards", "material_changes", "published_contact", "provenance"})
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.used(), 1)

    def test_new_key_for_owned_revision_does_not_use_second_trial(self):
        self.assertEqual(self.unlock().status_code, 200)
        second = self.unlock()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.used(), 1)

    def test_same_key_with_different_request_is_conflict(self):
        key = str(uuid4())
        self.assertEqual(self.unlock(key=key).status_code, 200)
        other_id, other_rev = self.create_dossier()
        self.assertEqual(self.unlock(key=key, dossier_id=other_id, revision=other_rev).status_code, 409)
        self.assertEqual(self.used(), 1)

    def test_unenriched_record_cannot_consume_entitlement(self):
        other_id, other_rev = self.create_dossier(eligible=False)
        self.assertEqual(self.unlock(dossier_id=other_id, revision=other_rev).status_code, 409)
        self.assertEqual(self.used(), 0)

    def test_paid_mode_is_disabled(self):
        self.assertEqual(self.unlock(funding="paid").status_code, 503)
        self.assertEqual(self.used(), 0)

    def test_missing_idempotency_key_never_unlocks(self):
        response = self.client.post(
            f"/v1/dossiers/{self.dossier_id}/unlock",
            json={"revision": self.revision, "funding": "trial"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.used(), 0)

    def test_stale_new_revision_cannot_consume_trial(self):
        other_id, other_rev = self.create_dossier(age_hours=72)
        self.assertEqual(self.unlock(dossier_id=other_id, revision=other_rev).status_code, 503)
        self.assertEqual(self.used(), 0)

    def test_operation_status_is_scoped_to_customer(self):
        response = self.unlock()
        operation_id = response.headers["X-Awardline-Operation-ID"]
        self.assertEqual(self.client.get(f"/v1/operations/{operation_id}", headers=self.headers()).json()["status"], "fulfilled")
        other_token = f"pilot-{uuid4().hex}{uuid4().hex}"
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute(
                "INSERT INTO pilot_customers (id, token_sha256, trial_expires_at) VALUES (%s, %s, now() + interval '30 days')",
                (uuid4(), sha256(other_token.encode()).hexdigest()),
            )
        self.assertEqual(self.client.get(f"/v1/operations/{operation_id}", headers={"Authorization": f"Bearer {other_token}"}).status_code, 404)

    def test_owned_revision_survives_staleness_and_revocation_blocks_it(self):
        self.assertEqual(self.unlock().status_code, 200)
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute("UPDATE dossiers SET data_as_of = now() - interval '10 days' WHERE id = %s AND revision = %s", (self.dossier_id, self.revision))
        path = f"/v1/dossiers/{self.dossier_id}/revisions/{self.revision}"
        self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 200)
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute("UPDATE pilot_customers SET enabled = false WHERE id = %s", (self.customer_id,))
        self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 401)

    def test_trial_limit_serializes_concurrent_distinct_unlocks(self):
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute("UPDATE pilot_customers SET trial_limit = 1 WHERE id = %s", (self.customer_id,))
        other_id, other_rev = self.create_dossier()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda pair: self.unlock(dossier_id=pair[0], revision=pair[1]), [(self.dossier_id, self.revision), (other_id, other_rev)]))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        self.assertEqual(self.used(), 1)

    def test_concurrent_same_key_consumes_one_trial(self):
        key = str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.unlock(key=key), range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(self.used(), 1)

    def test_cache_failure_does_not_block_owned_dossier(self):
        from awardline.api import create_app

        class FailingCache:
            def get(self, dossier_id, revision):
                raise OSError("Redis unavailable")

            def put(self, dossier_id, revision, payload):
                raise OSError("Redis unavailable")

        self.assertEqual(self.unlock().status_code, 200)
        client = self.TestClient(create_app(self.dsn, cache=FailingCache()))
        response = client.get(f"/v1/dossiers/{self.dossier_id}/revisions/{self.revision}", headers=self.headers())
        self.assertEqual(response.status_code, 200)

    def test_cache_hit_still_requires_database_authorization(self):
        from awardline.api import create_app

        class MemoryCache:
            def __init__(self):
                self.items = {}

            def get(self, dossier_id, revision):
                return self.items.get((dossier_id, revision))

            def put(self, dossier_id, revision, payload):
                self.items[(dossier_id, revision)] = payload

        self.assertEqual(self.unlock().status_code, 200)
        cache = MemoryCache()
        client = self.TestClient(create_app(self.dsn, cache=cache))
        path = f"/v1/dossiers/{self.dossier_id}/revisions/{self.revision}"
        self.assertEqual(client.get(path, headers=self.headers()).status_code, 200)
        self.assertIn((self.dossier_id, self.revision), cache.items)
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute("UPDATE pilot_customers SET enabled = false WHERE id = %s", (self.customer_id,))
        self.assertEqual(client.get(path, headers=self.headers()).status_code, 401)

    def test_database_outage_fails_closed_even_with_cached_payload(self):
        from awardline.api import create_app

        class MemoryCache:
            def get(self, dossier_id, revision):
                return {"dossier": {"id": dossier_id}}

            def put(self, dossier_id, revision, payload):
                pass

        client = self.TestClient(create_app("host=127.0.0.1 port=9 dbname=awardline_test_utf8 user=awardline connect_timeout=1", cache=MemoryCache()))
        response = client.get(f"/v1/dossiers/{self.dossier_id}/revisions/{self.revision}", headers=self.headers())
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
