"""Reference agent: watch exact supplier IDs and research the buyer on a new award.

`--demo` is a complete in-process HTTP run with synthetic releases. `--live`
reads the free Contracts Finder OCDS search feed, then calls an Awardline HTTP
server. It never initiates a payment.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any

import httpx

from .api import create_app
from .enrichment import arr, buyer_identifier, cpv_prefix, obj, source_ref
from .ingest import canonical_json, initial_url, parse_package, validate_next


async def watch_once(
    client: httpx.AsyncClient,
    releases: list[dict[str, Any]],
    *,
    supplier_id: str,
    category: str,
    seen: set[str],
    max_history_calls: int = 1,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Return explained triggers and a new seen set; caller persists after success."""
    if not supplier_id or len(category) != 4 or not category.isdigit() or max_history_calls < 1:
        raise ValueError("An exact supplier ID, four-digit CPV and positive call cap are required")
    latest: dict[str, dict[str, Any]] = {}
    for release in releases:
        for raw in arr(release.get("awards")):
            award = obj(raw)
            if not release.get("ocid") or not release.get("id") or not award.get("id"):
                continue
            event_id = f"{release['ocid']}:{award['id']}"
            rank = (str(release.get("date") or ""), str(release["id"]), sha256(canonical_json(release)).hexdigest())
            if event_id not in latest or rank > latest[event_id]["rank"]:
                latest[event_id] = {"release": release, "award": award, "rank": rank}
    pending = {
        event_id: event for event_id, event in latest.items()
        if event_id not in seen and event["award"].get("status") == "active"
        and cpv_prefix(event["release"]) == category and buyer_identifier(event["release"])
        and any(obj(s).get("id") == supplier_id for s in arr(event["award"].get("suppliers")))
    }
    results: list[dict[str, Any]] = []
    new_seen = set(seen)
    queried_buyers: dict[str, dict[str, Any]] = {}
    for event_id in sorted(pending):
        release = pending[event_id]["release"]
        award = pending[event_id]["award"]
        buyer_id = buyer_identifier(release)
        if buyer_id not in queried_buyers:
            if len(queried_buyers) >= max_history_calls:
                break
            response = await client.get("/v1/buyer-history", params={"buyer_id": buyer_id, "category": category, "limit": 20})
            response.raise_for_status()
            queried_buyers[buyer_id] = response.json()
        results.append({
            "reason": "Exact published supplier ID appears in a newly observed active award in the watched CPV category",
            "trigger": {
                "event_id": event_id, "ocid": release["ocid"], "award_id": award["id"],
                "supplier_id": supplier_id, "buyer_id": buyer_id, "category": category,
                "evidence": source_ref(release, "/awards"),
            },
            "buyer_history": queried_buyers[buyer_id],
        })
        new_seen.add(event_id)
    return results, new_seen


async def fetch_contracts_finder_awards(start: datetime, end: datetime, *, max_pages: int = 10, transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    """Read a fixed publication window; fail if it exceeds the safety cap."""
    published_from = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    published_to = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    url = initial_url(published_from, published_to)
    releases: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
        for page_number in range(max_pages):
            response = await client.get(url)
            response.raise_for_status()
            page = parse_package(response.json(), url, response.content)
            if page.issues:
                raise RuntimeError(f"Contracts Finder page has {len(page.issues)} parsing/coverage issue(s)")
            releases.extend(item.raw for item in page.releases)
            if not page.next_url:
                return releases
            validate_next(page.next_url, published_from, published_to)
            url = page.next_url
    raise RuntimeError(f"Contracts Finder publication window exceeds {max_pages} pages; split the window")


def _demo_release(ocid: str, supplier_id: str, supplier_name: str) -> dict[str, Any]:
    return {
        "ocid": ocid, "id": f"{ocid}-r1", "date": "2026-09-20T12:00:00Z",
        "uri": f"https://example.invalid/awardline/{ocid}",
        "buyer": {"name": "Example NHS Trust", "identifier": {"scheme": "GB-NHS", "id": "123"}},
        "tender": {"items": [{"classification": {"scheme": "CPV", "id": "72200000"}}]},
        "awards": [{"id": "award-1", "status": "active", "date": "2026-09-20T12:00:00Z",
                    "suppliers": [{"id": supplier_id, "name": supplier_name}],
                    "value": {"amount": 120000, "currency": "GBP"},
                    "contractPeriod": {"endDate": "2027-09-20T00:00:00Z"}}],
    }


async def run_demo() -> list[dict[str, Any]]:
    matching = _demo_release("ocds-demo-match", "GB-COH-456", "Watched Supplier")
    irrelevant = _demo_release("ocds-demo-other", "GB-COH-999", "Other Supplier")

    class DemoRepository:
        def releases_for_buyer(self, buyer_id: str) -> list[dict[str, Any]]:
            return [matching, irrelevant] if buyer_id == "GB-NHS:123" else []

    app = create_app("unused", history_repository=DemoRepository(), research_demo=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://awardline.local") as client:
        results, _ = await watch_once(client, [matching, irrelevant], supplier_id="GB-COH-456", category="7220", seen=set())
    return results


def _read_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("State file must be a JSON list of event IDs")
    return set(value)


def _write_seen(path: Path, seen: set[str]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(sorted(seen), indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


async def _run_live(args) -> list[dict[str, Any]]:
    if not args.supplier_id or not args.category or not args.api_base_url:
        raise ValueError("--live requires --supplier-id, --category and --api-base-url")
    state_path = Path(args.state_file)
    seen = _read_seen(state_path)
    end = datetime.now(timezone.utc)
    releases = await fetch_contracts_finder_awards(end - timedelta(hours=24), end)
    async with httpx.AsyncClient(base_url=args.api_base_url, timeout=30.0) as client:
        results, new_seen = await watch_once(client, releases, supplier_id=args.supplier_id, category=args.category, seen=seen, max_history_calls=args.max_history_calls)
    _write_seen(state_path, new_seen)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch exact supplier IDs in Contracts Finder awards")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="Run a synthetic, no-network HTTP demonstration")
    mode.add_argument("--live", action="store_true", help="Poll the latest 24h Contracts Finder window")
    parser.add_argument("--supplier-id", help="Exact published supplier ID to watch")
    parser.add_argument("--category", help="Four-digit CPV prefix")
    parser.add_argument("--api-base-url", help="Awardline HTTP base URL")
    parser.add_argument("--state-file", default=".awardline-watcher-state.json")
    parser.add_argument("--max-history-calls", type=int, default=1)
    args = parser.parse_args()
    try:
        results = asyncio.run(run_demo() if args.demo else _run_live(args))
    except (ValueError, httpx.HTTPError, RuntimeError) as exc:
        parser.exit(1, f"watcher failed: {exc}\n")
    print(json.dumps({"mode": "demo" if args.demo else "live", "triggers": results}, indent=2))


if __name__ == "__main__":
    main()
