"""Run with AWARDLINE_TEST_DATABASE_URL pointing to an isolated test DB."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from awardline.ingest import SourceRelease, parse_package
from awardline.postgres import PgIngestRepository, WindowOutOfOrder


def sample(release_id="r1", title="First"):
    return {"ocid": "ocds-b5fd17-example", "id": release_id, "date": "2026-09-27T10:00:00Z", "tender": {"title": title}}


class PostgresIngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ.get("AWARDLINE_TEST_DATABASE_URL")
        if not dsn:
            raise unittest.SkipTest("AWARDLINE_TEST_DATABASE_URL is not set")
        try:
            import psycopg
        except ImportError as exc:
            raise unittest.SkipTest("psycopg is not installed") from exc
        cls.connection = psycopg.connect(dsn)
        if not cls.connection.info.dbname.startswith("awardline_test"):
            cls.connection.close()
            raise RuntimeError("Integration tests require a database named awardline_test*")
        cls.repository = PgIngestRepository(cls.connection)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "connection"):
            cls.connection.close()

    def setUp(self):
        # Each test uses a distinct valid UTC window; no shared table truncation.
        self.connection.rollback()  # End any read transaction left by the previous test.
        seed = int(uuid4().hex[:8], 16)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seed)
        self.start = start.isoformat().replace("+00:00", "Z")
        self.end = (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        self.window = self.repository.open_window(self.start, self.end)

    def page(self, releases, next_url=None):
        return parse_package({"releases": releases, "links": {"next": next_url} if next_url else {}}, self.window["next_url"])

    def counts(self):
        with self.connection.cursor() as cur:
            cur.execute("SELECT pages_seen, releases_seen, issues_seen, state, next_url FROM ingest_windows WHERE id = %s", (self.window["id"],))
            return cur.fetchone()

    def test_commits_page_release_and_terminal_cursor(self):
        self.repository.persist_page(self.window["id"], self.page([sample()]))
        pages, releases, issues, state, next_url = self.counts()
        self.assertEqual((pages, releases, issues, state, next_url), (1, 1, 0, "complete", ""))
        with self.connection.cursor() as cur:
            cur.execute("SELECT response_bytes, package FROM raw_pages WHERE window_id = %s", (self.window["id"],))
            raw_bytes, package = cur.fetchone()
        self.assertIn(b'"releases"', raw_bytes)
        self.assertEqual(package["releases"][0]["id"], "r1")

    def test_resume_after_committed_cursor(self):
        next_url = f"https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?publishedFrom={self.start}&publishedTo={self.end}&cursor=ABC"
        self.repository.persist_page(self.window["id"], self.page([sample()], next_url))
        reopened = self.repository.open_window(self.start, self.end)
        self.assertEqual(reopened["next_url"], next_url)
        self.assertEqual(reopened["pages_seen"], 1)
        self.repository.persist_page(self.window["id"], parse_package({"releases": [], "links": {}}, next_url))
        self.assertEqual(self.counts()[3], "complete")

    def test_duplicate_release_does_not_duplicate_storage(self):
        item = sample(release_id=f"r-{uuid4().hex}")
        self.repository.persist_page(self.window["id"], self.page([item, item]))
        with self.connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM source_releases WHERE source = %s AND ocid = %s AND release_id = %s", ("contracts_finder", item["ocid"], item["id"]))
            count = cur.fetchone()[0]
        self.assertEqual(count, 1)

    def test_changed_same_release_id_is_retained_and_flagged(self):
        release_id = f"r-{uuid4().hex}"
        next_url = f"https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?publishedFrom={self.start}&publishedTo={self.end}&cursor=DEF"
        self.repository.persist_page(self.window["id"], self.page([sample(release_id, "First")], next_url))
        self.repository.persist_page(self.window["id"], parse_package({"releases": [sample(release_id, "Corrected")]}, next_url))
        with self.connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM source_releases WHERE release_id = %s", (release_id,))
            count = cur.fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual(self.counts()[2], 1)
        self.assertEqual(self.counts()[3], "complete_with_issues")

    def test_failure_after_page_insert_rolls_back_entire_page(self):
        page = self.page([sample(release_id=f"r-{uuid4().hex}")])
        damaged = replace(page.releases[0], raw={"not_json": object()})
        page = replace(page, releases=(damaged,))
        with self.assertRaises(TypeError):
            self.repository.persist_page(self.window["id"], page)
        self.assertEqual(self.counts()[:3], (0, 0, 0))
        with self.connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM raw_pages WHERE window_id = %s", (self.window["id"],))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_rejects_page_not_at_durable_cursor(self):
        page = replace(self.page([sample()]), url="https://wrong.example/")
        with self.assertRaises(WindowOutOfOrder):
            self.repository.persist_page(self.window["id"], page)
        self.assertEqual(self.counts()[0], 0)

    def test_coverage_audit_requires_terminal_issue_free_window(self):
        from awardline.coverage import audit_database

        start = datetime.fromisoformat(self.start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(self.end.replace("Z", "+00:00"))
        self.assertFalse(audit_database(self.connection.info.dsn, start, end)["complete"])
        self.repository.persist_page(self.window["id"], self.page([sample(release_id=f"r-{uuid4().hex}")]))
        result = audit_database(self.connection.info.dsn, start, end)
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["gaps"], [])

    def test_clean_split_windows_supersede_uncertain_full_page(self):
        start = datetime.fromisoformat(self.start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(self.end.replace("Z", "+00:00"))
        records = [sample(release_id=f"r-{uuid4().hex}") for _ in range(100)]
        original = parse_package({"releases": records}, self.window["next_url"])
        self.repository.persist_page(self.window["id"], original)
        self.assertFalse(self.repository.verified_coverage(start, end))
        midpoint = start + timedelta(minutes=30)
        for (left, right), half in zip(((start, midpoint), (midpoint, end)), (records[:50], records[50:])):
            split = self.repository.open_window(left.isoformat(), right.isoformat())
            self.repository.persist_page(split["id"], parse_package({"releases": half}, split["next_url"]))
        self.assertTrue(self.repository.verified_coverage(start, end))


if __name__ == "__main__":
    unittest.main()
