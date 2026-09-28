"""Disposable Redis payload cache. Authorization always happens in PostgreSQL."""

from __future__ import annotations

from hashlib import sha256
import json


DOSSIER_FIELDS = frozenset({"dossier", "buyer", "opportunity", "procurement_route", "related_awards", "material_changes", "published_contact", "provenance"})


class RedisPayloadCache:
    def __init__(self, client, ttl_seconds: int = 3600):
        self.client = client
        self.ttl_seconds = ttl_seconds

    @classmethod
    def from_url(cls, url: str):
        import redis

        return cls(redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=0.1, socket_timeout=0.1))

    @staticmethod
    def _key(dossier_id: str, revision: str) -> str:
        digest = sha256(f"{dossier_id}:{revision}".encode()).hexdigest()
        return f"awardline:dossier:v1:{digest}"

    def get(self, dossier_id: str, revision: str) -> dict | None:
        raw = self.client.get(self._key(dossier_id, revision))
        if raw is None:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict) or frozenset(payload) != DOSSIER_FIELDS:
            return None
        return payload

    def put(self, dossier_id: str, revision: str, payload: dict) -> None:
        if frozenset(payload) != DOSSIER_FIELDS:
            return
        self.client.setex(self._key(dossier_id, revision), self.ttl_seconds, json.dumps(payload, separators=(",", ":")))
