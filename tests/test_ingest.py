import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from awardline.ingest import FetchedPackage, PackageError, CursorError, parse_package, walk_pages
from awardline.cli import Throttled, fetch_json


def release(ocid="ocds-b5fd17-one", release_id="r1", **extra):
    return {"ocid": ocid, "id": release_id, "date": "2026-09-27T10:00:00Z", **extra}


class PackageTests(unittest.TestCase):
    def test_preserves_optional_and_unknown_fields(self):
        item = release(extra_extension={"anything": True})
        parsed = parse_package({"releases": [item]}, "https://example.test/page")
        self.assertEqual(parsed.releases[0].raw, item)
        self.assertEqual(parsed.releases[0].raw["extra_extension"], {"anything": True})

    def test_preserves_exact_response_bytes(self):
        body = b'{ "releases": [] }'
        parsed = parse_package({"releases": []}, "page", raw_bytes=body)
        self.assertEqual(parsed.raw_bytes, body)

    def test_quarantines_bad_release_without_losing_valid_one(self):
        parsed = parse_package({"releases": [{"id": "missing-ocid"}, release()]}, "page")
        self.assertEqual(len(parsed.releases), 1)
        self.assertEqual(len(parsed.issues), 1)

    def test_malformed_package_blocks_advance(self):
        with self.assertRaises(PackageError):
            parse_package({"releases": None}, "page")

    def test_same_release_id_changed_content_has_different_digest(self):
        a = parse_package({"releases": [release(title="A")]}, "page").releases[0]
        b = parse_package({"releases": [release(title="B")]}, "page").releases[0]
        self.assertNotEqual(a.digest, b.digest)

    def test_full_terminal_page_marks_coverage_uncertain(self):
        parsed = parse_package({"releases": [release(release_id=f"r{i}") for i in range(100)]}, "page")
        self.assertIn("full_page_without_next_link", [issue["reason"] for issue in parsed.issues])


class PaginationTests(unittest.TestCase):
    def test_accepts_transport_with_raw_body(self):
        body = b'{ "releases": [] }'
        pages = list(walk_pages(lambda url: FetchedPackage({"releases": []}, body), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"))
        self.assertEqual(pages[0].raw_bytes, body)

    def test_walks_next_link_even_after_short_page(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                return {"releases": [release()], "links": {"next": "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?cursor=ABC&publishedFrom=2026-09-01T00%3A00%3A00Z&publishedTo=2026-09-02T00%3A00%3A00Z"}}
            return {"releases": [], "links": {}}

        pages = list(walk_pages(fetch, "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"))
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(calls), 2)

    def test_rejects_cursor_loop(self):
        link = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?cursor=ABC&publishedFrom=2026-09-01T00%3A00%3A00Z&publishedTo=2026-09-02T00%3A00%3A00Z"

        def fetch(url):
            return {"releases": [release()], "links": {"next": link}}

        with self.assertRaises(CursorError):
            list(walk_pages(fetch, "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"))


    def test_rejects_foreign_next_host(self):
        def fetch(url):
            return {"releases": [release()], "links": {"next": "https://attacker.example/path?cursor=ABC"}}

        with self.assertRaises(CursorError):
            list(walk_pages(fetch, "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"))

    def test_rejects_missing_fixed_window(self):
        def fetch(url):
            return {"releases": [release()], "links": {"next": "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?cursor=ABC"}}

        with self.assertRaises(CursorError):
            list(walk_pages(fetch, "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"))


class TransportTests(unittest.TestCase):
    def test_403_stops_immediately_for_cooldown(self):
        with patch("awardline.cli.urlopen", side_effect=HTTPError("https://www.contractsfinder.service.gov.uk/", 403, "Forbidden", {}, None)) as mocked:
            with self.assertRaises(Throttled) as raised:
                fetch_json("https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search")
        self.assertEqual(mocked.call_count, 1)
        self.assertGreaterEqual(raised.exception.wait_seconds, 300)


if __name__ == "__main__":
    unittest.main()
