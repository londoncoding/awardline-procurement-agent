import unittest

from awardline.enrichment import build_dossier


def notice(ocid, release_id, buyer_id="123", cpv="72200000", **tender_overrides):
    tender = {
        "id": release_id,
        "title": "Managed cloud services",
        "status": "active",
        "procurementMethod": "open",
        "tenderPeriod": {"endDate": "2026-11-01T12:00:00Z"},
        "items": [{"classification": {"scheme": "CPV", "id": cpv}}],
    }
    tender.update(tender_overrides)
    return {
        "ocid": ocid,
        "id": release_id,
        "date": "2026-09-27T10:00:00Z",
        "buyer": {"name": "Example NHS Trust", "identifier": {"scheme": "GB-NHS", "id": buyer_id}},
        "tender": tender,
    }


def old_award(ocid="old-1", buyer_id="123", cpv="72200000"):
    item = notice(ocid, "old-r1", buyer_id, cpv)
    item["awards"] = [{"id": "award-1", "status": "active", "title": "Old managed services", "date": "2025-01-01T00:00:00Z", "suppliers": [{"name": "Supplier A"}]}]
    return item


class DossierTests(unittest.TestCase):
    def test_exact_eight_fields_and_accepted_comparable(self):
        dossier, eligible = build_dossier(notice("new-1", "r1"), [old_award()], None, "2026-09-27T11:00:00Z")
        self.assertEqual(set(dossier), {"dossier", "buyer", "opportunity", "procurement_route", "related_awards", "material_changes", "published_contact", "provenance"})
        self.assertTrue(eligible)
        self.assertEqual(dossier["related_awards"]["awards"][0]["relationship"], "same_buyer_same_category")
        self.assertIsNone(dossier["buyer"]["match"]["probability"])

    def test_namesake_buyer_never_accepted(self):
        current = notice("new-1", "r1", buyer_id="123")
        history = [old_award(buyer_id="999")]
        dossier, eligible = build_dossier(current, history, None, "2026-09-27T11:00:00Z")
        self.assertEqual(dossier["related_awards"]["awards"], [])
        self.assertFalse(eligible)

    def test_different_category_never_comparable(self):
        dossier, eligible = build_dossier(notice("new-1", "r1"), [old_award(cpv="80000000")], None, "2026-09-27T11:00:00Z")
        self.assertEqual(dossier["related_awards"]["awards"], [])
        self.assertFalse(eligible)

    def test_missing_identifier_does_not_guess(self):
        current = notice("new-1", "r1")
        current["buyer"].pop("identifier")
        dossier, eligible = build_dossier(current, [old_award()], None, "2026-09-27T11:00:00Z")
        self.assertEqual(dossier["related_awards"]["awards"], [])
        self.assertEqual(dossier["buyer"]["match"]["strength"], "unknown")
        self.assertFalse(eligible)

    def test_no_baseline_is_unknown_not_no_change(self):
        dossier, _ = build_dossier(notice("new-1", "r1"), [], None, "2026-09-27T11:00:00Z")
        self.assertEqual(dossier["material_changes"]["comparison_status"], "no_baseline")

    def test_observed_change_enables_entitlement(self):
        before = notice("new-1", "r0")
        after = notice("new-1", "r1", tenderPeriod={"endDate": "2026-11-05T12:00:00Z"})
        dossier, eligible = build_dossier(after, [], before, "2026-09-27T11:00:00Z")
        self.assertTrue(eligible)
        self.assertEqual(dossier["material_changes"]["changes"][0]["field"], "deadline")

    def test_text_only_edit_is_disclosed_but_not_billable(self):
        before = notice("new-1", "r0", title="Cloud services", description="Original wording")
        after = notice("new-1", "r1", title="Cloud service", description="Edited wording")
        dossier, eligible = build_dossier(after, [], before, "2026-09-27T11:00:00Z")
        self.assertEqual({change["field"] for change in dossier["material_changes"]["changes"]}, {"title", "description"})
        self.assertFalse(eligible)

    def test_award_history_without_stated_open_competition_is_not_billable(self):
        current = notice("new-1", "r1")
        current["tender"].pop("procurementMethod")
        dossier, eligible = build_dossier(current, [old_award()], None, "2026-09-27T11:00:00Z")
        self.assertEqual(len(dossier["related_awards"]["awards"]), 1)
        self.assertEqual(dossier["procurement_route"]["participation"], "unknown")
        self.assertFalse(eligible)

    def test_dynamic_market_history_is_not_billable(self):
        current = notice("new-1", "r1", procurementMethodDetails="Call-off from a dynamic purchasing system")
        dossier, eligible = build_dossier(current, [old_award()], None, "2026-09-27T11:00:00Z")
        self.assertEqual(len(dossier["related_awards"]["awards"]), 1)
        self.assertEqual(dossier["procurement_route"]["kind"], "dynamic_market_or_dps")
        self.assertFalse(eligible)

    def test_deterministic_revision_for_same_evidence(self):
        args = (notice("new-1", "r1"), [old_award()], None, "2026-09-27T11:00:00Z")
        first, _ = build_dossier(*args)
        second, _ = build_dossier(*args)
        self.assertEqual(first["dossier"]["revision"], second["dossier"]["revision"])

    def test_historical_award_correction_changes_revision(self):
        current = notice("new-1", "r1")
        previous = old_award()
        first, _ = build_dossier(current, [previous], None, "2026-09-27T11:00:00Z")
        previous["awards"][0]["title"] = "Corrected award title"
        second, _ = build_dossier(current, [previous], None, "2026-09-27T11:00:00Z")
        self.assertNotEqual(first["dossier"]["revision"], second["dossier"]["revision"])

    def test_rejects_baseline_from_another_process(self):
        with self.assertRaises(ValueError):
            build_dossier(notice("new-1", "r1"), [], notice("other", "r0"), "2026-09-27T11:00:00Z")

    def test_rejects_pending_award(self):
        old = old_award()
        old["awards"][0]["status"] = "pending"
        dossier, eligible = build_dossier(notice("new-1", "r1"), [old], None, "2026-09-27T11:00:00Z")
        self.assertEqual(dossier["related_awards"]["awards"], [])
        self.assertFalse(eligible)

    def test_explicit_prior_process_uses_published_link(self):
        current = notice("new-1", "r1", cpv="80000000")
        current["relatedProcesses"] = [{"identifier": "old-1", "relationship": ["prior"]}]
        dossier, eligible = build_dossier(current, [old_award()], None, "2026-09-27T11:00:00Z")
        self.assertTrue(eligible)
        self.assertEqual(dossier["related_awards"]["awards"][0]["relationship"], "explicit_prior_process")
        refs = dossier["related_awards"]["awards"][0]["match"]["evidence_refs"]
        paths = {item["field_path"] for item in dossier["provenance"]["evidence_refs"] if item["id"] in refs}
        self.assertIn("/relatedProcesses", paths)

    def test_published_call_off_takes_priority_over_framework_flag(self):
        current = notice("new-1", "r1", procurementMethodDetails="Framework call-off", techniques={"frameworkAgreement": {"id": "f-1"}})
        dossier, _ = build_dossier(current, [], None, "2026-09-27T11:00:00Z")
        self.assertEqual(dossier["procurement_route"]["kind"], "framework_call_off")

    def test_contracts_finder_party_id_and_cpv_shape(self):
        current = notice("new-1", "r1")
        current["buyer"] = {"id": "GB-CFS-338658", "name": "Example NHS Trust"}
        current["parties"] = [{"id": "GB-CFS-338658", "roles": ["buyer"], "contactPoint": {"email": "published@example.org"}}]
        current["tender"].pop("items")
        current["tender"]["classification"] = {"scheme": "CPV", "id": "72200000"}
        previous = old_award()
        previous["buyer"] = {"id": "GB-CFS-338658", "name": "Example NHS Trust"}
        previous["parties"] = [{"id": "GB-CFS-338658", "roles": ["buyer"]}]
        previous["tender"].pop("items")
        previous["tender"]["classification"] = {"scheme": "CPV", "id": "72210000"}
        dossier, eligible = build_dossier(current, [previous], None, "2026-09-27T11:00:00Z")
        self.assertFalse(eligible)  # 7220 and 7221 are distinct four-digit categories.
        previous["tender"]["classification"]["id"] = "72209999"
        dossier, eligible = build_dossier(current, [previous], None, "2026-09-27T11:00:00Z")
        self.assertTrue(eligible)
        self.assertEqual(dossier["buyer"]["identifier"], "contracts_finder:GB-CFS-338658")
        self.assertEqual(dossier["published_contact"]["email"], "published@example.org")
        refs = dossier["related_awards"]["awards"][0]["match"]["evidence_refs"]
        paths = {item["field_path"] for item in dossier["provenance"]["evidence_refs"] if item["id"] in refs}
        self.assertIn("/tender/classification", paths)

    def test_contracts_finder_namesake_with_distinct_party_ids_is_rejected(self):
        current = notice("new-1", "r1")
        current["buyer"] = {"id": "GB-CFS-111", "name": "Example NHS Trust"}
        current["parties"] = [{"id": "GB-CFS-111", "roles": ["buyer"]}]
        previous = old_award()
        previous["buyer"] = {"id": "GB-CFS-222", "name": "Example NHS Trust"}
        previous["parties"] = [{"id": "GB-CFS-222", "roles": ["buyer"]}]
        dossier, eligible = build_dossier(current, [previous], None, "2026-09-27T11:00:00Z")
        self.assertFalse(eligible)
        self.assertEqual(dossier["related_awards"]["awards"], [])

    def test_dynamic_system_call_off_is_not_framework_call_off(self):
        current = notice("new-1", "r1", procurementMethodDetails="Call-off from a dynamic purchasing system")
        dossier, _ = build_dossier(current, [], None, "2026-09-27T11:00:00Z")
        self.assertEqual(dossier["procurement_route"]["kind"], "dynamic_market_or_dps")

    def test_award_after_notice_is_not_history(self):
        current = notice("new-1", "r1")
        previous = old_award()
        previous["awards"][0]["date"] = "2026-10-01T00:00:00Z"
        dossier, eligible = build_dossier(current, [previous], None, "2026-09-27T11:00:00Z")
        self.assertFalse(eligible)
        self.assertEqual(dossier["related_awards"]["awards"], [])

    def test_award_outside_declared_lookback_is_excluded(self):
        dossier, eligible = build_dossier(notice("new-1", "r1"), [old_award()], None, "2026-09-27T11:00:00Z", lookback_start="2025-09-01T00:00:00Z")
        self.assertFalse(eligible)
        self.assertEqual(dossier["related_awards"]["awards"], [])

    def test_repeated_award_release_counts_as_one_latest_award(self):
        first = old_award()
        first["date"] = "2026-09-25T10:00:00Z"
        corrected = old_award()
        corrected["id"] = "old-r2"
        corrected["date"] = "2026-09-26T10:00:00Z"
        corrected["awards"][0]["title"] = "Corrected published title"
        dossier, _ = build_dossier(notice("new-1", "r1"), [first, corrected], None, "2026-09-27T11:00:00Z")
        awards = dossier["related_awards"]["awards"]
        self.assertEqual(len(awards), 1)
        self.assertEqual(awards[0]["title"], "Corrected published title")

    def test_every_evidence_reference_is_in_bounded_provenance(self):
        current = notice("new-1", "r1", title="New", description="New description", status="active", value={"amount": 20, "currency": "GBP"}, tenderPeriod={"endDate": "2026-11-05T12:00:00Z"})
        baseline = notice("new-1", "r0", title="Old", description="Old description", status="planned", value={"amount": 10, "currency": "GBP"})
        history = [old_award(ocid=f"old-{i}") for i in range(3)]
        dossier, _ = build_dossier(current, history, baseline, "2026-09-27T11:00:00Z")
        known = {item["id"] for item in dossier["provenance"]["evidence_refs"]}
        refs = set(dossier["buyer"]["match"]["evidence_refs"] + dossier["procurement_route"]["evidence_refs"] + dossier["published_contact"]["evidence_refs"])
        for award in dossier["related_awards"]["awards"]:
            refs.update(award["match"]["evidence_refs"])
        for change in dossier["material_changes"]["changes"]:
            refs.update(change["evidence_refs"])
        self.assertLessEqual(len(known), 12)
        self.assertLessEqual(refs, known)


if __name__ == "__main__":
    unittest.main()
