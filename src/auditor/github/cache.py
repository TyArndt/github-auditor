"""ETag cache for REST GETs.

Kept in memory during a run (worker threads read/write it) and persisted to
`inventory.http_cache` before and after the run by the main thread.
"""

import threading
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class CachedResponse:
    etag: str
    body: Any
    next_url: str | None


class EtagCache:
    def __init__(self) -> None:
        self._entries: dict[str, CachedResponse] = {}
        self._dirty: set[str] = set()
        self._lock = threading.Lock()

    def get(self, url: str) -> CachedResponse | None:
        with self._lock:
            return self._entries.get(url)

    def put(self, url: str, etag: str, body: Any, next_url: str | None) -> None:
        with self._lock:
            self._entries[url] = CachedResponse(etag, body, next_url)
            self._dirty.add(url)

    def __len__(self) -> int:
        return len(self._entries)

    def load(self, conn: psycopg.Connection[Any]) -> None:
        rows = conn.execute("SELECT url, etag, body FROM inventory.http_cache").fetchall()
        with self._lock:
            for url, etag, stored in rows:
                if etag and isinstance(stored, dict) and "data" in stored:
                    self._entries[url] = CachedResponse(etag, stored["data"], stored.get("next"))

    def flush(self, conn: psycopg.Connection[Any]) -> int:
        with self._lock:
            dirty = [(url, self._entries[url]) for url in self._dirty]
            self._dirty.clear()
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO inventory.http_cache (url, etag, body, fetched_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (url) DO UPDATE
                   SET etag = EXCLUDED.etag, body = EXCLUDED.body, fetched_at = now()
                """,
                [
                    (url, entry.etag, Jsonb({"data": entry.body, "next": entry.next_url}))
                    for url, entry in dirty
                ],
            )
        return len(dirty)
