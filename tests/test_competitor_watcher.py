"""The published example must make a real call through the buyer-history HTTP route."""

import asyncio
from datetime import datetime, timezone

import httpx

from awardline.api import create_app
from awardline.competitor_watcher import watch_once, run_demo, fetch_contracts_finder_awards


def release(ocid, release_id, *, supplier_id="GB-COH-456", supplier_name="Watched Ltd", buyer_id="123", cpv="72200000"):
    return {
        "ocid": ocid, "id": release_id, "date": "2026-09-20T12:00:00Z",
        "buyer": {"name": "Example Trust", "identifier": {"scheme": "GB-NHS", "id": buyer_id}},
        "tender": {"items": [{"classification": {"scheme": "CPV", "id": cpv}}]},
        "awards": [{"id": "a1", "status": "active", "date": "2026-09-20T12:00:00Z",
                    "suppliers": [{"id": supplier_id, "name": supplier_name}]}],
    }


def test_exact_supplier_trigger_calls_history_once_and_explains_why():
    wanted = release("ocds-wanted", "r1")
    namesake = release("ocds-namesake", "r1", supplier_id="OTHER")
    namesake["awards"][0]["suppliers"][0]["name"] = "Watched Ltd"
    different_category = release("ocds-other-category", "r1", cpv="80000000")

    class FixtureRepository:
        def releases_for_buyer(self, buyer_id):
            return [wanted]

    async def exercise():
        app = create_app("unused", history_repository=FixtureRepository(), research_demo=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://awardline.local") as client:
            return await watch_once(client, [wanted, namesake, different_category, wanted], supplier_id="GB-COH-456", category="7220", seen=set())

    results, seen = asyncio.run(exercise())
    assert len(results) == 1
    assert results[0]["trigger"]["supplier_id"] == "GB-COH-456"
    assert results[0]["trigger"]["ocid"] == "ocds-wanted"
    assert "exact" in results[0]["reason"].lower()
    assert results[0]["buyer_history"]["awards"][0]["evidence"]["source_url"].startswith("https://")
    assert len(seen) == 1


def test_seen_award_does_not_trigger_second_call():
    wanted = release("ocds-wanted", "r1")

    class FailingRepository:
        def releases_for_buyer(self, buyer_id):
            raise AssertionError("Already-seen event must not call buyer history")

    async def exercise():
        app = create_app("unused", history_repository=FailingRepository(), research_demo=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://awardline.local") as client:
            return await watch_once(client, [wanted], supplier_id="GB-COH-456", category="7220", seen={"ocds-wanted:a1"})

    results, seen = asyncio.run(exercise())
    assert results == []
    assert seen == {"ocds-wanted:a1"}


def test_later_cancelled_revision_does_not_trigger_an_old_active_award():
    old = release("ocds-wanted", "r1")
    cancelled = release("ocds-wanted", "r2")
    cancelled["date"] = "2026-09-21T12:00:00Z"
    cancelled["awards"][0]["status"] = "cancelled"

    class FailingRepository:
        def releases_for_buyer(self, buyer_id):
            raise AssertionError("Cancelled award must not request history")

    async def exercise():
        app = create_app("unused", history_repository=FailingRepository(), research_demo=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://awardline.local") as client:
            return await watch_once(client, [old, cancelled], supplier_id="GB-COH-456", category="7220", seen=set())

    assert asyncio.run(exercise()) == ([], set())


def test_one_command_demo_completes_without_network_or_database():
    results = asyncio.run(run_demo())
    assert len(results) == 1
    assert results[0]["buyer_history"]["query"] == {"buyer_id": "GB-NHS:123", "category": "7220"}


def test_live_feed_parser_reads_fixed_publication_window():
    wanted = release("ocds-feed", "r1")

    def responder(request):
        assert request.url.host == "www.contractsfinder.service.gov.uk"
        assert request.url.params["publishedFrom"] == "2026-09-20T00:00:00Z"
        assert request.url.params["publishedTo"] == "2026-09-21T00:00:00Z"
        return httpx.Response(200, json={"releases": [wanted], "links": {}})

    releases = asyncio.run(fetch_contracts_finder_awards(
        datetime(2026, 9, 20, tzinfo=timezone.utc),
        datetime(2026, 9, 21, tzinfo=timezone.utc),
        transport=httpx.MockTransport(responder),
    ))
    assert releases == [wanted]
