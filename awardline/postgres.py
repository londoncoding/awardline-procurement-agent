"""PostgreSQL persistence; each page and its cursor commit atomically.

Requires psycopg 3. Integration tests need a real PostgreSQL instance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .ingest import ParsedPage, SOURCE, initial_url


class WindowOutOfOrder(RuntimeError):
    pass


class PgIngestRepository:
    def __init__(self, connection):
        self.connection = connection
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SELECT current_setting('server_encoding')")
                encoding = cur.fetchone()[0]
        if encoding.upper() != "UTF8":
            raise RuntimeError(f"Awardline requires a UTF8 PostgreSQL database; found {encoding}")

    def open_window(self, published_from: str, published_to: str) -> dict:
        start = datetime.fromisoformat(published_from.replace("Z", "+00:00"))
        end = datetime.fromisoformat(published_to.replace("Z", "+00:00"))
        url = initial_url(published_from, published_to)
        with self.connection.transaction():
            with self.connection.cursor() as cur:
                cur.execute(
                    """INSERT INTO ingest_windows (id, source, published_from, published_to, next_url)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (source, published_from, published_to) DO NOTHING""",
                    (uuid4(), SOURCE, start, end, url),
                )
                cur.execute(
                    """SELECT id, next_url, state, pages_seen, releases_seen, issues_seen, cooldown_until
                       FROM ingest_windows WHERE source = %s AND published_from = %s AND published_to = %s""",
                    (SOURCE, start, end),
                )
                row = cur.fetchone()
        return dict(zip(("id", "next_url", "state", "pages_seen", "releases_seen", "issues_seen", "cooldown_until"), row))

    def mark_throttled(self, window_id, seconds: int) -> None:
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        with self.connection.transaction():
            with self.connection.cursor() as cur:
                cur.execute("UPDATE ingest_windows SET cooldown_until = %s, updated_at = now() WHERE id = %s", (until, window_id))

    def verified_coverage(self, start: datetime, end: datetime) -> bool:
        from .coverage import audit_coverage

        with self.connection.transaction():
            with self.connection.cursor() as cur:
                cur.execute(
                    """SELECT published_from, published_to, state, issues_seen FROM ingest_windows
                       WHERE source = %s AND published_from < %s AND published_to > %s""",
                    (SOURCE, end, start),
                )
                rows = cur.fetchall()
        if not audit_coverage(rows, start, end)["complete"]:
            return False
        # The issue window's raw releases must also appear verbatim in the
        # accepted replacement windows. Otherwise its untrusted extra records
        # would remain in source_releases and contaminate materialization.
        with self.connection.transaction():
            with self.connection.cursor() as cur:
                cur.execute(
                    """WITH suspect AS (
                           SELECT item.release FROM raw_pages p
                           JOIN ingest_windows w ON w.id = p.window_id
                           CROSS JOIN LATERAL jsonb_array_elements(p.package->'releases') AS item(release)
                           WHERE w.source = %s AND w.published_from = %s AND w.published_to = %s
                             AND w.state = 'complete_with_issues'
                       ), accepted AS (
                           SELECT item.release FROM raw_pages p
                           JOIN ingest_windows w ON w.id = p.window_id
                           CROSS JOIN LATERAL jsonb_array_elements(p.package->'releases') AS item(release)
                           WHERE w.source = %s AND w.published_from < %s AND w.published_to > %s
                             AND w.state = 'complete' AND w.issues_seen = 0
                       )
                       SELECT EXISTS(SELECT release FROM suspect EXCEPT SELECT release FROM accepted)""",
                    (SOURCE, start, end, SOURCE, end, start),
                )
                has_unverified_release = cur.fetchone()[0]
        return not has_unverified_release

    def persist_page(self, window_id, page: ParsedPage) -> None:
        # A transaction covers the raw page, all releases, issues, and the cursor.
        from psycopg.types.json import Jsonb

        with self.connection.transaction():
            with self.connection.cursor() as cur:
                cur.execute("SELECT next_url, state FROM ingest_windows WHERE id = %s FOR UPDATE", (window_id,))
                row = cur.fetchone()
                if row is None or row[1] != "running" or row[0] != page.url:
                    raise WindowOutOfOrder("Page does not match the durable next cursor")
                cur.execute(
                    """INSERT INTO raw_pages (id, window_id, page_url, next_url, package, response_bytes)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (uuid4(), window_id, page.url, page.next_url, Jsonb(page.raw), page.raw_bytes),
                )
                changed_ids = 0
                for release in page.releases:
                    cur.execute(
                        """SELECT content_sha256 FROM source_releases
                           WHERE source = %s AND ocid = %s AND release_id = %s""",
                        (release.source, release.ocid, release.release_id),
                    )
                    known_digests = {row[0] for row in cur.fetchall()}
                    if known_digests and release.digest not in known_digests:
                        changed_ids += 1
                        cur.execute(
                            """INSERT INTO ingest_issues (id, window_id, page_url, release_index, reason, raw)
                               VALUES (%s, %s, %s, NULL, %s, %s)
                               ON CONFLICT (window_id, page_url, release_index, reason) DO NOTHING""",
                            (uuid4(), window_id, page.url, "changed_same_release_id", Jsonb({"ocid": release.ocid, "release_id": release.release_id, "digest": release.digest})),
                        )
                    cur.execute(
                        """INSERT INTO source_releases (id, source, ocid, release_id, content_sha256, release)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (source, ocid, release_id, content_sha256) DO NOTHING""",
                        (uuid4(), release.source, release.ocid, release.release_id, release.digest, Jsonb(release.raw)),
                    )
                for issue in page.issues:
                    cur.execute(
                        """INSERT INTO ingest_issues (id, window_id, page_url, release_index, reason, raw)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (window_id, page_url, release_index, reason) DO NOTHING""",
                        (uuid4(), window_id, page.url, issue["index"], issue["reason"], Jsonb(issue["raw"])),
                    )
                state = "running" if page.next_url else ("complete_with_issues" if page.issues or changed_ids else "complete")
                cur.execute(
                    """UPDATE ingest_windows SET next_url = %s, state = %s,
                       pages_seen = pages_seen + 1, releases_seen = releases_seen + %s,
                       issues_seen = issues_seen + %s, cooldown_until = NULL, updated_at = now() WHERE id = %s""",
                    (page.next_url or "", state, len(page.releases), len(page.issues) + changed_ids, window_id),
                )
