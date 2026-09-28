"""PostgreSQL authority for pilot authorization, trial usage, and retries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg


class AccessError(Exception):
    def __init__(self, status_code: int, code: str):
        super().__init__(code)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class UnlockResult:
    payload: dict
    operation_id: str
    access_type: str
    trial_remaining: int


class PilotLedger:
    def __init__(self, dsn: str, cache=None):
        self.dsn = dsn
        self.cache = cache

    @staticmethod
    def _customer(cur, token: str):
        digest = sha256(token.encode("utf-8")).hexdigest()
        cur.execute(
            """SELECT id, trial_limit, trial_used, trial_expires_at FROM pilot_customers
               WHERE token_sha256 = %s AND enabled = true FOR UPDATE""",
            (digest,),
        )
        customer = cur.fetchone()
        if not customer:
            raise AccessError(401, "invalid_token")
        return customer

    @staticmethod
    def _fresh(data_as_of: datetime | None) -> bool:
        now = datetime.now(timezone.utc)
        return data_as_of is not None and now - timedelta(hours=36) <= data_as_of <= now + timedelta(minutes=5)

    @classmethod
    def _preview_row(cls, dossier_id: str, revision: str, payload: dict, eligible: bool, data_as_of: datetime | None) -> dict:
        available = bool(eligible and cls._fresh(data_as_of))
        return {
            "dossier_id": dossier_id,
            "revision": revision,
            "buyer_name": payload["buyer"].get("published_name"),
            "title": payload["opportunity"].get("title"),
            "deadline": payload["opportunity"].get("deadline"),
            "route_kind": payload["procurement_route"].get("kind"),
            "enrichment_available": available,
            "history_coverage": payload["related_awards"].get("coverage_status"),
            "data_as_of": data_as_of.isoformat() if data_as_of else None,
            "price_usdc": "5.00" if available else None,
        }

    def preview(self, dossier_id: str) -> dict:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cur:
                cur.execute(
                    """SELECT d.revision, d.payload, d.eligible, d.data_as_of
                       FROM dossier_heads h JOIN dossiers d ON (h.id = d.id AND h.revision = d.revision)
                       WHERE h.id = %s""",
                    (dossier_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise AccessError(404, "dossier_not_found")
        revision, payload, eligible, data_as_of = row
        return self._preview_row(dossier_id, revision, payload, eligible, data_as_of)

    def search_previews(self, category: str | None, query: str | None, limit: int, offset: int) -> dict:
        if not category and not query:
            raise AccessError(400, "search_filter_required")
        now = datetime.now(timezone.utc)
        predicates = [
            "d.eligible = true",
            "d.data_as_of >= %s",
            "d.data_as_of <= %s",
            "d.payload #>> '{opportunity,published_status}' = 'active'",
            "(d.payload #>> '{opportunity,deadline}')::timestamptz > %s",
        ]
        parameters: list = [now - timedelta(hours=36), now + timedelta(minutes=5), now]
        if category:
            predicates.append("d.payload #>> '{opportunity,service_category}' = %s")
            parameters.append(category)
        if query:
            predicates.append("position(lower(%s) in lower(d.payload #>> '{opportunity,title}')) > 0")
            parameters.append(query)
        sql = f"""SELECT h.id, d.revision, d.payload, d.eligible, d.data_as_of
                  FROM dossier_heads h JOIN dossiers d ON d.id = h.id AND d.revision = h.revision
                  WHERE {' AND '.join(predicates)} ORDER BY h.id LIMIT %s OFFSET %s"""
        parameters.extend([limit, offset])
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cur:
                cur.execute(sql, parameters)
                rows = cur.fetchall()
        return {"items": [self._preview_row(*row) for row in rows], "limit": limit, "offset": offset}

    def unlock(self, token: str, dossier_id: str, revision: str, key: UUID, funding: str) -> UnlockResult:
        request_sha = sha256(f"POST\n/v1/dossiers/{dossier_id}/unlock\n{revision}\n{funding}".encode()).hexdigest()
        with psycopg.connect(self.dsn) as connection:
            with connection.transaction():
                with connection.cursor() as cur:
                    customer_id, trial_limit, trial_used, trial_expires_at = self._customer(cur, token)
                    if funding == "paid":
                        raise AccessError(503, "payments_not_enabled")
                    if funding != "trial":
                        raise AccessError(400, "invalid_funding")

                    cur.execute(
                        """SELECT id, request_sha256, status, dossier_id, revision
                           FROM operations WHERE customer_id = %s AND idempotency_key = %s""",
                        (customer_id, key),
                    )
                    existing = cur.fetchone()
                    if existing:
                        op_id, old_sha, status, old_dossier, old_revision = existing
                        if old_sha != request_sha:
                            raise AccessError(409, "idempotency_conflict")
                        if status != "fulfilled":
                            raise AccessError(409, "operation_pending")
                        cur.execute(
                            """SELECT d.payload FROM entitlements e JOIN dossiers d
                               ON d.id = e.dossier_id AND d.revision = e.revision
                               WHERE e.customer_id = %s AND e.dossier_id = %s AND e.revision = %s""",
                            (customer_id, old_dossier, old_revision),
                        )
                        owned = cur.fetchone()
                        if not owned:
                            raise AccessError(503, "entitlement_inconsistent")
                        return UnlockResult(owned[0], str(op_id), "replay", trial_limit - trial_used)

                    cur.execute(
                        """SELECT d.payload FROM entitlements e JOIN dossiers d
                           ON d.id = e.dossier_id AND d.revision = e.revision
                           WHERE e.customer_id = %s AND e.dossier_id = %s AND e.revision = %s""",
                        (customer_id, dossier_id, revision),
                    )
                    owned = cur.fetchone()
                    if owned:
                        op_id = uuid4()
                        cur.execute(
                            """INSERT INTO operations (id, customer_id, idempotency_key, request_sha256,
                               dossier_id, revision, funding, status)
                               VALUES (%s, %s, %s, %s, %s, %s, 'trial', 'fulfilled')""",
                            (op_id, customer_id, key, request_sha, dossier_id, revision),
                        )
                        return UnlockResult(owned[0], str(op_id), "owned", trial_limit - trial_used)

                    cur.execute(
                        """SELECT d.payload, d.eligible, d.data_as_of
                           FROM dossier_heads h JOIN dossiers d
                           ON d.id = h.id AND d.revision = h.revision
                           WHERE h.id = %s AND h.revision = %s""",
                        (dossier_id, revision),
                    )
                    row = cur.fetchone()
                    if not row:
                        cur.execute("SELECT 1 FROM dossier_heads WHERE id = %s", (dossier_id,))
                        raise AccessError(409 if cur.fetchone() else 404, "revision_changed" if cur.rowcount else "dossier_not_found")
                    payload, eligible, data_as_of = row
                    if not eligible:
                        raise AccessError(409, "unenriched")
                    now = datetime.now(timezone.utc)
                    if data_as_of is None or data_as_of < now - timedelta(hours=36) or data_as_of > now + timedelta(minutes=5):
                        raise AccessError(503, "source_stale")
                    if trial_expires_at <= now:
                        raise AccessError(409, "trial_expired")
                    if trial_used >= trial_limit:
                        raise AccessError(409, "trial_exhausted")

                    op_id = uuid4()
                    cur.execute(
                        """INSERT INTO operations (id, customer_id, idempotency_key, request_sha256,
                           dossier_id, revision, funding, status)
                           VALUES (%s, %s, %s, %s, %s, %s, 'trial', 'fulfilled')""",
                        (op_id, customer_id, key, request_sha, dossier_id, revision),
                    )
                    cur.execute(
                        """INSERT INTO entitlements (id, customer_id, dossier_id, revision, grant_type, operation_id)
                           VALUES (%s, %s, %s, %s, 'trial', %s)""",
                        (uuid4(), customer_id, dossier_id, revision, op_id),
                    )
                    cur.execute("UPDATE pilot_customers SET trial_used = trial_used + 1 WHERE id = %s", (customer_id,))
                    return UnlockResult(payload, str(op_id), "trial", trial_limit - trial_used - 1)

    def owned_revision(self, token: str, dossier_id: str, revision: str) -> dict:
        with psycopg.connect(self.dsn) as connection:
            with connection.transaction():
                with connection.cursor() as cur:
                    customer_id = self._customer(cur, token)[0]
                    cur.execute(
                        """SELECT 1 FROM entitlements e
                           WHERE e.customer_id = %s AND e.dossier_id = %s AND e.revision = %s""",
                        (customer_id, dossier_id, revision),
                    )
                    if not cur.fetchone():
                        raise AccessError(404, "entitlement_not_found")
                    if self.cache is not None:
                        try:
                            cached = self.cache.get(dossier_id, revision)
                            if cached is not None:
                                return cached
                        except Exception:
                            pass  # Cache is disposable; PostgreSQL remains authoritative.
                    cur.execute("SELECT payload FROM dossiers WHERE id = %s AND revision = %s", (dossier_id, revision))
                    row = cur.fetchone()
                    if not row:
                        raise AccessError(503, "entitlement_inconsistent")
                    payload = row[0]
        if self.cache is not None:
            try:
                self.cache.put(dossier_id, revision, payload)
            except Exception:
                pass
        return payload

    def operation_status(self, token: str, operation_id: UUID) -> dict:
        with psycopg.connect(self.dsn) as connection:
            with connection.transaction():
                with connection.cursor() as cur:
                    customer_id = self._customer(cur, token)[0]
                    cur.execute(
                        """SELECT status, dossier_id, revision, funding FROM operations
                           WHERE id = %s AND customer_id = %s""",
                        (operation_id, customer_id),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise AccessError(404, "operation_not_found")
                    return {"operation_id": str(operation_id), "status": row[0], "dossier_id": row[1], "revision": row[2], "funding": row[3]}
