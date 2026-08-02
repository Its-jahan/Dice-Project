"""Runtime configuration for the DICE backend.

The Dune API key is deliberately *not* required here: the primary way to supply
it is per-request, from the browser (see ``app.main.resolve_api_key``).  The
environment variable only acts as a fallback for headless/CLI usage — and it is
what the background watchlist monitor uses, since scheduled runs happen with no
browser attached.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _default_db_path() -> str:
    # backend/data/dice.db next to the app package; overridable for deployments
    # where the code directory is read-only (see deploy/dice.service).
    return str(Path(__file__).resolve().parents[1] / "data" / "dice.db")


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

    # ------------------------------------------------- watchlist monitoring

    # SQLite file holding watchlists, monitor runs and signals.
    db_path: str = field(
        default_factory=lambda: os.getenv("DICE_DB_PATH") or _default_db_path()
    )
    # Master switch for the background scheduler. Manual "run now" from the UI
    # works regardless; only *scheduled* runs need this (plus a server key).
    monitor_enabled: bool = field(
        default_factory=lambda: _env_bool("DICE_MONITOR_ENABLED", True)
    )
    # How often the scheduler wakes up to look for due watchlists.
    monitor_tick_seconds: int = field(
        default_factory=lambda: _env_int("DICE_MONITOR_TICK_SECONDS", 60)
    )
    # Cap on wallets per watchlist: every address is interpolated into the
    # trades query, so an unbounded list would produce megabytes of SQL.
    monitor_max_wallets: int = field(
        default_factory=lambda: _env_int("DICE_MONITOR_MAX_WALLETS", 2000)
    )
    # Optional Telegram push for new/strengthened signals.
    telegram_bot_token: str | None = field(
        default_factory=lambda: os.getenv("DICE_TELEGRAM_BOT_TOKEN") or None
    )
    telegram_chat_id: str | None = field(
        default_factory=lambda: os.getenv("DICE_TELEGRAM_CHAT_ID") or None
    )
    # Public HTTPS origin of this deployment. Alchemy delivers webhooks here,
    # so it must be reachable from the internet. Normally set in the UI; this
    # is the fallback.
    public_base_url: str | None = field(
        default_factory=lambda: os.getenv("DICE_PUBLIC_URL") or None
    )
    # How often the live sweep re-checks stored events against each live
    # watchlist's threshold. Reads only local SQLite, so this is cheap.
    live_sweep_seconds: int = field(
        default_factory=lambda: _env_int("DICE_LIVE_SWEEP_SECONDS", 300)
    )


settings = Settings()
