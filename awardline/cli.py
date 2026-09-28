"""Explicit, bounded ingestion command. Never runs on import."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .ingest import FetchedPackage, walk_pages


class Throttled(RuntimeError):
    def __init__(self, message: str, wait_seconds: int = 300):
        super().__init__(message)
        self.wait_seconds = wait_seconds


def fetch_json(url: str) -> FetchedPackage:
    for attempt in range(3):
        try:
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "AwardlinePilot/0.1"})
            with urlopen(request, timeout=30) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if "json" not in content_type:
                    raise ValueError("Contracts Finder response was not JSON")
                raw = response.read(20_000_001)
                if len(raw) > 20_000_000:
                    raise ValueError("OCDS package exceeded the 20 MB safety bound")
                return FetchedPackage(json.loads(raw), raw)
        except HTTPError as exc:
            if exc.code == 403:
                raise Throttled("Contracts Finder returned 403. Stop requests for at least five minutes.") from exc
            if exc.code == 429:
                retry = exc.headers.get("Retry-After", "300")
                seconds = int(retry) if retry.isdigit() else 300
                raise Throttled("Contracts Finder returned 429. Respect Retry-After before retrying.", seconds) from exc
            if exc.code not in (500, 502, 503, 504) or attempt == 2:
                raise
        except URLError:
            if attempt == 2:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("Unreachable retry state")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest one fixed Contracts Finder publication window")
    parser.add_argument("--published-from", required=True, help="UTC ISO-8601, e.g. 2026-09-01T00:00:00Z")
    parser.add_argument("--published-to", required=True)
    parser.add_argument("--max-pages", type=int, default=10)
    args = parser.parse_args()
    if args.max_pages < 1:
        parser.error("--max-pages must be positive")
    dsn = os.environ.get("AWARDLINE_DATABASE_URL")
    if not dsn:
        parser.error("AWARDLINE_DATABASE_URL must point to an existing PostgreSQL database")
    import psycopg

    from .postgres import PgIngestRepository

    with psycopg.connect(dsn) as connection:
        repository = PgIngestRepository(connection)
        window = repository.open_window(args.published_from, args.published_to)
        if window["state"] != "running":
            print(json.dumps({"state": window["state"], "pages_seen": window["pages_seen"]}))
            return
        if window["cooldown_until"] and datetime.now(timezone.utc) < window["cooldown_until"]:
            print(json.dumps({"state": "cooldown", "until": window["cooldown_until"].isoformat()}))
            return
        start_url = window["next_url"] if window["pages_seen"] else None
        page_count = 0
        last_request_at = None

        def paced_fetch(url):
            nonlocal last_request_at
            if last_request_at is not None:
                time.sleep(max(0, 5 - (time.monotonic() - last_request_at)))
            last_request_at = time.monotonic()
            return fetch_json(url)

        try:
            for page in walk_pages(paced_fetch, args.published_from, args.published_to, start_url):
                repository.persist_page(window["id"], page)
                page_count += 1
                print(json.dumps({"page": page_count, "releases": len(page.releases), "issues": len(page.issues), "has_next": bool(page.next_url)}))
                if page_count >= args.max_pages:
                    break
        except Throttled as exc:
            repository.mark_throttled(window["id"], exc.wait_seconds)
            print(json.dumps({"state": "cooldown", "seconds": exc.wait_seconds, "reason": str(exc)}))


if __name__ == "__main__":
    main()
