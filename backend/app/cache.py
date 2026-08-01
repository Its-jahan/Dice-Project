"""A small shared cache for things that cost a Dune execution to work out.

Resolving which balance table to read runs a real query against Dune, which
costs credits. Holding that answer in process memory meant every uvicorn worker
paid for it separately, a restart paid again, and "refresh" only cleared the
one worker that happened to serve the request. Disk is shared by all workers
and survives a restart.

Entries are plain JSON and hold nothing sensitive — table and column names from
a public catalogue.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Long enough to avoid re-billing a resolution on every request, short enough
#: that a rotated build schema is picked up the same day without intervention.
DEFAULT_TTL_SECONDS = 6 * 3600

_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def default_cache_dir() -> Path:
    configured = os.getenv("DICE_CACHE_DIR") or os.getenv("DICE_JOB_DIR")
    base = Path(configured) if configured else Path(tempfile.gettempdir())
    return base / "cache"


class DiskCache:
    def __init__(
        self, directory: Path | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> None:
        self._dir = directory or default_cache_dir()
        self._ttl = ttl_seconds

    def _path(self, key: str) -> Path | None:
        if not _SAFE_KEY.match(key):
            return None
        return self._dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if path is None:
            return None
        try:
            if time.time() - path.stat().st_mtime > self._ttl:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        path = self._path(key)
        if path is None:
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            # Write then rename, so another worker never reads a partial file.
            temporary = path.with_suffix(".partial")
            temporary.write_text(json.dumps(value), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:  # a read-only disk must not break the request
            log.warning("could not cache %s: %s", key, exc)

    def drop(self, key: str) -> None:
        path = self._path(key)
        if path is not None:
            path.unlink(missing_ok=True)


cache = DiskCache()
