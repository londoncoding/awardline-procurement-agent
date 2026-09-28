"""Acceptance tests for the single buyer/category evidence action."""

from copy import deepcopy
from hashlib import sha256
import os
from uuid import uuid4

import pytest

from fastapi.testclient import TestClient

from awardline.api import create_app
from awardline.buyer_history import build_buyer_history, preview_buyer_history
from awardline.buyer_history import PgBuyerHistoryRepository, valid_buyer_id
from awardline.ingest import canonical_json


BUYER_ID = "GB-NHS:123"


def release(ocid="ocds-1", release_id="r1", *, buyer_id="123", cpv="72200000", supplier_id="GB-COH-456", award_id="a1", date="2026-09-20T12:00:00Z"):
    return {
        "ocid": ocid, "id": release_id, "date": date,
        "buyer": {"name": "Example Trust", "identifier": {"scheme": "GB-NHS", "id": buyer_id}},
        "tender": {"items": [{"classification": {"scheme": "CPV", "id": cpv}}]},
        "awards": [{"id": award_id, "status": "active", "date": date,
                    "suppliers": [{"id": supplier_id, "name": "Example Supplier"}],
                    "value": {"amount": 120000, "currency": "GBP"},
                    "contractPeriod": {"endDate": "2027-09-20T00:00:00Z"}}],
    }


def test_exact_source_identifiers_and_category_only():
    namesake = release("wrong", buyer_id="999")
    namesake["buyer"]["name"] = "Example Trust"
    other_category = release("other-category", cpv="80000000")
    no_supplier_id = release("no-id")
    no_supplier_id["awards"][0]["suppliers"][0].pop("id")
    result = build_buyer_history(BUYER_ID, "7220", [release(), namesake, other_category, no_supplier_id])
    assert [(a["ocid"], a["award_id"]) for a in result["awards"]] == [("ocds-1", "a1")]
    assert result["awards"][0]["suppliers"][0]["id"] == "GB-COH-456"
    assert result["buyer"]["match"]["probability"] is None
    assert result["buyer"]["match"]["method"] == "published_namespaced_identifier"


def test_latest_award_revision_wins_and_cancelled_revision_retires():
    old = release()
    revised = release(release_id="r2", date="2026-09-22T12:00:00Z")
    revised["awards"][0]["value"]["amount"] = 125000
    result = build_buyer_history(BUYER_ID, "7220", [old, revised])
    assert len(result["awards"]) == 1
    assert result["awards"][0]["value"]["amount"] == 125000
    assert result["awards"][0]["evidence"]["release_id"] == "r2"
    cancelled = deepcopy(revised)
    cancelled["id"] = "r3"
    cancelled["date"] = "2026-09-23T12:00:00Z"
    cancelled["awards"][0]["status"] = "cancelled"
    assert build_buyer_history(BUYER_ID, "7220", [old, revised, cancelled])["awards"] == []


def test_latest_award_revision_moving_out_of_category_retires_old_category_hit():
    old = release()
    revised = release(release_id="r2", cpv="80000000", date="2026-09-22T12:00:00Z")
    assert build_buyer_history(BUYER_ID, "7220", [old, revised])["awards"] == []


def test_empty_and_bounded_results_disclose_coverage():
    empty = build_buyer_history(BUYER_ID, "7220", [])
    assert empty["awards"] == []
    assert empty["coverage"]["status"] == "observed_records_only"
    assert empty["coverage"]["observed_from"] is None
    assert empty["buyer"]["match"]["strength"] == "unknown"
    rows = [release(f"ocds-{n}", award_id=f"a{n}", date=f"2026-09-{n+10:02d}T12:00:00Z") for n in range(3)]
    bounded = build_buyer_history(BUYER_ID, "7220", rows, limit=2)
    assert len(bounded["awards"]) == 2
    assert bounded["coverage"]["truncated"] is True
    assert bounded["coverage"]["matched_awards"] == 3


def test_freeform_names_cannot_be_used_as_buyer_ids():
    assert not valid_buyer_id("Example Trust")
    assert not valid_buyer_id("contracts_finder:Example Trust")
    assert not valid_buyer_id("contracts_finder:foo")


