"""Streaming materialization against the real local PostgreSQL schema."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
import unittest
from uuid import uuid4

from awardline.ingest import canonical_json


class MaterializePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ.get("AWARDLINE_TEST_DATABASE_URL")
        if not cls.dsn:
            raise unittest.SkipTest("AWARDLINE_TEST_DATABASE_URL is not set")
        try:
            import psycopg
        except ImportError as exc:
            raise unittest.SkipTest("psycopg is not installed") from exc
        cls.psycopg = psycopg
        with psycopg.connect(cls.dsn) as connection:
            if not connection.info.dbname.startswith("awardline_test"):
                raise RuntimeError("Materializer tests require an awardline_test* database")

    def insert_release(self, connection, item):
        from psycopg.types.json import Jsonb

        connection.execute(
            """INSERT INTO source_releases (id, source, ocid, release_id, content_sha256, release)
               VALUES (%s, 'contracts_finder', %s, %s, %s, %s)""",
            (uuid4(), item["ocid"], item["id"], sha256(canonical_json(item)).hexdigest(), Jsonb(item)),
        )

    def make_release(self, ocid, release_id, buyer_id, *, award=False, title="IT services"):
        now = datetime.now(timezone.utc)
        item = {
            "ocid": ocid, "id": release_id,
            "date": (now - timedelta(days=3 if award else 1)).isoformat(),
            "tag": ["award" if award else "tender"],
            "buyer": {"id": buyer_id, "name": "Same published name"},
            "parties": [{"id": buyer_id, "roles": ["buyer"]}],
            "tender": {
                "title": title, "status": "active", "procurementMethod": "open",
                "classification": {"scheme": "CPV", "id": "72200000"},
                "tenderPeriod": {"endDate": (now + timedelta(days=10)).isoformat()},
            },
        }
        if award:
            item["awards"] = [{"id": f"award-{release_id}", "status": "active", "date": (now - timedelta(days=4)).isoformat()}]
        return item

    def test_streaming_materializer_uses_exact_buyer_and_not_global_release_cap(self):
        from awardline.materialize import materialize_database

        suffix = uuid4().hex
        buyer = f"GB-CFS-{suffix}"
        other_buyer = f"GB-CFS-{uuid4().hex}"
        current_ocid = f"ocds-b5fd17-current-{suffix}"
        history = self.make_release(f"ocds-b5fd17-history-{suffix}", f"award-{suffix}", buyer, award=True)
        namesake = self.make_release(f"ocds-b5fd17-namesake-{suffix}", f"namesake-{suffix}", other_buyer, award=True)
        current = self.make_release(current_ocid, f"tender-{suffix}", buyer)
        with self.psycopg.connect(self.dsn) as connection:
            for item in (history, namesake, current):
                self.insert_release(connection, item)
        result = materialize_database(self.dsn, batch_size=2)
        self.assertGreater(result["source_releases"], 2)
        dossier_id = sha256(f"contracts_finder:{current_ocid}:".encode()).hexdigest()[:20]
        with self.psycopg.connect(self.dsn) as connection:
            row = connection.execute(
                "SELECT d.payload, d.eligible FROM dossier_heads h JOIN dossiers d ON d.id=h.id AND d.revision=h.revision WHERE h.id=%s",
                (dossier_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row[1])
        self.assertEqual([a["award_id"] for a in row[0]["related_awards"]["awards"]], [f"award-award-{suffix}"])

    def test_later_award_retires_preview_but_keeps_prior_revision(self):
        from awardline.materialize import materialize_database

        suffix = uuid4().hex
        ocid = f"ocds-b5fd17-retired-{suffix}"
        tender = self.make_release(ocid, f"tender-{suffix}", f"GB-CFS-{suffix}")
        dossier_id = sha256(f"contracts_finder:{ocid}:".encode()).hexdigest()[:20]
        with self.psycopg.connect(self.dsn) as connection:
            self.insert_release(connection, tender)
        materialize_database(self.dsn, batch_size=3)
        with self.psycopg.connect(self.dsn) as connection:
            self.assertIsNotNone(connection.execute("SELECT revision FROM dossier_heads WHERE id=%s", (dossier_id,)).fetchone())
        later = self.make_release(ocid, f"later-award-{suffix}", f"GB-CFS-{suffix}", award=True)
        later["date"] = datetime.now(timezone.utc).isoformat()
        with self.psycopg.connect(self.dsn) as connection:
            self.insert_release(connection, later)
        materialize_database(self.dsn, batch_size=3)
        with self.psycopg.connect(self.dsn) as connection:
            self.assertIsNone(connection.execute("SELECT revision FROM dossier_heads WHERE id=%s", (dossier_id,)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT revision FROM dossiers WHERE id=%s", (dossier_id,)).fetchone())

    def test_old_active_notice_becomes_fresh_after_continuous_source_check(self):
        from awardline.materialize import materialize_database
        from awardline.ingest import parse_package
        from awardline.postgres import PgIngestRepository

        now = datetime.now(timezone.utc)
        suffix = uuid4().hex
        ocid = f"ocds-b5fd17-refreshed-{suffix}"
        tender = self.make_release(ocid, f"tender-{suffix}", f"GB-CFS-{suffix}")
        tender["date"] = (now - timedelta(days=3)).isoformat()
        start = now - timedelta(days=4)
        end = now - timedelta(hours=1)
        with self.psycopg.connect(self.dsn) as connection:
            repo = PgIngestRepository(connection)
            window = repo.open_window(start.isoformat(), end.isoformat())
            repo.persist_page(window["id"], parse_package({"releases": [tender]}, window["next_url"]))
        materialize_database(self.dsn, batch_size=3)
        dossier_id = sha256(f"contracts_finder:{ocid}:".encode()).hexdigest()[:20]
        with self.psycopg.connect(self.dsn) as connection:
            source_as_of = connection.execute(
                "SELECT d.data_as_of FROM dossier_heads h JOIN dossiers d ON d.id=h.id AND d.revision=h.revision WHERE h.id=%s",
                (dossier_id,),
            ).fetchone()[0]
        self.assertGreater(source_as_of, now - timedelta(hours=36))
        self.assertEqual(source_as_of, end)


if __name__ == "__main__":
    unittest.main()
