"""Conservative audit of completed, issue-free publication windows."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from typing import Iterable


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Coverage bounds must be timezone-aware")
    return value.astimezone(timezone.utc)


def audit_coverage(
    windows: Iterable[tuple[datetime, datetime, str, int]],
    start: datetime,
    end: datetime,
) -> dict:
    start, end = _utc(start), _utc(end)
    if start >= end:
        raise ValueError("Coverage interval must increase")
    accepted = []
    rejected = 0
    for left, right, state, issues in windows:
        left, right = _utc(left), _utc(right)
        if right <= start or left >= end:
            continue
        if state == "complete" and issues == 0:
            accepted.append((max(left, start), min(right, end)))
        else:
            rejected += 1
    accepted.sort()
    cursor = start
    gaps = []
    for left, right in accepted:
        if left > cursor:
            gaps.append([cursor.isoformat(), left.isoformat()])
        cursor = max(cursor, right)
    if cursor < end:
        gaps.append([cursor.isoformat(), end.isoformat()])
    return {"complete": not gaps, "gaps": gaps, "accepted_windows": len(accepted), "rejected_windows": rejected}


def audit_database(dsn: str, start: datetime, end: datetime) -> dict:
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cur:
            cur.execute(
                """SELECT published_from, published_to, state, issues_seen FROM ingest_windows
                   WHERE source = 'contracts_finder' AND published_from < %s AND published_to > %s""",
                (_utc(end), _utc(start)),
            )
            rows = cur.fetchall()
    return audit_coverage(rows, start, end)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit whether stored Contracts Finder windows cover an interval")
    parser.add_argument("--published-from", required=True)
    parser.add_argument("--published-to", required=True)
    args = parser.parse_args()
    dsn = os.environ.get("AWARDLINE_DATABASE_URL")
    if not dsn:
        parser.error("AWARDLINE_DATABASE_URL is required")
    try:
        start = datetime.fromisoformat(args.published_from.replace("Z", "+00:00"))
        end = datetime.fromisoformat(args.published_to.replace("Z", "+00:00"))
        result = audit_database(dsn, start, end)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