def test_revision_is_stable_and_changes_with_published_correction():
    first = build_buyer_history(BUYER_ID, "7220", [release()])
    assert first["revision"] == build_buyer_history(BUYER_ID, "7220", [release()])["revision"]
    corrected = release()
    corrected["awards"][0]["value"]["amount"] = 1
    assert first["revision"] != build_buyer_history(BUYER_ID, "7220", [corrected])["revision"]


def test_http_action_is_explicitly_demo_gated_and_uses_same_payload():
    class FixtureRepository:
        def releases_for_buyer(self, buyer_id):
            assert buyer_id == BUYER_ID
            return [release()]

    params = {"buyer_id": BUYER_ID, "category": "7220"}
    path = "/v1/buyer-history"
    disabled = TestClient(create_app("unused", history_repository=FixtureRepository()))
    assert disabled.get(path, params=params).status_code == 503
    demo = TestClient(create_app("unused", history_repository=FixtureRepository(), research_demo=True))
    response = demo.get(path, params=params)
    assert response.status_code == 200
    assert response.json() == build_buyer_history(BUYER_ID, "7220", [release()])
    assert response.headers["Cache-Control"] == "no-store"
    assert demo.get(path, params={"buyer_id": BUYER_ID, "category": "bad"}).status_code == 422


def test_source_scoped_buyer_id_with_slash_survives_http_query():
    source_id = "contracts_finder:GB-SRS-supplierregistration.cabinetoffice.gov.uk/4fsZVQg5"

    class FixtureRepository:
        def releases_for_buyer(self, buyer_id):
            assert buyer_id == source_id
            return []

    client = TestClient(create_app("unused", history_repository=FixtureRepository(), research_demo=True))
    response = client.get("/v1/buyer-history", params={"buyer_id": source_id, "category": "7942"})
    assert response.status_code == 200
    assert response.json()["query"]["buyer_id"] == source_id


def test_free_preview_shows_coverage_without_paid_award_details():
    class FixtureRepository:
        def releases_for_buyer(self, buyer_id):
            return [release()]

    client = TestClient(create_app("unused", history_repository=FixtureRepository()))
    response = client.get("/v1/buyer-history/preview", params={"buyer_id": BUYER_ID, "category": "7220"})
    assert response.status_code == 200
    body = response.json()
    assert body["buyer_id"] == BUYER_ID
    assert body["award_count"] == 1
    assert body["enrichment_available"] is True
    assert body["payment_enabled"] is False
    assert body["price_usdc"] is None
    assert body["coverage_status"] == "observed_records_only"
    assert "suppliers" not in str(body)
    assert "120000" not in str(body)
    assert "source_url" not in str(body)
    assert client.get("/v1/buyer-history", params={"buyer_id": BUYER_ID, "category": "7220"}).status_code == 503


def test_empty_preview_never_offers_a_charge():
    full = build_buyer_history(BUYER_ID, "7220", [])
    preview = preview_buyer_history(full)
    assert preview["award_count"] == 0
    assert preview["enrichment_available"] is False
    assert preview["price_usdc"] is None


def test_postgres_repository_filters_by_exact_published_buyer_id():
    dsn = os.environ.get("AWARDLINE_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("AWARDLINE_TEST_DATABASE_URL is not set")
    import psycopg
    from psycopg.types.json import Jsonb

    buyer = uuid4().hex
    wanted = release("ocds-" + uuid4().hex, buyer_id=buyer)
    namesake = release("ocds-" + uuid4().hex, buyer_id=uuid4().hex)
    namesake["buyer"]["name"] = wanted["buyer"]["name"]
    with psycopg.connect(dsn) as connection:
        if not connection.info.dbname.startswith("awardline_test"):
            raise RuntimeError("Buyer-history integration test requires awardline_test*")
        for item in (wanted, namesake):
            connection.execute(
                "INSERT INTO source_releases (id, source, ocid, release_id, content_sha256, release) VALUES (%s, %s, %s, %s, %s, %s)",
                (uuid4(), "contracts_finder", item["ocid"], item["id"], sha256(canonical_json(item)).hexdigest(), Jsonb(item)),
            )
    try:
        rows = PgBuyerHistoryRepository(dsn).releases_for_buyer("GB-NHS:" + buyer)
        assert [item["ocid"] for item in rows] == [wanted["ocid"]]
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute("DELETE FROM source_releases WHERE ocid IN (%s, %s)", (wanted["ocid"], namesake["ocid"]))
