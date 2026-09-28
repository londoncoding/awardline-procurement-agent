"""A bounded, source-backed buyer/category award-history action.

Names are display-only. Relationships require the exact buyer identifier and CPV
prefix published by Contracts Finder; no incumbent or renewal is inferred.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Any

import psycopg

from .enrichment import buyer_identifier, cpv_prefix, obj, arr, source_ref
from .ingest import canonical_json


SOURCE = "contracts_finder"
SCHEMA_VERSION = "0.1.0"


def valid_buyer_id(value: str) -> bool:
    return bool(re.fullmatch(r"(?:contracts_finder:GB-(?:CFS|SRS|GOR|LAE)-[^\s:]+|(?!contracts_finder:)[^:\s]+:[^:\s]+)", value))


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(timezone.utc) if result.tzinfo else None


class PgBuyerHistoryRepository:
    """Read raw award-bearing releases using the existing buyer-ID indexes."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def releases_for_buyer(self, buyer_id: str) -> list[dict[str, Any]]:
        if not valid_buyer_id(buyer_id):
            raise ValueError("A published, source-scoped buyer identifier is required")
        if buyer_id.startswith("contracts_finder:"):
            where = "release #>> '{buyer,id}' = %s"
            params = (buyer_id.split(":", 1)[1],)
        else:
            scheme, identifier = buyer_id.split(":", 1)
            where = "release #>> '{buyer,identifier,scheme}' = %s AND release #>> '{buyer,identifier,id}' = %s"
            params = (scheme, identifier)
        with psycopg.connect(self.dsn) as connection:
            rows = connection.execute(
                f"""SELECT release FROM source_releases
                    WHERE source = %s AND {where}
                      AND jsonb_typeof(release->'awards') = 'array'
                      AND release->'awards' <> '[]'::jsonb""",
                (SOURCE, *params),
            ).fetchall()
        # SQL narrows candidates; the extractor verifies party role and exact ID.
        return [row[0] for row in rows if buyer_identifier(row[0]) == buyer_id]


def build_buyer_history(buyer_id: str, category: str, releases: list[dict[str, Any]], *, limit: int = 20) -> dict[str, Any]:
    if not valid_buyer_id(buyer_id) or not re.fullmatch(r"\d{4}", category) or not 1 <= limit <= 20:
        raise ValueError("Invalid buyer, category or result limit")
    candidates: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    observed: list[datetime] = []
    buyer_name = None
    for release in releases:
        if buyer_identifier(release) != buyer_id:
            continue
        published = _instant(release.get("date"))
        if published and cpv_prefix(release) == category:
            observed.append(published)
        if not release.get("ocid") or not release.get("id") or not published:
            continue
        if not buyer_name and cpv_prefix(release) == category:
            buyer_name = obj(release.get("buyer")).get("name")
        for award in arr(release.get("awards")):
            award = obj(award)
            if not award.get("id"):
                continue
            key = (str(release["ocid"]), str(award["id"]))
            previous = candidates.get(key)
            rank = (published, str(release["id"]), sha256(canonical_json(release)).hexdigest())
            if previous is None:
                candidates[key] = (release, award)
            else:
                old = previous[0]
                old_rank = (_instant(old.get("date")), str(old["id"]), sha256(canonical_json(old)).hexdigest())
                if rank > old_rank:
                    candidates[key] = (release, award)

    selected = []
    for (ocid, award_id), (release, award) in candidates.items():
        if cpv_prefix(release) != category:
            continue
        suppliers = [
            {"id": s["id"], "name": s.get("name")}
            for raw in arr(award.get("suppliers"))
            if (s := obj(raw)).get("id") and isinstance(s["id"], str)
        ]
        if award.get("status") != "active" or not _instant(award.get("date")) or not suppliers:
            continue
        contract_end = obj(award.get("contractPeriod")).get("endDate")
        if not contract_end:
            contract_end = next((obj(obj(c).get("period")).get("endDate") for c in arr(release.get("contracts")) if obj(c).get("awardID") == award_id), None)
        selected.append({
            "ocid": ocid,
            "award_id": award_id,
            "award_date": award["date"],
            "suppliers": suppliers,
            "value": obj(award.get("value")) or None,
            "contract_end": contract_end,
            "evidence": source_ref(release, "/awards"),
        })
    selected.sort(key=lambda item: (_instant(item["award_date"]), item["ocid"], item["award_id"]), reverse=True)
    visible = selected[:limit]
    revision = sha256(canonical_json({"buyer_id": buyer_id, "category": category, "awards": selected})).hexdigest()[:20]
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "query": {"buyer_id": buyer_id, "category": category},
        "buyer": {"published_name": buyer_name, "identifier": buyer_id, "match": {
            "strength": "identifier_matched" if observed else "unknown", "method": ("contracts_finder_party_id" if buyer_id.startswith("contracts_finder:") else "published_namespaced_identifier") if observed else "none",
            "probability": None,
        }},
        "awards": visible,
        "coverage": {
            "source": SOURCE, "status": "observed_records_only",
            "observed_from": min(observed).isoformat() if observed else None,
            "observed_to": max(observed).isoformat() if observed else None,
            "matched_awards": len(selected), "returned_awards": len(visible), "truncated": len(selected) > limit,
            "limitations": ["Only ingested Contracts Finder releases", "No inference of incumbency, legal-entity identity or future renewal"],
        },
    }
