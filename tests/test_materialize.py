from datetime import datetime, timedelta, timezone
import unittest

from awardline.materialize import candidate_dossiers


NOW = datetime(2026, 9, 28, 9, tzinfo=timezone.utc)


def release(ocid, release_id, tag, published, *, deadline="2026-10-15T12:00:00Z", award=False, buyer_id="GB-CFS-111"):
    item = {
        "ocid": ocid,
        "id": release_id,
        "date": published,
        "tag": [tag],
        "buyer": {"id": buyer_id, "name": "Example Buyer"},
        "parties": [{"id": buyer_id, "roles": ["buyer"]}],
        "tender": {"title": "Cloud services", "status": "active", "procurementMethod": "open", "classification": {"scheme": "CPV", "id": "72200000"}, "tenderPeriod": {"endDate": deadline}},
    }
    if award:
        item["awards"] = [{"id": "award-1", "status": "active", "date": "2026-09-01T00:00:00Z", "suppliers": [{"name": "Supplier A"}]}]
    return item


class CandidateTests(unittest.TestCase):
    def test_source_refresh_requires_continuous_clean_windows_since_notice(self):
        from awardline.materialize import _source_as_of

        published = NOW - timedelta(days=3)
        windows = [
            (NOW - timedelta(days=4), NOW - timedelta(days=2)),
            (NOW - timedelta(days=2), NOW - timedelta(hours=1)),
        ]
        self.assertEqual(_source_as_of(windows, published, NOW), NOW - timedelta(hours=1))
        self.assertEqual(_source_as_of(windows[:1], published, NOW), NOW - timedelta(days=2))
        self.assertIsNone(_source_as_of([(NOW - timedelta(days=2), NOW - timedelta(hours=1))], published, NOW))

    def test_latest_active_tender_and_prior_award_create_eligible_dossier(self):
        old = release("old-1", "a1", "award", "2026-09-02T00:00:00Z", award=True)
        tender = release("new-1", "t1", "tender", "2026-09-27T00:00:00Z")
        rows = candidate_dossiers([old, tender], NOW, "2026-09-01T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][1])
        self.assertEqual(rows[0][0]["related_awards"]["awards"][0]["award_id"], "award-1")

    def test_later_award_replaces_tender_head(self):
        tender = release("new-1", "t1", "tender", "2026-09-27T00:00:00Z")
        later = release("new-1", "a1", "award", "2026-09-28T00:00:00Z", award=True)
        self.assertEqual(candidate_dossiers([tender, later], NOW, None), [])

    def test_expired_tender_is_not_materialized(self):
        tender = release("new-1", "t1", "tender", "2026-09-27T00:00:00Z", deadline="2026-09-27T12:00:00Z")
        self.assertEqual(candidate_dossiers([tender], NOW, None), [])

    def test_lot_notice_waits_for_lot_safe_relationships(self):
        tender = release("new-1", "t1", "tender", "2026-09-27T00:00:00Z")
        tender["tender"]["lots"] = [{"id": "lot-1"}, {"id": "lot-2"}]
        self.assertEqual(candidate_dossiers([tender], NOW, None), [])


if __name__ == "__main__":
    unittest.main()
