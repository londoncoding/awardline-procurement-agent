"""Phase 3 HTTP adapter. Paid settlement is intentionally disabled."""

from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel
import psycopg

from .ledger import AccessError, PilotLedger
from .cache import RedisPayloadCache
from .buyer_history import PgBuyerHistoryRepository, build_buyer_history, valid_buyer_id


class UnlockRequest(BaseModel):
    revision: str
    funding: str


def _token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer ") or not authorization[7:].strip():
        raise HTTPException(status_code=401, detail="invalid_token")
    return authorization[7:].strip()


def _key(value: str | None) -> UUID:
    if not value:
        raise HTTPException(status_code=400, detail="invalid_idempotency_key")
    try:
        return UUID(value)
    except ValueError:
        pass
    raise HTTPException(status_code=400, detail="invalid_idempotency_key")


def create_app(dsn: str, cache=None, redis_url: str | None = None, *, history_repository=None, research_demo: bool = False) -> FastAPI:
    app = FastAPI(title="Awardline Pilot API", version="0.1.0")
    if cache is None and redis_url:
        cache = RedisPayloadCache.from_url(redis_url)
    ledger = PilotLedger(dsn, cache=cache)
    history_repository = history_repository or PgBuyerHistoryRepository(dsn)

    def call(action):
        try:
            return action()
        except AccessError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            raise HTTPException(status_code=503, detail="database_unavailable") from exc

    @app.get("/v1/buyer-history")
    def buyer_history(
        response: Response,
        buyer_id: str = Query(min_length=3, max_length=300),
        category: str = Query(pattern=r"^\d{4}$"),
        limit: int = Query(default=20, ge=1, le=20),
    ):
        if not valid_buyer_id(buyer_id):
            raise HTTPException(status_code=400, detail="invalid_buyer_id")
        if not research_demo:
            raise HTTPException(status_code=503, detail="buyer_history_payment_disabled")
        result = call(lambda: build_buyer_history(buyer_id, category, history_repository.releases_for_buyer(buyer_id), limit=limit))
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.get("/v1/dossiers/{dossier_id}/preview")
    def preview(dossier_id: str):
        return call(lambda: ledger.preview(dossier_id))

    @app.get("/v1/dossiers")
    def search_dossiers(
        category: str | None = Query(default=None, pattern=r"^\d{4}$"),
        q: str | None = Query(default=None, min_length=2, max_length=80),
        limit: int = Query(default=20, ge=1, le=20),
        offset: int = Query(default=0, ge=0, le=500),
    ):
        return call(lambda: ledger.search_previews(category, q.strip() if q else None, limit, offset))

    @app.post("/v1/dossiers/{dossier_id}/unlock")
    def unlock(
        dossier_id: str,
        body: UnlockRequest,
        response: Response,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        token = _token(authorization)
        key = _key(idempotency_key)
        result = call(lambda: ledger.unlock(token, dossier_id, body.revision, key, body.funding))
        response.headers["X-Awardline-Operation-ID"] = result.operation_id
        response.headers["X-Awardline-Access-Type"] = result.access_type
        response.headers["X-Awardline-Trial-Remaining"] = str(result.trial_remaining)
        response.headers["Cache-Control"] = "private, no-store"
        return result.payload

    @app.get("/v1/dossiers/{dossier_id}/revisions/{revision}")
    def owned_revision(dossier_id: str, revision: str, response: Response, authorization: str | None = Header(default=None)):
        payload = call(lambda: ledger.owned_revision(_token(authorization), dossier_id, revision))
        response.headers["Cache-Control"] = "private, no-store"
        return payload

    @app.get("/v1/operations/{operation_id}")
    def operation_status(operation_id: UUID, authorization: str | None = Header(default=None)):
        return call(lambda: ledger.operation_status(_token(authorization), operation_id))

    return app
