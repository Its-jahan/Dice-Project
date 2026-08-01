"""Result store, so an export doesn't re-run (and re-bill) the query.

Backed by files rather than process memory, for one concrete reason: the
service runs uvicorn with several workers. A result created by the worker that
handled ``POST /api/holders`` is invisible to the worker that handles the
follow-up ``GET /api/export``, which surfaced to the user as "Result expired or
unknown job id" on every single download. Disk is shared by all workers, and
survives a restart as a bonus.

Contents are public chain data, but they can be large, so entries expire and
the directory is capped.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .models import HoldersResponse

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_ENTRIES = 32


def default_job_dir() -> Path:
    """Where results live. Override with ``DICE_JOB_DIR``.

    The systemd unit points this at its StateDirectory; the fallback keeps
    local development working with no configuration.
    """
    configured = os.getenv("DICE_JOB_DIR")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "dice-jobs"


class JobStore:
    def __init__(
        self,
        directory: Path | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._dir = directory or default_job_dir()
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def _path(self, job_id: str) -> Path:
        # job ids are generated here, never taken from the caller, but keep the
        # filename derivation total anyway so a stray id cannot escape the dir.
        return self._dir / f"{job_id}.json"

    def put(self, result: HoldersResponse) -> str:
        job_id = uuid.uuid4().hex
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(job_id)
        # Write then rename, so a reader in another worker never sees a
        # half-written file.
        temporary = path.with_suffix(".partial")
        temporary.write_text(result.model_dump_json(), encoding="utf-8")
        temporary.replace(path)
        with self._lock:
            self._prune()
        return job_id

    def get(self, job_id: str) -> HoldersResponse | None:
        if not _is_job_id(job_id):
            return None
        path = self._path(job_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        if self._age(path) > self._ttl:
            return None
        try:
            return HoldersResponse.model_validate_json(raw)
        except ValueError:
            log.warning("unreadable job file %s", path.name)
            return None

    def _age(self, path: Path) -> float:
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return float("inf")

    def _prune(self) -> None:
        """Drop expired files, then the oldest until back under the cap.

        Pruning happens only on write. Doing it on read — as an earlier
        in-memory version did — meant a download at capacity could delete
        somebody else's result.
        """
        try:
            files = sorted(
                self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime
            )
        except OSError:
            return

        for path in list(files):
            if self._age(path) > self._ttl:
                path.unlink(missing_ok=True)
                files.remove(path)

        while len(files) > self._max_entries:
            files.pop(0).unlink(missing_ok=True)


def _is_job_id(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


store = JobStore()
