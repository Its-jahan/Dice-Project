"""Runtime configuration for the DICE backend.

The Dune API key is deliberately *not* required here: the primary way to supply
it is per-request, from the browser (see ``app.main.resolve_api_key``).  The
environment variable only acts as a fallback for headless/CLI usage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """Process-wide settings, read once at import time.

    Not frozen: deployments (and tests) occasionally override a field in place
    rather than round-tripping through the environment.
    """

    # Optional server-side fallback key. Leave unset in shared deployments so
    # every user brings their own key from the UI.
    dune_api_key: str | None = field(
        default_factory=lambda: os.getenv("DUNE_API_KEY") or None
    )
    dune_api_base: str = field(
        default_factory=lambda: os.getenv("DUNE_API_BASE", "https://api.dune.com/api/v1")
    )
    # A pre-saved parametrised Dune query. When set, DICE executes it instead of
    # creating an ad-hoc query (ad-hoc creation needs a Plus plan).
    dune_query_id: int | None = field(
        default_factory=lambda: _env_int("DUNE_QUERY_ID", 0) or None
    )
    # Seconds to wait for an execution to finish before giving up.
    execution_timeout: int = field(
        default_factory=lambda: _env_int("DICE_EXECUTION_TIMEOUT", 900)
    )
    poll_interval: float = field(
        default_factory=lambda: float(os.getenv("DICE_POLL_INTERVAL", "2.0"))
    )
    # Hard cap on rows pulled back from Dune, to keep exports sane.
    max_rows: int = field(default_factory=lambda: _env_int("DICE_MAX_ROWS", 1_000_000))


settings = Settings()
