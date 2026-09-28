"""Conservative, deterministic OCDS evidence extraction.

This module never calls a model or guesses a buyer relationship from a name.
"""

from __future__ import annotations

from hashlib import sha256
from datetime import datetime, timezone
import re
from typing import Any

from .ingest import canonical_json


RULE_VERSION = "1.2.0"
SCHEMA_VERSION = "1.0.0"
SOURCE = "contracts_finder"


def obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def arr(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def buyer_identifier(release: dict[str, Any]) -> str | None:
    buyer = obj(release.get("buyer"))
    identifier = obj(buyer.get("identifier"))
    scheme, value = identifier.get("scheme"), identifier.get("id")
    if isinstance(scheme, str) and scheme.strip() and isinstance(value, str) and value.strip():
        return f"{scheme.strip()}:{value.strip()}"
    # Contracts Finder publishes the reusable organisation identifier at
    # buyer.id, with a matching parties[].id. Treat it as source-scoped.
    party_id = buyer.get("id")
    if isinstance(party_id, str) and re.fullmatch(r"GB-(?:CFS|SRS|GOR|LAE)-\S+", party_id):
        if any(obj(p).get("id") == party_id and "buyer" in arr(obj(p).get("roles")) for p in arr(release.get("parties"))):
            return f"{SOURCE}:{party_id}"
    return None


def buyer_identifier_path(release: dict[str, Any]) -> str:
    return "/buyer/identifier" if obj(obj(release.get("buyer")).get("identifier")).get("scheme") else "/buyer/id"


def cpv_prefix(release: dict[str, Any]) -> str | None:
    tender = obj(release.get("tender"))
    classifications = [obj(item).get("classification") for item in arr(tender.get("items"))]
    classifications.append(tender.get("classification"))
    for entry in classifications:
        classification = obj(entry)
        code = classification.get("id")
        if str(classification.get("scheme", "")).upper() == "CPV" and isinstance(code, str) and len(code) >= 4 and code[:4].isdigit():
            return code[:4]
    return None


def cpv_path(release: dict[str, Any]) -> str | None:
    tender = obj(release.get("tender"))
    for index, item in enumerate(arr(tender.get("items"))):
        classification = obj(obj(item).get("classification"))
        if str(classification.get("scheme", "")).upper() == "CPV" and isinstance(classification.get("id"), str) and classification["id"][:4].isdigit():
            return f"/tender/items/{index}/classification"
    if cpv_prefix(release):
        return "/tender/classification"
    return None


def source_ref(release: dict[str, Any], field_path: str) -> dict[str, Any]:
    return {
        "id": sha256(f"{release.get('ocid')}:{release.get('id')}:{field_path}".encode()).hexdigest()[:12],
        "source_url": release.get("uri") or f"https://www.contractsfinder.service.gov.uk/Published/OCDS/Release/{release.get('id')}",
        "ocid": release.get("ocid"),
        "release_id": release.get("id"),
        "field_path": field_path,
    }


def route(release: dict[str, Any]) -> dict[str, Any]:
    tender = obj(release.get("tender"))
    techniques = obj(tender.get("techniques"))
    category = str(tender.get("procurementMethodDetails") or "").lower()
    if "dynamic purchasing system" in category or techniques.get("dynamicPurchasingSystem"):
        kind, path = "dynamic_market_or_dps", "/tender/procurementMethodDetails" if "dynamic purchasing system" in category else "/tender/techniques/dynamicPurchasingSystem"
    elif ("call-off" in category or "call off" in category) and "framework" in category:
        kind, path = "framework_call_off", "/tender/procurementMethodDetails"
    elif techniques.get("frameworkAgreement"):
        kind, path = "framework_establishment", "/tender/techniques/frameworkAgreement"
    else:
        kind, path = "unknown", None
    method = tender.get("procurementMethod")
    tags = {str(tag) for tag in arr(release.get("tag"))}
    if "award" in tags or "awardUpdate" in tags:
        participation = "no_current_competition"
    elif method == "open":
        participation = "open_competition_stated"
    else:
        participation = "unknown"
    refs = [source_ref(release, path)["id"]] if path else []
    if method == "open" and participation == "open_competition_stated":
        refs.append(source_ref(release, "/tender/procurementMethod")["id"])
    elif participation == "no_current_competition":
        refs.append(source_ref(release, "/tag")["id"])
    return {"kind": kind, "participation": participation, "framework_id": None, "evidence_refs": refs}


def _explicit_prior_ocids(current: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for item in arr(current.get("relatedProcesses")):
        item = obj(item)
        relationships = {str(v).lower() for v in arr(item.get("relationship"))}
        if relationships.intersection({"prior", "predecessor", "renewal"}) and isinstance(item.get("identifier"), str):
            out.add(item["identifier"])
    return out


def _value(tender: dict[str, Any]) -> dict[str, Any]:
    value = obj(tender.get("value"))
    return {"amount": value.get("amount"), "currency": value.get("currency"), "basis": tender.get("valueBasis")}


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _snapshot(release: dict[str, Any]) -> dict[str, Any]:
    tender = obj(release.get("tender"))
    return {
        "deadline": obj(tender.get("tenderPeriod")).get("endDate"),
        "value": _value(tender),
        "status": tender.get("status"),
        "route": route(release)["kind"],
        "title": tender.get("title"),
        "description": tender.get("description"),
    }


def build_dossier(
    current: dict[str, Any],
    history: list[dict[str, Any]],
    baseline: dict[str, Any] | None,
    retrieved_at: str,
    *,
    lookback_start: str | None = None,
    coverage_limits: list[str] | None = None,
    lot_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    if not current.get("ocid") or not current.get("id"):
        raise ValueError("Current release needs OCID and release ID")
    if baseline is not None and baseline.get("ocid") != current["ocid"]:
        raise ValueError("Baseline must belong to the same contracting process")
    tender = obj(current.get("tender"))
    buyer = obj(current.get("buyer"))
    buyer_id = buyer_identifier(current)
    cpv = cpv_prefix(current)
    current_date = _instant(current.get("date"))
    lookback_date = _instant(lookback_start) if lookback_start else None
    explicit = _explicit_prior_ocids(current)
    evidence: dict[str, dict[str, Any]] = {}

    def ref(release: dict[str, Any], path: str) -> str:
        entry = source_ref(release, path)
        evidence[entry["id"]] = entry
        return entry["id"]

    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for historical in history:
        if historical.get("ocid") == current["ocid"] or not buyer_id or buyer_identifier(historical) != buyer_id:
            continue
        relationship = "explicit_prior_process" if historical.get("ocid") in explicit else (
            "same_buyer_same_category" if cpv and cpv_prefix(historical) == cpv else None
        )
        if not relationship:
            continue
        for award in arr(historical.get("awards")):
            award = obj(award)
            award_date = _instant(award.get("date"))
            if not award.get("id") or award.get("status") != "active" or not current_date or not award_date or award_date >= current_date:
                continue
            if lookback_start and (not lookback_date or award_date < lookback_date):
                continue
            match_refs = [ref(historical, buyer_identifier_path(historical)), ref(historical, "/awards")]
            if relationship == "explicit_prior_process":
                match_refs.append(ref(current, "/relatedProcesses"))
            else:
                match_refs.extend([ref(current, cpv_path(current)), ref(historical, cpv_path(historical))])
            selected.append((historical, {
                "award_id": award["id"],
                "title": award.get("title"),
                "suppliers": [obj(s).get("name") for s in arr(award.get("suppliers"))[:3]],
                "suppliers_truncated": len(arr(award.get("suppliers"))) > 3,
                "award_date": award.get("date"),
                "contract_end": obj(award.get("contractPeriod")).get("endDate") or next((obj(obj(c).get("period")).get("endDate") for c in arr(historical.get("contracts")) if obj(c).get("awardID") == award["id"]), None),
                "value": obj(award.get("value")) or None,
                "relationship": relationship,
                "match": {"strength": "source_linked" if relationship == "explicit_prior_process" else "identifier_matched", "method": relationship, "probability": None, "evidence_refs": match_refs, "rule_version": RULE_VERSION},
            }))
    # An award often reappears in later OCDS releases of the same process.
    # Keep its newest published version instead of presenting it as several wins.
    by_award: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for historical, award in selected:
        key = (historical["ocid"], str(award["award_id"]))
        previous = by_award.get(key)
        if previous is None or (_instant(historical.get("date")) or datetime.min.replace(tzinfo=timezone.utc)) > (_instant(previous[0].get("date")) or datetime.min.replace(tzinfo=timezone.utc)):
            by_award[key] = (historical, award)
    selected = list(by_award.values())
    selected.sort(key=lambda item: (str(item[1]["award_date"] or ""), str(item[1]["award_id"])), reverse=True)
    awards = [item[1] for item in selected[:3]]

    changes = []
    if baseline is not None:
        before, after = _snapshot(baseline), _snapshot(current)
        paths = {"deadline": "/tender/tenderPeriod/endDate", "value": "/tender/value", "status": "/tender/status", "route": "/tender/techniques", "title": "/tender/title", "description": "/tender/description"}
        for field in ("deadline", "value", "status", "route", "title", "description"):
            if before[field] != after[field]:
                changes.append({"field": field, "previous": before[field], "current": after[field], "detected_at": retrieved_at, "evidence_refs": [ref(baseline, paths[field]), ref(current, paths[field])]})
    changes = changes[:5]

    contact = obj(buyer.get("contactPoint"))
    contact_path = "/buyer/contactPoint"
    if not contact:
        for index, party in enumerate(arr(current.get("parties"))):
            party = obj(party)
            if party.get("id") == buyer.get("id") and "buyer" in arr(party.get("roles")) and obj(party.get("contactPoint")):
                contact = obj(party.get("contactPoint"))
                contact_path = f"/parties/{index}/contactPoint"
                break
    contact_refs = [ref(current, contact_path)] if contact else []
    route_data = route(current)
    for path in ("/tender/techniques/frameworkAgreement", "/tender/techniques/dynamicPurchasingSystem", "/tender/procurementMethodDetails", "/tender/procurementMethod", "/tag"):
        if source_ref(current, path)["id"] in route_data["evidence_refs"]:
            ref(current, path)
    if buyer_id:
        buyer_ref = ref(current, buyer_identifier_path(current))
    else:
        buyer_ref = None

    revision_material = {"current": current, "selected_history": [r for r, _ in selected[:3]], "baseline": baseline, "rule_version": RULE_VERSION, "lot_id": lot_id}
    dossier_id = sha256(f"{SOURCE}:{current['ocid']}:{lot_id or ''}".encode()).hexdigest()[:20]
    def required_evidence_ids() -> set[str]:
        ids = set(route_data["evidence_refs"] + contact_refs + ([buyer_ref] if buyer_ref else []))
        for award in awards:
            ids.update(award["match"]["evidence_refs"])
        for change in changes:
            ids.update(change["evidence_refs"])
        return ids

    # The public schema caps provenance at twelve entries. Never ship a
    # dangling evidence reference just to fit that cap.
    while len(required_evidence_ids()) > 12 and changes:
        changes.pop()
    while len(required_evidence_ids()) > 12 and awards:
        awards.pop()
    revision_material["selected_history"] = [r for r, _ in selected[:len(awards)]]
    revision = sha256(canonical_json(revision_material)).hexdigest()[:20]
    included_ids = required_evidence_ids()

    payload = {
        "dossier": {"id": dossier_id, "schema_version": SCHEMA_VERSION, "revision": revision, "source": SOURCE, "ocid": current["ocid"], "notice_id": current["id"], "lot_id": lot_id},
        "buyer": {"published_name": buyer.get("name"), "identifier": buyer_id, "match": {"strength": "identifier_matched" if buyer_id else "unknown", "method": ("published_namespaced_identifier" if obj(buyer.get("identifier")).get("scheme") else "contracts_finder_party_id") if buyer_id else "none", "probability": None, "evidence_refs": [buyer_ref] if buyer_ref else [], "rule_version": RULE_VERSION}},
        "opportunity": {"title": tender.get("title"), "service_category": cpv, "published_status": tender.get("status"), "deadline": obj(tender.get("tenderPeriod")).get("endDate"), "published_value": _value(tender)},
        "procurement_route": route_data,
        "related_awards": {"coverage_status": "bounded" if lookback_start else "input_history_only", "lookback_start": lookback_start, "awards": awards},
        "material_changes": {"baseline_revision": sha256(canonical_json(baseline)).hexdigest()[:20] if baseline is not None else None, "comparison_status": "compared" if baseline is not None else "no_baseline", "changes": changes},
        "published_contact": {"published_name": contact.get("name"), "email": contact.get("email"), "clarification_url": contact.get("url"), "evidence_refs": contact_refs},
        "provenance": {"retrieved_at": retrieved_at, "data_as_of": current.get("date"), "coverage_limits": coverage_limits or ["Contracts Finder only", "History limited to supplied releases"], "evidence_refs": [entry for key, entry in evidence.items() if key in included_ids]},
    }
    # A wording-only amendment is worth disclosing, but exact string
    # inequality cannot establish a commercially meaningful edit.
    billable_change = any(change["field"] in {"deadline", "value", "status", "route"} for change in changes)
    competition_stated = route_data["participation"] == "open_competition_stated"
    no_explicit_restricted_route = route_data["kind"] in {"standalone", "unknown"}
    return payload, bool((awards or billable_change) and competition_stated and no_explicit_restricted_route)
