"""Acceptance tests for bounded historical ingestion and coverage claims."""

from datetime import datetime, timedelta, timezone
import unittest

from awardline.cli import Throttled
from awardline.ingest import initial_url, parse_package


START = datetime(2026, 9, 1, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self):
        self.windows = {}
        self.persisted = []
        self.throttled = []

    def open_window(self, start, end):
        key = (start, end)
        if key not in self.windows:
            self.windows[key] = {
                "id": key, "next_url": initial_url(start, end), "state": "running",
                "pages_seen": 0, "issues_seen": 0, "cooldown_until": None,
            }
        return self.windows[key].copy()

    def persist_page(self, window_id, page):
        window = self.windows[window_id]
        if window["next_url"] != page.url:
            raise AssertionError("wrong cursor")
        window["next_url"] = page.next_url or ""
        window["pages_seen"] += 1
        window["issues_seen"] += len(page.issues)
        window["state"] = "running" if page.next_url else ("complete_with_issues" if page.issues else "complete")
        self.persisted.append((window_id, page.url))

    def mark_throttled(self, window_id, seconds):
        self.throttled.append((window_id, seconds))

    def verified_coverage(self, start, end):
        from awardline.coverage import audit_coverage

        rows = [
            (datetime.fromisoformat(left.replace("Z", "+00:00")), datetime.fromisoformat(right.replace("Z", "+00:00")), row["state"], row["issues_seen"])
            for (left, right), row in self.windows.items()
        ]
        return audit_coverage(rows, start, end)["complete"]


class BackfillTests(unittest.TestCase):
    def test_plans_exact_adjacent_utc_windows(self):
        from awardline.backfill import plan_windows

        windows = list(plan_windows(START, START + timedelta(hours=50), timedelta(days=1)))
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0], (START, START + timedelta(days=1)))
        self.assertEqual(windows[-1], (START + timedelta(days=2), START + timedelta(hours=50)))

    def test_rejects_naive_and_reverse_ranges(self):
        from awardline.backfill import plan_windows

        with self.assertRaises(ValueError):
            list(plan_windows(datetime(2026, 9, 1), START + timedelta(days=1)))
        with self.assertRaises(ValueError):
            list(plan_windows(START, START))

    def test_page_budget_stops_and_later_run_resumes_cursor(self):
        from awardline.backfill import run_backfill

        repo = FakeRepository()
        start = START.isoformat().replace("+00:00", "Z")
        end = (START + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        first_url = initial_url(start, end)
        second_url = first_url + "&cursor=next"
        seen = []

        def fetch(url):
            seen.append(url)
            if url == first_url:
                return {"releases": [], "links": {"next": second_url}}
            return {"releases": []}

        first = run_backfill(repo, fetch, START, START + timedelta(days=1), max_pages=1)
        self.assertEqual(first["state"], "page_limit")
        self.assertEqual(seen, [first_url])
        second = run_backfill(repo, fetch, START, START + timedelta(days=1), max_pages=1)
        self.assertEqual(second["state"], "complete")
        self.assertEqual(seen, [first_url, second_url])
        self.assertEqual(len(repo.persisted), 2)

    def test_does_not_skip_issue_window_to_claim_continuity(self):
        from awardline.backfill import run_backfill

        repo = FakeRepository()
        start = START.isoformat().replace("+00:00", "Z")
        end = (START + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        window = repo.open_window(start, end)
        page = parse_package({"releases": [{"id": "missing-ocid"}]}, window["next_url"])
        repo.persist_page(window["id"], page)
        result = run_backfill(repo, lambda url: self.fail("should not fetch"), START, START + timedelta(days=2))
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(len(repo.windows), 1)

    def test_clean_split_windows_can_supersede_full_page_uncertainty(self):
        from awardline.backfill import run_backfill

        repo = FakeRepository()
        day_start = START.isoformat().replace("+00:00", "Z")
        midpoint = (START + timedelta(hours=12)).isoformat().replace("+00:00", "Z")
        day_end = (START + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        original = repo.open_window(day_start, day_end)
        repo.persist_page(original["id"], parse_package({"releases": [{}] * 100}, original["next_url"]))
        for left, right in ((day_start, midpoint), (midpoint, day_end)):
            split = repo.open_window(left, right)
            repo.persist_page(split["id"], parse_package({"releases": []}, split["next_url"]))
        result = run_backfill(repo, lambda _: {"releases": []}, START, START + timedelta(days=2), max_windows=1)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["windows_completed"], 2)

    def test_throttle_stops_and_persists_cooldown(self):
        from awardline.backfill import run_backfill

        repo = FakeRepository()

        def throttled(_):
            raise Throttled("source says wait", 300)

        result = run_backfill(repo, throttled, START, START + timedelta(days=1))
        self.assertEqual(result["state"], "cooldown")
        self.assertEqual(len(repo.throttled), 1)
        self.assertEqual(len(repo.persisted), 0)


class CoverageTests(unittest.TestCase):
    def test_contiguous_complete_windows_support_coverage(self):
        from awardline.coverage import audit_coverage

        rows = [
            (START, START + timedelta(days=1), "complete", 0),
            (START + timedelta(days=1), START + timedelta(days=2), "complete", 0),
        ]
        result = audit_coverage(rows, START, START + timedelta(days=2))
        self.assertTrue(result["complete"])
        self.assertEqual(result["gaps"], [])

    def test_missing_or_issue_window_cannot_support_coverage(self):
        from awardline.coverage import audit_coverage

        rows = [
            (START, START + timedelta(days=1), "complete", 0),
            (START + timedelta(days=1), START + timedelta(days=2), "complete_with_issues", 1),
        ]
        result = audit_coverage(rows, START, START + timedelta(days=3))
        self.assertFalse(result["complete"])
        self.assertEqual(result["gaps"], [[(START + timedelta(days=1)).isoformat(), (START + timedelta(days=3)).isoformat()]])
        self.assertEqual(result["rejected_windows"], 1)

    def test_overlapping_completed_windows_merge_without_false_gap(self):
        from awardline.coverage import audit_coverage

        rows = [
            (START, START + timedelta(hours=30), "complete", 0),
            (START + timedelta(days=1), START + timedelta(days=2), "complete", 0),
        ]
        self.assertTrue(audit_coverage(rows, START, START + timedelta(days=2))["complete"])


if __name__ == "__main__":
    unittest.main()
