"""In-memory result cache so an export doesn't re-run (and re-bill) the query.

Deliberately process-local and volatile: results contain nothing but public
chain data, but they can be large, so entries expire and the store is capped.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from .models import HoldersResponse

DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_ENTRIES = 32


@dataclass
class _Entry:
    result: HoldersResponse
    created_at: float


class JobStore:
    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def put(self, result: HoldersResponse) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._evict_locked()
            self._entries[job_id] = _Entry(result=result, created_at=time.monotonic())
        return job_id

    def get(self, job_id: str) -> HoldersResponse | None:
        with self._lock:
            self._evict_locked()
            entry = self._entries.get(job_id)
            return entry.result if entry else None

    def _evict_locked(self) -> None:
        now = time.monotonic()
        for key, entry in list(self._entries.items()):
            if now - entry.created_at > self._ttl:
                del self._entries[key]
        while len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k].created_at)
            del self._entries[oldest]


store = JobStore()
