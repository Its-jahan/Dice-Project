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


def _env_float_or_none(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


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
    # A token needs at least this much pooled liquidity before it can signal.
    # 0 still requires a pool to exist — which is what excludes spam that was
    # never tradeable at all.
    min_liquidity_usd: float = field(
        default_factory=lambda: float(os.getenv("DICE_MIN_LIQUIDITY_USD", "1000"))
    )
    # Live signals are pooled across every live watchlist: a token fires when
    # this share of *all* tracked wallets bought it. Editable in the UI, which
    # takes precedence over these.
    pool_pct: float = field(
        default_factory=lambda: float(os.getenv("DICE_POOL_PCT", "10"))
    )
    pool_min_wallets: int = field(
        default_factory=lambda: _env_int("DICE_POOL_MIN_WALLETS", 3)
    )
    # Whether wallets that were *handed* a token count towards a signal. Safe
    # to leave on because the liquidity gate already drops tokens with no
    # pool, which is what spam airdrops are.
    signal_airdrops: bool = field(
        default_factory=lambda: _env_bool("DICE_SIGNAL_AIRDROPS", True)
    )
    # Screen the contract before a signal goes out. Only unsellability blocks;
    # taxes, open mint authority and unlocked liquidity are attached as
    # warnings, because they make a trade unwise rather than impossible.
    # None means no limit. Set it and pooled signals ignore tokens whose
    # liquidity pool is older — see realtime.max_pool_age_hours().
    max_pool_age_hours: float | None = field(
        default_factory=lambda: _env_float_or_none("DICE_MAX_POOL_AGE_HOURS")
    )
    risk_screening: bool = field(
        default_factory=lambda: _env_bool("DICE_RISK_SCREENING", True)
    )
    # An LLM brief attached to each new signal: what the token actually is,
    # researched with web search. Off unless a key is saved — it costs money
    # per signal and the system works without it.
    # Either key works. OpenRouter is a gateway to the same Claude models at
    # the same list price, and it is the reachable option in a lot of the
    # world; a direct Anthropic key is used when that is what is saved.
    openrouter_api_key: str | None = field(
        default_factory=lambda: os.getenv("OPENROUTER_API_KEY") or None
    )
    anthropic_api_key: str | None = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY") or None
    )
    ai_enrichment: bool = field(
        default_factory=lambda: _env_bool("DICE_AI_ENRICHMENT", True)
    )


settings = Settings()
