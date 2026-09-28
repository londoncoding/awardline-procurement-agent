"""Contracts Finder OCDS search parsing and safe cursor traversal.

The module deliberately has no database or HTTP-library dependency. A caller
supplies a transport and persists each yielded page before asking for the next.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlencode, urlparse


SOURCE = "contracts_finder"
BASE_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"


class PackageError(ValueError):
    pass


class CursorError(ValueError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class SourceRelease:
    source: str
    ocid: str
    release_id: str
    digest: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class FetchedPackage:
    parsed: dict[str, Any]
    raw_bytes: bytes


@dataclass(frozen=True)
class ParsedPage:
    url: str
    releases: tuple[SourceRelease, ...]
    issues: tuple[dict[str, Any], ...]
    next_url: str | None
    raw: dict[str, Any]
    raw_bytes: bytes


def parse_package(package: Any, url: str, raw_bytes: bytes | None = None) -> ParsedPage:
    if not isinstance(package, dict) or not isinstance(package.get("releases"), list):
        raise PackageError("Expected an OCDS release package with a releases array")
    links = package.get("links") or {}
    if not isinstance(links, dict):
        raise PackageError("Invalid links object")
    next_url = links.get("next")
    if next_url is not None and not isinstance(next_url, str):
        raise PackageError("Invalid next link")

    releases: list[SourceRelease] = []
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(package["releases"]):
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ("ocid", "id")):
            issues.append({"index": index, "reason": "missing_ocid_or_release_id", "raw": item})
            continue
        releases.append(SourceRelease(SOURCE, item["ocid"], item["id"], sha256(canonical_json(item)).hexdigest(), item))
    if len(package["releases"]) == 100 and not next_url:
        issues.append({"index": None, "reason": "full_page_without_next_link", "raw": None})
    return ParsedPage(url, tuple(releases), tuple(issues), next_url, package, raw_bytes if raw_bytes is not None else canonical_json(package))


def initial_url(published_from: str, published_to: str) -> str:
    try:
        start = datetime.fromisoformat(published_from.replace("Z", "+00:00"))
        end = datetime.fromisoformat(published_to.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("A valid fixed publication window is required") from exc
    if start.tzinfo is None or end.tzinfo is None or start.astimezone(timezone.utc) >= end.astimezone(timezone.utc):
        raise ValueError("An increasing timezone-aware publication window is required")
    return BASE_URL + "?" + urlencode({"publishedFrom": published_from, "publishedTo": published_to, "limit": 100})


def validate_next(url: str, published_from: str, published_to: str) -> None:
    parsed = urlparse(url)
    base = urlparse(BASE_URL)
    if (parsed.scheme, parsed.netloc.lower(), parsed.path) != (base.scheme, base.netloc.lower(), base.path):
        raise CursorError("Next link is outside the Contracts Finder search endpoint")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query.get("publishedFrom") != [published_from] or query.get("publishedTo") != [published_to]:
        raise CursorError("Next link changes or omits the frozen publication window")
    if len(query.get("cursor", [])) != 1 or not query["cursor"][0]:
        raise CursorError("Next link has no cursor")
    if "limit" in query and query["limit"] != ["100"]:
        raise CursorError("Next link changes the page limit")


def walk_pages(fetch_json: Callable[[str], Any], published_from: str, published_to: str, resume_url: str | None = None) -> Iterator[ParsedPage]:
    """Yield pages; caller must commit each page before requesting the next."""
    url = resume_url or initial_url(published_from, published_to)
    if resume_url:
        validate_next(resume_url, published_from, published_to)
    seen: set[str] = set()
    while True:
        if url in seen:
            raise CursorError("Cursor loop detected")
        seen.add(url)
        fetched = fetch_json(url)
        page = parse_package(fetched.parsed, url, fetched.raw_bytes) if isinstance(fetched, FetchedPackage) else parse_package(fetched, url)
        if page.next_url:
            validate_next(page.next_url, published_from, published_to)
        yield page
        if not page.next_url:
            break
        url = page.next_url
