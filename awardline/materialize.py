"""Build dossier snapshots from a bounded set of ingested OCDS releases."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from typing import Any

from .enrichment import _instant, build_dossier, buyer_identifier


def _coverage_spans(windows: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    spans: list[tuple[datetime, datetime]] = []
    for left, right in sorted(windows):
        if not spans or left > spans[-1][1]:
            spans.append((left, right))
        else:
            spans[-1] = (spans[-1][0], max(spans[-1][1], right))
    return spans


def _source_as_of(windows: list[tuple[datetime, datetime]], published: datetime, now: datetime) -> datetime | None:
    """A clean, gap-free source window from publication refreshes an old notice."""
    for left, right in _coverage_spans(windows):
        if left <= published < right and right <= now:
            return right
    return None


def _current_candidate(process: list[dict[str, Any]], now: datetime) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    dated = [(index, _instant(item.get("date")), item) for index, item in enumerate(process)]
    dated = [(index, when, item) for index, when, item in dated if when and when <= now]
    if not dated:
        return None
    _, current_date, current = max(dated, key=lambda entry: (entry[1], entry[0]))
    tags = set(current.get("tag") or [])
    if not tags.intersection({"tender", "tenderAmendment"}) or tags.intersection({"award", "awardUpdate"}):
        return None
    tender = current.get("tender") or {}
    if tender.get("status") != "active" or tender.get("lots"):
        return None
    deadline = _instant((tender.get("tenderPeriod") or {}).get("endDate"))
    if deadline is None or deadline <= now:
        return None
    prior = [(index, when, item) for index, when, item in dated if when < current_date]
    baseline = max(prior, key=lambda entry: (entry[1], entry[0]))[2] if prior else None
    return current, baseline


def candidate_dossiers(releases: list[dict[str, Any]], now: datetime, lookback_start: str | None) -> list[tuple[dict[str, Any], bool]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in releases:
        if item.get("ocid"):
            grouped.setdefault(item["ocid"], []).append(item)
    award_releases = [item for item in releases if isinstance(item.get("awards"), list) and item["awards"]]
    results = []
    for ocid, process in sorted(grouped.items()):
        selected = _current_candidate(process, now)
        if selected is None:
            continue
        current, baseline = selected
        payload, eligible = build_dossier(
            current,
            award_releases,
            baseline,
            now.isoformat(),
            lookback_start=lookback_start,
            coverage_limits=["Contracts Finder only", "Ingested releases may have publication-window gaps", "Lots excluded until lot-level evidence rules exist"],
        )
        results.append((payload, eligible))
    return results


def _history_for_buyer(connection, buyer_id: str | None) -> list[dict[str, Any]]:
    if not buyer_id:
        return []
    if buyer_id.startswith("contracts_finder:"):
        condition = "release #>> '{buyer,id}' = %s"
        parameters = (buyer_id.removeprefix("contracts_finder:"),)
    else:
        scheme, _, value = buyer_id.partition(":")
        condition = "release #>> '{buyer,identifier,scheme}' = %s AND release #>> '{buyer,identifier,id}' = %s"
        parameters = (scheme, value)
    with connection.cursor() as cur:
        cur.execute(
            f"""SELECT ocid, release_id, release FROM source_releases
                WHERE source = 'contracts_finder' AND ({condition})
                  AND jsonb_typeof(release->'awards') = 'array' AND release->'awards' <> '[]'::jsonb
                ORDER BY first_seen_at, id""",
            parameters,
        )
        latest_per_release = {(ocid, release_id): release for ocid, release_id, release in cur.fetchall()}
    return list(latest_per_release.values())


def _persist_batch(connection, dossiers: list[tuple[dict[str, Any], bool, datetime | None]], retired: list[str]) -> None:
    from psycopg.types.json import Jsonb

    with connection.transaction():
        with connection.cursor() as cur:
            for payload, eligible, checked_through in dossiers:
                meta = payload["dossier"]
                as_of = checked_through or _instant(payload["provenance"].get("data_as_of"))
                cur.execute(
                    """INSERT INTO dossiers (id, revision, payload, eligible, data_as_of)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (id, revision) DO UPDATE SET data_as_of = EXCLUDED.data_as_of""",
                    (meta["id"], meta["revision"], Jsonb(payload), eligible, as_of),
                )
                cur.execute(
                    """INSERT INTO dossier_heads (id, revision) VALUES (%s, %s)
                       ON CONFLICT (id) DO UPDATE SET revision = EXCLUDED.revision""",
                    (meta["id"], meta["revision"]),
                )
            if retired:
                cur.execute("DELETE FROM dossier_heads WHERE id = ANY(%s)", (retired,))


def materialize_database(dsn: str, batch_size: int = 500) -> dict[str, int]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    import psycopg

    now = datetime.now(timezone.utc)
    source_count = 0
    dossier_count = 0
    eligible_count = 0
    pending: list[tuple[dict[str, Any], bool, datetime | None]] = []
    retired: list[str] = []
    with psycopg.connect(dsn) as scan, psycopg.connect(dsn, autocommit=True) as work:
        with work.cursor() as cur:
            cur.execute(
                """SELECT published_from, published_to FROM ingest_windows
                   WHERE source = 'contracts_finder' AND state = 'complete' AND issues_seen = 0
                     AND published_to <= %s""",
                (now,),
            )
            coverage_spans = _coverage_spans(cur.fetchall())
        with scan.cursor(name="awardline_process_scan") as cursor:
            cursor.itersize = batch_size
            cursor.execute(
                """SELECT ocid, release_id, release FROM source_releases
                   WHERE source = 'contracts_finder' ORDER BY ocid, first_seen_at, id"""
            )
            group_ocid = None
            group: dict[str, dict[str, Any]] = {}

            def finish_process(ocid: str, process: list[dict[str, Any]]) -> None:
                nonlocal dossier_count, eligible_count
                selected = _current_candidate(process, now)
                if selected is None:
                    retired.append(sha256(f"contracts_finder:{ocid}:".encode()).hexdigest()[:20])
                else:
                    current, baseline = selected
                    history = _history_for_buyer(work, buyer_identifier(current))
                    payload, eligible = build_dossier(
                        current, history, baseline, now.isoformat(),
                        lookback_start=None,
                        coverage_limits=["Contracts Finder only", "Ingested releases may have publication-window gaps", "Lots excluded until lot-level evidence rules exist"],
                    )
                    published = _instant(current.get("date"))
                    checked_through = _source_as_of(coverage_spans, published, now) if published else None
                    pending.append((payload, eligible, checked_through))
                    dossier_count += 1
                    eligible_count += int(eligible)
                if len(pending) + len(retired) >= batch_size:
                    _persist_batch(work, pending, retired)
                    pending.clear()
                    retired.clear()

            for ocid, release_id, release in cursor:
                source_count += 1
                if group_ocid is not None and ocid != group_ocid:
                    finish_process(group_ocid, list(group.values()))
                    group.clear()
                group_ocid = ocid
                group[release_id] = release  # Latest content for an identical release ID wins.
            if group_ocid is not None:
                finish_process(group_ocid, list(group.values()))
            if pending or retired:
                _persist_batch(work, pending, retired)
    return {"source_releases": source_count, "dossiers": dossier_count, "eligible": eligible_count}


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream dossier snapshots from ingested releases")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    dsn = os.environ.get("AWARDLINE_DATABASE_URL")
    if not dsn:
        parser.error("AWARDLINE_DATABASE_URL is required")
    print(json.dumps(materialize_database(dsn, args.batch_size)))


if __name__ == "__main__":
    main()
