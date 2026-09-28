"""Bounded, resumable Contracts Finder historical backfill.

One invocation processes only a small explicit number of windows and pages.
Re-running it resumes the first unfinished durable cursor.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import time
from typing import Callable, Iterator

from .cli import Throttled, fetch_json
from .ingest import walk_pages


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Backfill times must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def plan_windows(start: datetime, end: datetime, step: timedelta = timedelta(days=1)) -> Iterator[tuple[datetime, datetime]]:
    start, end = _utc(start), _utc(end)
    if start >= end or step <= timedelta(0):
        raise ValueError("Backfill needs an increasing interval and positive window size")
    current = start
    while current < end:
        following = min(current + step, end)
        yield current, following
        current = following


def run_backfill(
    repository,
    fetch: Callable[[str], object],
    start: datetime,
    end: datetime,
    *,
    step: timedelta = timedelta(days=1),
    max_windows: int = 1,
    max_pages: int = 10,
) -> dict:
    if max_windows < 1 or max_pages < 1:
        raise ValueError("max_windows and max_pages must be positive")
    pages = 0
    attempted_windows = 0
    completed_windows = 0
    for window_start, window_end in plan_windows(start, end, step):
        from_text, to_text = _text(window_start), _text(window_end)
        window = repository.open_window(from_text, to_text)
        if window["state"] == "complete":
            completed_windows += 1
            continue
        if window["state"] == "complete_with_issues":
            # A full 100-release page without a next cursor is uncertain. A
            # separately ingested set of smaller, issue-free windows can
            # establish coverage of the same interval without rewriting it.
            if repository.verified_coverage(window_start, window_end):
                completed_windows += 1
                continue
            return {"state": "needs_review", "window": [from_text, to_text], "pages": pages, "windows_completed": completed_windows}
        if attempted_windows >= max_windows:
            return {"state": "window_limit", "window": [from_text, to_text], "pages": pages, "windows_completed": completed_windows}
        if pages >= max_pages:
            return {"state": "page_limit", "window": [from_text, to_text], "pages": pages, "windows_completed": completed_windows}
        cooldown = window.get("cooldown_until")
        if cooldown and _utc(cooldown) > datetime.now(timezone.utc):
            return {"state": "cooldown", "window": [from_text, to_text], "until": _text(cooldown), "pages": pages, "windows_completed": completed_windows}
        attempted_windows += 1
        resume = window["next_url"] if window["pages_seen"] else None
        try:
            for page in walk_pages(fetch, from_text, to_text, resume):
                repository.persist_page(window["id"], page)
                pages += 1
                if page.next_url and pages >= max_pages:
                    return {"state": "page_limit", "window": [from_text, to_text], "pages": pages, "windows_completed": completed_windows}
        except Throttled as exc:
            repository.mark_throttled(window["id"], exc.wait_seconds)
            return {"state": "cooldown", "window": [from_text, to_text], "seconds": exc.wait_seconds, "pages": pages, "windows_completed": completed_windows}
        settled = repository.open_window(from_text, to_text)
        if settled["state"] != "complete":
            return {"state": "needs_review", "window": [from_text, to_text], "pages": pages, "windows_completed": completed_windows}
        completed_windows += 1
    return {"state": "complete", "pages": pages, "windows_completed": completed_windows}


def main() -> None:
    parser = argparse.ArgumentParser(description="Resume a bounded Contracts Finder backfill")
    parser.add_argument("--published-from", required=True, help="UTC ISO-8601 start")
    parser.add_argument("--published-to", required=True, help="UTC ISO-8601 end")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--max-windows", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=10)
    args = parser.parse_args()
    if args.window_hours < 1 or args.window_hours > 24 or args.max_windows < 1 or args.max_pages < 1:
        parser.error("window-hours must be 1–24 and limits must be positive")
    dsn = os.environ.get("AWARDLINE_DATABASE_URL")
    if not dsn:
        parser.error("AWARDLINE_DATABASE_URL is required")
    try:
        start = datetime.fromisoformat(args.published_from.replace("Z", "+00:00"))
        end = datetime.fromisoformat(args.published_to.replace("Z", "+00:00"))
        next(plan_windows(start, end, timedelta(hours=args.window_hours)))
    except (ValueError, StopIteration) as exc:
        parser.error(str(exc))
    import psycopg

    from .postgres import PgIngestRepository

    last_request_at = None

    def paced_fetch(url):
        nonlocal last_request_at
        if last_request_at is not None:
            time.sleep(max(0, 5 - (time.monotonic() - last_request_at)))
        last_request_at = time.monotonic()
        return fetch_json(url)

    with psycopg.connect(dsn) as connection:
        result = run_backfill(
            PgIngestRepository(connection), paced_fetch, start, end,
            step=timedelta(hours=args.window_hours), max_windows=args.max_windows, max_pages=args.max_pages,
        )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
