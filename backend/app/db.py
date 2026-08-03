"""SQLite persistence for watchlists, monitor runs and co-buy signals.

Watchlists are the one part of DICE that must survive a restart: they exist
precisely so wallets can be re-checked hours and days later. SQLite keeps the
zero-dependency deployment story (stdlib only, one file, WAL mode), and every
function here opens a short-lived connection, so it is safe from any thread or
worker process.

Multi-worker note: uvicorn runs several processes in the deployed setup, each
with its own scheduler loop. ``claim_next_due`` hands a due watchlist to
exactly one of them via an atomic conditional UPDATE — the losers simply see
zero rows changed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Iterable

from .config import settings

# Keep this many finished runs per watchlist; older ones are pruned.
RUN_HISTORY_LIMIT = 50
# Cap on buyer entries stored per signal, so one absurd token cannot bloat a row.
SIGNAL_BUYERS_LIMIT = 200

_SCHEMA_HEAD = """
CREATE TABLE IF NOT EXISTS watchlists (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    name                   TEXT    NOT NULL,
    chain                  TEXT    NOT NULL,
    source_token_address   TEXT,
    notes                  TEXT    NOT NULL DEFAULT '',
    min_wallets            INTEGER NOT NULL DEFAULT 5,
    min_wallets_pct        REAL    NOT NULL DEFAULT 10.0,
    buy_window_hours       INTEGER NOT NULL DEFAULT 48,
    monitor_interval_hours REAL    NOT NULL DEFAULT 24.0,
    min_buy_usd            REAL    NOT NULL DEFAULT 0.0,
    auto_monitor           INTEGER NOT NULL DEFAULT 1,
    ignore_tokens          TEXT    NOT NULL DEFAULT '[]',
    buy_detection          TEXT    NOT NULL DEFAULT 'both',
    realtime               INTEGER NOT NULL DEFAULT 0,
    derived                INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT    NOT NULL,
    last_run_at            TEXT,
    next_run_at            TEXT,
    claimed_until          TEXT
);

CREATE TABLE IF NOT EXISTS watchlist_wallets (
    watchlist_id   INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    wallet_address TEXT    NOT NULL,
    added_at       TEXT    NOT NULL,
    PRIMARY KEY (watchlist_id, wallet_address)
);

CREATE TABLE IF NOT EXISTS monitor_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id      INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    trigger           TEXT    NOT NULL DEFAULT 'manual',
    started_at        TEXT    NOT NULL,
    finished_at       TEXT,
    status            TEXT    NOT NULL DEFAULT 'running',
    error             TEXT,
    window_hours      INTEGER NOT NULL,
    wallets_checked   INTEGER NOT NULL DEFAULT 0,
    wallets_truncated INTEGER NOT NULL DEFAULT 0,
    buy_rows          INTEGER NOT NULL DEFAULT 0,
    tokens_seen       INTEGER NOT NULL DEFAULT 0,
    signals_fired     INTEGER NOT NULL DEFAULT 0,
    execution_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_watchlist
    ON monitor_runs (watchlist_id, id DESC);

CREATE TABLE IF NOT EXISTS dune_query_slots (
    key_hash   TEXT    NOT NULL,
    purpose    TEXT    NOT NULL,
    query_id   INTEGER NOT NULL,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (key_hash, purpose)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One Alchemy Address Activity webhook per network, holding the union of the
-- wallets of every realtime-enabled watchlist on that chain.
CREATE TABLE IF NOT EXISTS alchemy_webhooks (
    chain       TEXT PRIMARY KEY,
    network     TEXT NOT NULL,
    webhook_id  TEXT NOT NULL,
    signing_key TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    synced_at   TEXT,
    address_count INTEGER NOT NULL DEFAULT 0
);

-- Token arrivals pushed by Alchemy. The rolling-window signal check counts
-- distinct wallets in here, so no Dune credit is spent per evaluation.
CREATE TABLE IF NOT EXISTS wallet_events (
    chain          TEXT    NOT NULL,
    wallet_address TEXT    NOT NULL,
    token_address  TEXT    NOT NULL,
    tx_hash        TEXT    NOT NULL,
    token_symbol   TEXT,
    amount         REAL,
    block_num      INTEGER,
    seen_at        TEXT    NOT NULL,
    from_address   TEXT,
    -- 1 when the wallet also sent value out in the same transaction, i.e. it
    -- paid for the token. 0 for a one-way arrival: an airdrop.
    is_buy         INTEGER NOT NULL DEFAULT 0,
    -- 1 when the token *left* this wallet and something else came back in the
    -- same transaction — a sale rather than a transfer. Both flags can be set
    -- on one row: a router can move a token through a wallet in both
    -- directions inside a single transaction.
    is_sell        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chain, wallet_address, token_address, tx_hash)
);

CREATE INDEX IF NOT EXISTS idx_events_window
    ON wallet_events (chain, token_address, seen_at);
CREATE INDEX IF NOT EXISTS idx_events_wallet
    ON wallet_events (chain, wallet_address, seen_at);

-- Every webhook delivery, accepted or not. This is the evidence that the
-- Alchemy side is actually working: without it a silent webhook and a chain
-- where nobody happens to be buying look identical.
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chain          TEXT,
    received_at    TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    activity_count INTEGER NOT NULL DEFAULT 0,
    stored         INTEGER NOT NULL DEFAULT 0,
    signals        INTEGER NOT NULL DEFAULT 0,
    detail         TEXT
);

CREATE INDEX IF NOT EXISTS idx_deliveries_recent
    ON webhook_deliveries (id DESC);

-- Cached DexScreener answers. has_pair = 0 means the token has no liquidity
-- pool anywhere, so it cannot have been bought and must not signal.
CREATE TABLE IF NOT EXISTS signal_briefs (
    signal_id     INTEGER PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
    chain         TEXT    NOT NULL,
    token_address TEXT    NOT NULL,
    theme         TEXT,
    what          TEXT,
    read_note     TEXT,
    risk_note     TEXT,
    body          TEXT    NOT NULL,
    model         TEXT,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS token_risk (
    chain           TEXT    NOT NULL,
    token_address   TEXT    NOT NULL,
    -- The verdict is a nested shape (lists of blockers and warnings), so it
    -- is stored whole rather than spread over columns that would need a
    -- migration every time the risk API grows a field.
    verdict         TEXT    NOT NULL,
    blocked         INTEGER NOT NULL DEFAULT 0,
    checked_at      TEXT    NOT NULL,
    PRIMARY KEY (chain, token_address)
);

CREATE TABLE IF NOT EXISTS token_market (
    chain           TEXT    NOT NULL,
    token_address   TEXT    NOT NULL,
    has_pair        INTEGER NOT NULL DEFAULT 0,
    price_usd       REAL,
    liquidity_usd   REAL    NOT NULL DEFAULT 0,
    volume_24h      REAL    NOT NULL DEFAULT 0,
    fdv             REAL,
    symbol          TEXT,
    name            TEXT,
    pair_url        TEXT,
    pair_created_at TEXT,
    checked_at      TEXT    NOT NULL,
    PRIMARY KEY (chain, token_address)
);

-- What happened after a signal fired. Without this the whole system is
-- unfalsifiable: no way to tell a working threshold from a broken one, or a
-- predictive cohort from noise. The entry price is stamped at fire time and
-- the horizons are filled in later.
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id       INTEGER PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
    chain           TEXT    NOT NULL,
    token_address   TEXT    NOT NULL,
    token_symbol    TEXT,
    fired_at        TEXT    NOT NULL,
    wallet_count    INTEGER NOT NULL DEFAULT 0,
    pool_size       INTEGER NOT NULL DEFAULT 0,
    -- Snapshot of the market at the moment of the signal, so returns are
    -- measured against what was actually knowable then.
    entry_price     REAL,
    entry_liquidity REAL,
    -- Age of the liquidity pool when the signal fired: the honest measure of
    -- how early this system actually is.
    pool_age_hours  REAL,
    price_1h        REAL,
    price_24h       REAL,
    price_7d        REAL,
    checked_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_outcomes_pending
    ON signal_outcomes (fired_at);
"""


#: Deliveries kept for the diagnostics panel; older rows are pruned.
DELIVERY_HISTORY_LIMIT = 100

_SIGNALS_SCHEMA = """
-- Signals are either pooled across every live watchlist (watchlist_id NULL,
-- the default for the live path) or owned by one watchlist (the Dune monitor).
-- Partial unique indexes keep both kinds deduplicated, since SQLite treats
-- NULLs as distinct and a plain UNIQUE would let pooled duplicates through.
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id    INTEGER REFERENCES watchlists(id) ON DELETE CASCADE,
    chain           TEXT    NOT NULL,
    token_address   TEXT    NOT NULL,
    token_symbol    TEXT,
    wallet_count    INTEGER NOT NULL,
    watchlist_size  INTEGER NOT NULL,
    total_usd       REAL,
    buyers          TEXT    NOT NULL DEFAULT '[]',
    breakdown       TEXT    NOT NULL DEFAULT '[]',
    first_seen_at   TEXT    NOT NULL,
    last_updated_at TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'active'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_owned
    ON signals (watchlist_id, token_address) WHERE watchlist_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_pooled
    ON signals (chain, token_address) WHERE watchlist_id IS NULL;
"""

#: The full schema: everything else, plus the signals tables. Kept separable
#: because the pooled-signal migration has to recreate signals on its own.
_SCHEMA = _SCHEMA_HEAD + _SIGNALS_SCHEMA

log = logging.getLogger(__name__)

_init_lock = threading.Lock()
_initialized_paths: set[str] = set()

#: Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` never
#: alters an existing table, so databases created earlier get them here; the
#: "duplicate column" error on ALTER means it is already present.
_MIGRATIONS = (
    "ALTER TABLE watchlists ADD COLUMN buy_detection TEXT NOT NULL DEFAULT 'both'",
    "ALTER TABLE watchlists ADD COLUMN realtime INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE wallet_events ADD COLUMN from_address TEXT",
    # Existing rows predate the airdrop check, so they default to "not a buy":
    # they are exactly the one-way arrivals that made the board unusable.
    "ALTER TABLE wallet_events ADD COLUMN is_buy INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE wallet_events ADD COLUMN is_sell INTEGER NOT NULL DEFAULT 0",
    # Derived cohorts are built *from* the others, so they must be excluded
    # from overlap analysis: being a subset, they would score 100% against
    # everything and crowd out every real finding.
    "ALTER TABLE watchlists ADD COLUMN derived INTEGER NOT NULL DEFAULT 0",
)


def _migrate(conn: sqlite3.Connection) -> None:
    for statement in _MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    _migrate_pooled_signals(conn)


def _migrate_pooled_signals(conn: sqlite3.Connection) -> None:
    """Let a signal belong to the whole pool rather than one watchlist.

    Signals used to be per-watchlist, so ``watchlist_id`` was NOT NULL. A
    pooled signal has no single owner, so the column has to become nullable —
    which SQLite cannot do in place, hence the copy. Existing rows keep their
    watchlist and carry on working.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
    if not columns or "breakdown" in columns:
        return  # fresh database, or already migrated

    conn.execute("ALTER TABLE signals RENAME TO signals_legacy")
    conn.executescript(_SIGNALS_SCHEMA)
    conn.execute(
        """
        INSERT INTO signals (
            id, watchlist_id, chain, token_address, token_symbol, wallet_count,
            watchlist_size, total_usd, buyers, first_seen_at, last_updated_at,
            status
        )
        SELECT id, watchlist_id, chain, token_address, token_symbol,
               wallet_count, watchlist_size, total_usd, buyers, first_seen_at,
               last_updated_at, status
        FROM signals_legacy
        """
    )
    conn.execute("DROP TABLE signals_legacy")
    log.info("migrated signals table to support pooled signals")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso_plus_hours(start_iso: str, hours: float) -> str:
    start = datetime.fromisoformat(start_iso)
    return (start + timedelta(hours=hours)).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Short-lived connection to the configured database; commits and closes.

    ``sqlite3``'s own context manager only wraps a transaction — it never
    closes the handle — so this wrapper exists to guarantee both.
    """
    path = settings.db_path
    if path not in _initialized_paths:
        with _init_lock:
            if path not in _initialized_paths:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                setup = sqlite3.connect(path, timeout=30)
                try:
                    setup.execute("PRAGMA journal_mode=WAL")
                    setup.executescript(_SCHEMA)
                    _migrate(setup)
                    setup.commit()
                finally:
                    setup.close()
                _initialized_paths.add(path)

    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ watchlists


def create_watchlist(
    *,
    name: str,
    chain: str,
    wallets: Iterable[str],
    source_token_address: str | None,
    notes: str,
    min_wallets: int,
    min_wallets_pct: float,
    buy_window_hours: int,
    monitor_interval_hours: float,
    min_buy_usd: float,
    auto_monitor: bool,
    ignore_tokens: list[str],
    buy_detection: str = "both",
    realtime: bool = False,
    derived: bool = False,
) -> int:
    now = utcnow_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO watchlists (
                name, chain, source_token_address, notes,
                min_wallets, min_wallets_pct, buy_window_hours,
                monitor_interval_hours, min_buy_usd, auto_monitor,
                ignore_tokens, buy_detection, realtime, derived, created_at, next_run_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                chain,
                source_token_address,
                notes,
                min_wallets,
                min_wallets_pct,
                buy_window_hours,
                monitor_interval_hours,
                min_buy_usd,
                int(auto_monitor),
                json.dumps(ignore_tokens),
                buy_detection,
                int(realtime),
                int(derived),
                now,
                iso_plus_hours(now, monitor_interval_hours),
            ),
        )
        watchlist_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist_wallets VALUES (?, ?, ?)",
            [(watchlist_id, wallet, now) for wallet in wallets],
        )
    return watchlist_id


def get_watchlist(watchlist_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM watchlists WHERE id = ?", (watchlist_id,)
        ).fetchone()
        return dict(row) if row else None


def get_wallets(watchlist_id: int) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT wallet_address FROM watchlist_wallets "
            "WHERE watchlist_id = ? ORDER BY added_at, wallet_address",
            (watchlist_id,),
        ).fetchall()
        return [row["wallet_address"] for row in rows]


_OVERVIEW_SQL = """
    SELECT
        w.*,
        (SELECT COUNT(*) FROM watchlist_wallets ww
          WHERE ww.watchlist_id = w.id)                    AS wallet_count,
        (SELECT COUNT(*) FROM signals s
          WHERE s.watchlist_id = w.id AND s.status = 'active')
                                                           AS active_signals,
        (SELECT r.status FROM monitor_runs r
          WHERE r.watchlist_id = w.id
          ORDER BY r.id DESC LIMIT 1)                      AS last_run_status,
        (SELECT r.error FROM monitor_runs r
          WHERE r.watchlist_id = w.id
          ORDER BY r.id DESC LIMIT 1)                      AS last_run_error
    FROM watchlists w
"""


def list_watchlists() -> list[dict[str, Any]]:
    """All watchlists plus the derived columns the UI needs in one query."""
    with connect() as conn:
        rows = conn.execute(_OVERVIEW_SQL + " ORDER BY w.id DESC").fetchall()
        return [dict(row) for row in rows]


def get_watchlist_overview(watchlist_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            _OVERVIEW_SQL + " WHERE w.id = ?", (watchlist_id,)
        ).fetchone()
        return dict(row) if row else None


def update_watchlist_fields(watchlist_id: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE watchlists SET {columns} WHERE id = ?",
            (*fields.values(), watchlist_id),
        )


def add_wallets(watchlist_id: int, wallets: Iterable[str]) -> None:
    now = utcnow_iso()
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist_wallets VALUES (?, ?, ?)",
            [(watchlist_id, wallet, now) for wallet in wallets],
        )


def remove_wallets(watchlist_id: int, wallets: Iterable[str]) -> None:
    with connect() as conn:
        conn.executemany(
            "DELETE FROM watchlist_wallets "
            "WHERE watchlist_id = ? AND wallet_address = ?",
            [(watchlist_id, wallet) for wallet in wallets],
        )


def delete_watchlist(watchlist_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM watchlists WHERE id = ?", (watchlist_id,))
        return cursor.rowcount > 0


# ------------------------------------------------------------------- scheduler


def claim_next_due(*, now_iso: str, claim_minutes: int = 30) -> int | None:
    """Atomically claim one due watchlist for this process, or return None.

    A claim is a lease: it expires after ``claim_minutes`` so a worker that
    dies mid-run does not park the watchlist forever.
    """
    claim_until = iso_plus_hours(now_iso, claim_minutes / 60)
    with connect() as conn:
        while True:
            row = conn.execute(
                """
                SELECT id FROM watchlists
                WHERE auto_monitor = 1
                  AND (next_run_at IS NULL OR next_run_at <= ?)
                  AND (claimed_until IS NULL OR claimed_until < ?)
                ORDER BY next_run_at
                LIMIT 1
                """,
                (now_iso, now_iso),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE watchlists SET claimed_until = ?
                WHERE id = ? AND (claimed_until IS NULL OR claimed_until < ?)
                """,
                (claim_until, row["id"], now_iso),
            )
            conn.commit()
            if cursor.rowcount == 1:
                return int(row["id"])
            # Another worker beat us to this one; try the next candidate.


def finish_schedule(watchlist_id: int, *, interval_hours: float) -> None:
    """Record a run happened now and release the scheduler claim."""
    now = utcnow_iso()
    update_watchlist_fields(
        watchlist_id,
        {
            "last_run_at": now,
            "next_run_at": iso_plus_hours(now, interval_hours),
            "claimed_until": None,
        },
    )


# ------------------------------------------------------------------------ runs


def create_run(
    *, watchlist_id: int, trigger: str, window_hours: int
) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO monitor_runs (watchlist_id, trigger, started_at, window_hours)
            VALUES (?, ?, ?, ?)
            """,
            (watchlist_id, trigger, utcnow_iso(), window_hours),
        )
        return int(cursor.lastrowid)


def finish_run(run_id: int, **fields: Any) -> None:
    fields.setdefault("finished_at", utcnow_iso())
    columns = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE monitor_runs SET {columns} WHERE id = ?",
            (*fields.values(), run_id),
        )


def get_run(run_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM monitor_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None


def list_runs(watchlist_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM monitor_runs WHERE watchlist_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (watchlist_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def prune_runs(watchlist_id: int, keep: int = RUN_HISTORY_LIMIT) -> None:
    with connect() as conn:
        conn.execute(
            """
            DELETE FROM monitor_runs
            WHERE watchlist_id = ? AND id NOT IN (
                SELECT id FROM monitor_runs
                WHERE watchlist_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (watchlist_id, watchlist_id, keep),
        )


# --------------------------------------------------------------------- signals


def upsert_signal(
    *,
    watchlist_id: int,
    chain: str,
    token_address: str,
    token_symbol: str | None,
    wallet_count: int,
    watchlist_size: int,
    total_usd: float | None,
    buyers: list[dict[str, Any]],
) -> tuple[int, bool, int]:
    """Insert or refresh a signal; returns (id, created, previous_wallet_count).

    A dismissed signal is refreshed in place but keeps its dismissed status:
    the user said "stop showing me this token" and a re-trigger should respect
    that, while still recording the newest numbers for when they look again.
    """
    now = utcnow_iso()
    buyers_json = json.dumps(buyers[:SIGNAL_BUYERS_LIMIT])
    with connect() as conn:
        existing = conn.execute(
            "SELECT id, wallet_count, token_symbol FROM signals "
            "WHERE watchlist_id = ? AND token_address = ?",
            (watchlist_id, token_address),
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO signals (
                    watchlist_id, chain, token_address, token_symbol,
                    wallet_count, watchlist_size, total_usd, buyers,
                    first_seen_at, last_updated_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    watchlist_id,
                    chain,
                    token_address,
                    token_symbol,
                    wallet_count,
                    watchlist_size,
                    total_usd,
                    buyers_json,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid), True, 0

        conn.execute(
            """
            UPDATE signals SET
                token_symbol = COALESCE(?, token_symbol),
                wallet_count = ?, watchlist_size = ?, total_usd = ?,
                buyers = ?, last_updated_at = ?
            WHERE id = ?
            """,
            (
                token_symbol,
                wallet_count,
                watchlist_size,
                total_usd,
                buyers_json,
                now,
                existing["id"],
            ),
        )
        return int(existing["id"]), False, int(existing["wallet_count"])


def upsert_pool_signal(
    *,
    chain: str,
    token_address: str,
    token_symbol: str | None,
    wallet_count: int,
    pool_size: int,
    total_usd: float | None,
    buyers: list[dict[str, Any]],
    breakdown: list[dict[str, Any]],
) -> tuple[int, bool, int]:
    """Insert or refresh the pooled signal for a token on a chain.

    Mirrors :func:`upsert_signal` but keyed on the chain rather than a
    watchlist, because the pool spans all of them. ``breakdown`` records which
    watchlists the buyers came from, which is the only thing lost by pooling.
    """
    now = utcnow_iso()
    buyers_json = json.dumps(buyers[:SIGNAL_BUYERS_LIMIT])
    breakdown_json = json.dumps(breakdown)
    with connect() as conn:
        existing = conn.execute(
            "SELECT id, wallet_count FROM signals "
            "WHERE watchlist_id IS NULL AND chain = ? AND token_address = ?",
            (chain, token_address),
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO signals (
                    watchlist_id, chain, token_address, token_symbol,
                    wallet_count, watchlist_size, total_usd, buyers, breakdown,
                    first_seen_at, last_updated_at, status
                ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    chain,
                    token_address,
                    token_symbol,
                    wallet_count,
                    pool_size,
                    total_usd,
                    buyers_json,
                    breakdown_json,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid), True, 0

        conn.execute(
            """
            UPDATE signals SET
                token_symbol = COALESCE(?, token_symbol),
                wallet_count = ?, watchlist_size = ?, total_usd = ?,
                buyers = ?, breakdown = ?, last_updated_at = ?
            WHERE id = ?
            """,
            (
                token_symbol,
                wallet_count,
                pool_size,
                total_usd,
                buyers_json,
                breakdown_json,
                now,
                existing["id"],
            ),
        )
        return int(existing["id"]), False, int(existing["wallet_count"])


def wallet_watchlists(chain: str) -> dict[str, list[dict[str, Any]]]:
    """Which live watchlists each wallet belongs to, for signal attribution.

    A wallet can sit in several lists, so this is a list per wallet rather
    than a single owner.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT ww.wallet_address, w.id, w.name
            FROM watchlist_wallets ww
            JOIN watchlists w ON w.id = ww.watchlist_id
            WHERE w.chain = ? AND w.realtime = 1
            """,
            (chain,),
        ).fetchall()
    owners: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        owners.setdefault(row["wallet_address"], []).append(
            {"watchlist_id": row["id"], "name": row["name"]}
        )
    return owners


def list_signals(
    *,
    watchlist_id: int | None = None,
    include_dismissed: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    # LEFT JOIN, not JOIN: a pooled signal has no watchlist and an inner join
    # would silently hide every one of them.
    query = (
        "SELECT s.*, w.name AS watchlist_name FROM signals s "
        "LEFT JOIN watchlists w ON w.id = s.watchlist_id"
    )
    conditions: list[str] = []
    params: list[Any] = []
    if watchlist_id is not None:
        conditions.append("s.watchlist_id = ?")
        params.append(watchlist_id)
    if not include_dismissed:
        conditions.append("s.status = 'active'")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY s.last_updated_at DESC, s.id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_signal(signal_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT s.*, w.name AS watchlist_name FROM signals s "
            "LEFT JOIN watchlists w ON w.id = s.watchlist_id WHERE s.id = ?",
            (signal_id,),
        ).fetchone()
        return dict(row) if row else None


def pool_size(chain: str) -> int:
    """Distinct wallets across every live watchlist on a chain."""
    return len(realtime_wallets(chain))


def set_signal_status(signal_id: int, status: str) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE signals SET status = ?, last_updated_at = ? WHERE id = ?",
            (status, utcnow_iso(), signal_id),
        )
        return cursor.rowcount > 0


# ----------------------------------------------------------- dune query slots
#
# Dune caps how many *private queries* an account may own, and DICE used to
# create a fresh one per execution — after enough runs every request died with
# "Max number of private queries reached". A slot remembers the query DICE
# already created for one (account, purpose) pair so the SQL can be PATCHed
# into it instead. The key is a fingerprint of the API key, never the key.


def get_query_slot(key_hash: str, purpose: str) -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT query_id FROM dune_query_slots "
            "WHERE key_hash = ? AND purpose = ?",
            (key_hash, purpose),
        ).fetchone()
        return int(row["query_id"]) if row else None


def set_query_slot(key_hash: str, purpose: str, query_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO dune_query_slots VALUES (?, ?, ?, ?) "
            "ON CONFLICT (key_hash, purpose) DO UPDATE "
            "SET query_id = excluded.query_id, updated_at = excluded.updated_at",
            (key_hash, purpose, query_id, utcnow_iso()),
        )


def drop_query_slot(key_hash: str, purpose: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM dune_query_slots WHERE key_hash = ? AND purpose = ?",
            (key_hash, purpose),
        )


def drop_purpose_slots(purpose: str) -> None:
    """Forget a purpose across every account (e.g. a deleted watchlist)."""
    with connect() as conn:
        conn.execute("DELETE FROM dune_query_slots WHERE purpose = ?", (purpose,))


def drop_account_slots(key_hash: str) -> int:
    """Forget every slot of one account (after its queries were archived)."""
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM dune_query_slots WHERE key_hash = ?", (key_hash,)
        )
        return cursor.rowcount


# ------------------------------------------------------------------- settings
#
# Small key/value store for operator-editable configuration: the server-side
# Dune API key and the Telegram credentials. The deployment is single-user by
# design (see README), so persisting the key server-side is a deliberate
# trade: it is what lets scheduled monitor runs work without a browser open.


def get_setting(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_settings VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE "
            "SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, utcnow_iso()),
        )


def delete_setting(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))


def server_api_key() -> str | None:
    """The key scheduled runs use: saved via the UI, else the environment."""
    stored = (get_setting("dune_api_key") or "").strip()
    if stored:
        return stored
    return (settings.dune_api_key or "").strip() or None


# ------------------------------------------------------ realtime (Alchemy)


def get_webhook(chain: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM alchemy_webhooks WHERE chain = ?", (chain,)
        ).fetchone()
        return dict(row) if row else None


def list_webhooks() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM alchemy_webhooks ORDER BY chain"
        ).fetchall()
        return [dict(row) for row in rows]


def save_webhook(
    *,
    chain: str,
    network: str,
    webhook_id: str,
    signing_key: str,
    webhook_url: str,
    address_count: int,
) -> None:
    now = utcnow_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO alchemy_webhooks
                (chain, network, webhook_id, signing_key, webhook_url,
                 created_at, synced_at, address_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chain) DO UPDATE SET
                network = excluded.network,
                webhook_id = excluded.webhook_id,
                signing_key = excluded.signing_key,
                webhook_url = excluded.webhook_url,
                synced_at = excluded.synced_at,
                address_count = excluded.address_count
            """,
            (chain, network, webhook_id, signing_key, webhook_url, now, now,
             address_count),
        )


def mark_webhook_synced(chain: str, address_count: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE alchemy_webhooks SET synced_at = ?, address_count = ? "
            "WHERE chain = ?",
            (utcnow_iso(), address_count, chain),
        )


def delete_webhook(chain: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM alchemy_webhooks WHERE chain = ?", (chain,))


def webhook_by_id(webhook_id: str) -> dict[str, Any] | None:
    """Find the registration a delivery belongs to, by Alchemy's webhook id."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM alchemy_webhooks WHERE webhook_id = ?", (webhook_id,)
        ).fetchone()
        return dict(row) if row else None


def realtime_wallets(chain: str) -> set[str]:
    """Union of the wallets of every realtime-enabled watchlist on a chain."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ww.wallet_address
            FROM watchlist_wallets ww
            JOIN watchlists w ON w.id = ww.watchlist_id
            WHERE w.chain = ? AND w.realtime = 1
            """,
            (chain,),
        ).fetchall()
        return {row["wallet_address"] for row in rows}


def realtime_watchlists_for_wallet(chain: str, wallet: str) -> list[dict[str, Any]]:
    """Every realtime watchlist on this chain that contains this wallet."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT w.* FROM watchlists w
            JOIN watchlist_wallets ww ON ww.watchlist_id = w.id
            WHERE w.chain = ? AND w.realtime = 1 AND ww.wallet_address = ?
            """,
            (chain, wallet),
        ).fetchall()
        return [dict(row) for row in rows]


def realtime_chains() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chain FROM watchlists WHERE realtime = 1"
        ).fetchall()
        return [row["chain"] for row in rows]


# --------------------------------------------------------------- wallet events


def record_events(events: Iterable[dict[str, Any]]) -> int:
    """Store token movements, ignoring ones already seen. Returns rows written.

    A webhook can be redelivered — Alchemy retries on a non-2xx — so the
    primary key makes replays free rather than double-counting a buyer.

    The flags are OR-ed rather than overwritten. One transaction can carry the
    same token into and out of a wallet, and a plain INSERT OR IGNORE would
    keep whichever arrived first and silently lose the other half.
    """
    rows = [
        (
            event["chain"],
            event["wallet_address"],
            event["token_address"],
            event["tx_hash"],
            event.get("token_symbol"),
            event.get("amount"),
            event.get("block_num"),
            event.get("seen_at") or utcnow_iso(),
            event.get("from_address"),
            int(bool(event.get("is_buy"))),
            int(bool(event.get("is_sell"))),
        )
        for event in events
    ]
    if not rows:
        return 0
    with connect() as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO wallet_events (
                chain, wallet_address, token_address, tx_hash, token_symbol,
                amount, block_num, seen_at, from_address, is_buy, is_sell
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chain, wallet_address, token_address, tx_hash)
            DO UPDATE SET
                is_buy  = MAX(is_buy,  excluded.is_buy),
                is_sell = MAX(is_sell, excluded.is_sell)
            -- Only when a flag actually gains something. Without this, a
            -- redelivery of an identical payload would rewrite every row and
            -- report them all as newly stored.
            WHERE excluded.is_buy > is_buy OR excluded.is_sell > is_sell
            """,
            rows,
        )
        return conn.total_changes - before


def events_in_window(
    *,
    chain: str,
    token_address: str,
    since_iso: str,
    wallets: Iterable[str],
    only_buys: bool = True,
) -> list[dict[str, Any]]:
    """Per-wallet aggregate of one token's arrivals inside the window.

    ``only_buys`` keeps arrivals the wallet actually paid for. Leave it on for
    anything that fires a signal: an airdrop reaches thousands of addresses at
    once and would otherwise look exactly like coordinated buying.
    """
    wallet_list = list(wallets)
    if not wallet_list:
        return []
    placeholders = ", ".join("?" for _ in wallet_list)
    buys_only = "AND is_buy = 1" if only_buys else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                wallet_address,
                COUNT(*)            AS buy_count,
                MAX(token_symbol)   AS token_symbol,
                MIN(seen_at)        AS first_buy_at,
                MAX(seen_at)        AS last_buy_at,
                -- 1 when this wallet paid for the token at least once, so a
                -- buyer can be told from an airdrop recipient per wallet.
                MAX(is_buy)         AS paid
            FROM wallet_events
            WHERE chain = ? AND token_address = ? AND seen_at >= ?
              AND wallet_address IN ({placeholders})
              {buys_only}
            GROUP BY wallet_address
            ORDER BY first_buy_at
            """,
            (chain, token_address, since_iso, *wallet_list),
        ).fetchall()
        return [dict(row) for row in rows]


def record_delivery(
    *,
    chain: str | None,
    status: str,
    activity_count: int = 0,
    stored: int = 0,
    signals: int = 0,
    detail: str | None = None,
) -> None:
    """Log one webhook delivery and keep the history bounded."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO webhook_deliveries
                (chain, received_at, status, activity_count, stored, signals, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (chain, utcnow_iso(), status, activity_count, stored, signals, detail),
        )
        conn.execute(
            """
            DELETE FROM webhook_deliveries WHERE id NOT IN (
                SELECT id FROM webhook_deliveries ORDER BY id DESC LIMIT ?
            )
            """,
            (DELIVERY_HISTORY_LIMIT,),
        )


def list_deliveries(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM webhook_deliveries ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def token_activity(
    *,
    chain: str,
    wallets: Iterable[str],
    since_iso: str,
    limit: int = 300,
    only_buys: bool = True,
) -> list[dict[str, Any]]:
    """Every token these wallets touched in the window, busiest first.

    This is the accumulation view: it includes tokens *below* the signal
    threshold, which is the whole point — a token four wallets into a
    ten-wallet threshold is invisible everywhere else.
    """
    wallet_list = list(wallets)
    if not wallet_list:
        return []
    placeholders = ", ".join("?" for _ in wallet_list)
    buys_only = "AND is_buy = 1" if only_buys else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                token_address,
                COUNT(DISTINCT wallet_address) AS wallet_count,
                COUNT(*)                       AS buy_count,
                MAX(token_symbol)              AS token_symbol,
                MIN(seen_at)                   AS first_buy_at,
                MAX(seen_at)                   AS last_buy_at,
                SUM(is_buy)                    AS paid_count,
                COUNT(DISTINCT from_address)   AS sender_count
            FROM wallet_events
            WHERE chain = ? AND seen_at >= ?
              AND wallet_address IN ({placeholders})
              {buys_only}
            GROUP BY token_address
            ORDER BY wallet_count DESC, last_buy_at DESC
            LIMIT ?
            """,
            (chain, since_iso, *wallet_list, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def live_watchlists(chain: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM watchlists WHERE realtime = 1"
    params: list[Any] = []
    if chain is not None:
        query += " AND chain = ?"
        params.append(chain)
    with connect() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def get_token_markets(
    *, chain: str, addresses: Iterable[str], fresh_after: str
) -> dict[str, dict[str, Any]]:
    """Cached market data for these tokens, dropping entries older than TTL."""
    wanted = list(addresses)
    if not wanted:
        return {}
    placeholders = ", ".join("?" for _ in wanted)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM token_market
            WHERE chain = ? AND checked_at >= ?
              AND token_address IN ({placeholders})
            """,
            (chain, fresh_after, *wanted),
        ).fetchall()
    return {
        row["token_address"]: {
            "has_pair": bool(row["has_pair"]),
            "price_usd": row["price_usd"],
            "liquidity_usd": row["liquidity_usd"],
            "volume_24h": row["volume_24h"],
            "fdv": row["fdv"],
            "symbol": row["symbol"],
            "name": row["name"],
            "pair_url": row["pair_url"],
            "pair_created_at": row["pair_created_at"],
        }
        for row in rows
    }


def save_token_market(
    *, chain: str, token_address: str, summary: dict[str, Any]
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO token_market (
                chain, token_address, has_pair, price_usd, liquidity_usd,
                volume_24h, fdv, symbol, name, pair_url, pair_created_at,
                checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chain, token_address) DO UPDATE SET
                has_pair = excluded.has_pair,
                price_usd = excluded.price_usd,
                liquidity_usd = excluded.liquidity_usd,
                volume_24h = excluded.volume_24h,
                fdv = excluded.fdv,
                symbol = excluded.symbol,
                name = excluded.name,
                pair_url = excluded.pair_url,
                pair_created_at = excluded.pair_created_at,
                checked_at = excluded.checked_at
            """,
            (
                chain,
                token_address,
                int(bool(summary.get("has_pair"))),
                summary.get("price_usd"),
                summary.get("liquidity_usd") or 0.0,
                summary.get("volume_24h") or 0.0,
                summary.get("fdv"),
                summary.get("symbol"),
                summary.get("name"),
                summary.get("pair_url"),
                summary.get("pair_created_at"),
                utcnow_iso(),
            ),
        )


def prune_events(older_than_iso: str) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM wallet_events WHERE seen_at < ?", (older_than_iso,)
        )
        return cursor.rowcount


def telegram_credentials() -> tuple[str | None, str | None]:
    """(bot_token, chat_id) — UI-saved values win over the environment."""
    token = (get_setting("telegram_bot_token") or "").strip() or (
        settings.telegram_bot_token or ""
    ).strip()
    chat_id = (get_setting("telegram_chat_id") or "").strip() or (
        settings.telegram_chat_id or ""
    ).strip()
    return (token or None, chat_id or None)


# ------------------------------------------------------------------ cohorts
#
# A watchlist is a cohort: a named set of wallets that shared some history.
# Asking which cohorts overlap answers "the wallets that farmed X are now in
# Y" — the question that needs no Dune credit, because both sides are already
# stored here.


def cohort_sizes() -> dict[int, dict[str, Any]]:
    """Every watchlist with its wallet count, keyed by id."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.name, w.chain, w.source_token_address,
                   COUNT(ww.wallet_address) AS wallets
            FROM watchlists w
            LEFT JOIN watchlist_wallets ww ON ww.watchlist_id = w.id
            GROUP BY w.id
            """
        ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def cohort_overlaps() -> list[dict[str, Any]]:
    """Shared wallet counts for every pair of watchlists on the same chain.

    One self-join rather than a query per pair: with N cohorts there are
    N(N-1)/2 pairs, and asking separately would be quadratic round trips for
    what SQLite can group in a single pass. ``b.watchlist_id > a.watchlist_id``
    keeps each pair once and drops self-comparison.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT a.watchlist_id AS a_id,
                   b.watchlist_id AS b_id,
                   COUNT(*)       AS overlap
            FROM watchlist_wallets a
            JOIN watchlist_wallets b
              ON b.wallet_address = a.wallet_address
             AND b.watchlist_id > a.watchlist_id
            JOIN watchlists wa ON wa.id = a.watchlist_id
            JOIN watchlists wb ON wb.id = b.watchlist_id
            WHERE wa.chain = wb.chain
              AND wa.derived = 0 AND wb.derived = 0
            GROUP BY 1, 2
            """
        ).fetchall()
    return [dict(row) for row in rows]


def shared_wallets(a_id: int, b_id: int, limit: int = 500) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT a.wallet_address FROM watchlist_wallets a
            JOIN watchlist_wallets b ON b.wallet_address = a.wallet_address
            WHERE a.watchlist_id = ? AND b.watchlist_id = ?
            ORDER BY a.wallet_address LIMIT ?
            """,
            (a_id, b_id, limit),
        ).fetchall()
    return [row["wallet_address"] for row in rows]


def repeat_wallets(chain: str, min_cohorts: int) -> list[dict[str, Any]]:
    """Wallets that appear in at least ``min_cohorts`` real cohorts on a chain.

    This is the payoff of collecting cohorts: a wallet that was early to one
    token may have been lucky, and one that was early to four was probably
    not. Derived cohorts are excluded from the count, or the set would
    reinforce itself every time it is rebuilt.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT ww.wallet_address, COUNT(DISTINCT w.id) AS cohorts
            FROM watchlist_wallets ww
            JOIN watchlists w ON w.id = ww.watchlist_id
            WHERE w.chain = ? AND w.derived = 0
            GROUP BY ww.wallet_address
            HAVING cohorts >= ?
            ORDER BY cohorts DESC, ww.wallet_address
            """,
            (chain, min_cohorts),
        ).fetchall()
    return [dict(row) for row in rows]


def find_derived(chain: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM watchlists WHERE chain = ? AND derived = 1 "
            "ORDER BY id LIMIT 1",
            (chain,),
        ).fetchone()
        return dict(row) if row else None


def replace_wallets(watchlist_id: int, wallets: Iterable[str]) -> None:
    """Swap a watchlist's membership wholesale, in one transaction.

    Used to refresh a derived cohort: wallets that no longer qualify have to
    leave, not just new ones arrive.
    """
    now = utcnow_iso()
    with connect() as conn:
        conn.execute(
            "DELETE FROM watchlist_wallets WHERE watchlist_id = ?", (watchlist_id,)
        )
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist_wallets VALUES (?, ?, ?)",
            [(watchlist_id, wallet, now) for wallet in wallets],
        )


# ------------------------------------------------------------------ outcomes
#
# The feedback loop. Without it the threshold, the buy window and the cohorts
# are all tuned by feel; with it they are tuned by hit rate.


def record_outcome(
    *,
    signal_id: int,
    chain: str,
    token_address: str,
    token_symbol: str | None,
    wallet_count: int,
    pool_size: int,
    entry_price: float | None,
    entry_liquidity: float | None,
    pool_age_hours: float | None,
) -> None:
    """Stamp the market at the moment a signal fired.

    Only the first fire is recorded: a signal that later gains buyers is the
    same opportunity, and re-stamping it would quietly re-baseline the entry
    price to a level the signal itself may have helped move.
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO signal_outcomes (
                signal_id, chain, token_address, token_symbol, fired_at,
                wallet_count, pool_size, entry_price, entry_liquidity,
                pool_age_hours
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id, chain, token_address, token_symbol, utcnow_iso(),
                wallet_count, pool_size, entry_price, entry_liquidity,
                pool_age_hours,
            ),
        )


def outcomes_due(column: str, older_than_iso: str, limit: int = 50) -> list[dict[str, Any]]:
    """Signals old enough for a horizon that has not been filled in yet."""
    if column not in ("price_1h", "price_24h", "price_7d"):
        raise ValueError(f"unknown horizon column: {column}")
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM signal_outcomes
            WHERE {column} IS NULL AND entry_price IS NOT NULL
              AND fired_at <= ?
            ORDER BY fired_at LIMIT ?
            """,
            (older_than_iso, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def set_outcome_price(signal_id: int, column: str, price: float | None) -> None:
    if column not in ("price_1h", "price_24h", "price_7d"):
        raise ValueError(f"unknown horizon column: {column}")
    with connect() as conn:
        conn.execute(
            f"UPDATE signal_outcomes SET {column} = ?, checked_at = ? "
            "WHERE signal_id = ?",
            (price, utcnow_iso(), signal_id),
        )


def list_outcomes(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signal_outcomes ORDER BY fired_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

# ------------------------------------------------------------ wallet quality
#
# Everything above treats every watched wallet as equal — one wallet, one vote.
# These queries are the raw material for not doing that: what each wallet
# actually bought, which of those became signals, and how far ahead of the
# signal it moved.


def wallet_buy_counts(chain: str) -> dict[str, int]:
    """Distinct tokens each wallet paid for, within the retained event window.

    The denominator of a wallet's hit rate. A wallet that buys two hundred
    tokens a fortnight hits a few signals by volume alone; one that buys five
    and hits three is telling you something.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT wallet_address, COUNT(DISTINCT token_address) AS tokens
            FROM wallet_events
            WHERE chain = ? AND is_buy = 1
            GROUP BY wallet_address
            """,
            (chain,),
        ).fetchall()
    return {row["wallet_address"]: int(row["tokens"]) for row in rows}


def wallet_signal_participation(chain: str) -> list[dict[str, Any]]:
    """One row per (wallet, signal) the wallet bought into before it fired.

    Joined on the token rather than on the signal's stored buyer list, because
    the buyer list is a snapshot of who had bought at fire time while the
    events are the record of who bought at all — including the wallets that
    were early enough to be forgotten by the window.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT e.wallet_address,
                   o.signal_id,
                   o.token_symbol,
                   o.entry_price,
                   o.price_24h,
                   o.pool_age_hours,
                   o.fired_at,
                   MIN(e.seen_at) AS first_buy_at
            FROM wallet_events e
            JOIN signal_outcomes o
              ON o.chain = e.chain AND o.token_address = e.token_address
            WHERE e.chain = ? AND e.is_buy = 1 AND e.seen_at <= o.fired_at
            GROUP BY e.wallet_address, o.signal_id
            """,
            (chain,),
        ).fetchall()
    return [dict(row) for row in rows]


def wallet_cohort_counts(chain: str) -> dict[str, int]:
    """How many real (non-derived) cohorts each wallet belongs to."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT ww.wallet_address, COUNT(DISTINCT w.id) AS cohorts
            FROM watchlist_wallets ww
            JOIN watchlists w ON w.id = ww.watchlist_id
            WHERE w.chain = ? AND w.derived = 0
            GROUP BY ww.wallet_address
            """,
            (chain,),
        ).fetchall()
    return {row["wallet_address"]: int(row["cohorts"]) for row in rows}

def get_token_risks(
    *, chain: str, addresses: Iterable[str], fresh_after: str
) -> dict[str, dict[str, Any]]:
    """Cached risk verdicts, dropping entries older than the TTL.

    The risk API answers one token per request, so without this cache a busy
    board would exhaust its rate limit within a minute.
    """
    wanted = list(addresses)
    if not wanted:
        return {}
    placeholders = ", ".join("?" for _ in wanted)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT token_address, verdict FROM token_risk
            WHERE chain = ? AND checked_at >= ?
              AND token_address IN ({placeholders})
            """,
            (chain, fresh_after, *wanted),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            out[row["token_address"]] = json.loads(row["verdict"])
        except (TypeError, ValueError):  # pragma: no cover - corrupt row
            continue
    return out


def get_token_risk(*, chain: str, token_address: str) -> dict[str, Any] | None:
    """The stored verdict for one token, however old.

    Used where a slightly stale warning beats a silent one — a signal
    notification must carry whatever is known, and the gate has usually just
    refreshed it on the same pass.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT verdict FROM token_risk WHERE chain = ? AND token_address = ?",
            (chain, token_address.lower()),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["verdict"])
    except (TypeError, ValueError):  # pragma: no cover - corrupt row
        return None


def save_token_risk(*, chain: str, token_address: str, verdict: dict[str, Any]) -> None:
    """Store a verdict. Unchecked ones are stored too, but with no TTL value.

    An unchecked verdict is cached deliberately *briefly* by the caller rather
    than not at all: a chain the API does not cover should not be re-asked on
    every sweep, but an outage should not be remembered for hours either.
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO token_risk (chain, token_address, verdict, blocked, checked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (chain, token_address) DO UPDATE SET
                verdict = excluded.verdict,
                blocked = excluded.blocked,
                checked_at = excluded.checked_at
            """,
            (
                chain,
                token_address.lower(),
                json.dumps(verdict),
                1 if verdict.get("blocked") else 0,
                utcnow_iso(),
            ),
        )

def token_exits(
    *, chain: str, token_address: str, wallets: Iterable[str], since_iso: str
) -> dict[str, Any]:
    """How many of the watched wallets have sold this token, and when.

    The counterpart to the buy count a signal is built from. A signal says ten
    wallets bought; this says three of them have since sold — which is the
    difference between a position being built and one being distributed, and
    the buy count alone cannot tell them apart.
    """
    wallet_list = list(wallets)
    if not wallet_list:
        return {"sellers": 0, "last_sell_at": None, "wallets": []}
    placeholders = ", ".join("?" for _ in wallet_list)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT wallet_address, MAX(seen_at) AS last_sell_at
            FROM wallet_events
            WHERE chain = ? AND token_address = ? AND is_sell = 1
              AND seen_at >= ?
              AND wallet_address IN ({placeholders})
            GROUP BY wallet_address
            ORDER BY last_sell_at DESC
            """,
            (chain, token_address.lower(), since_iso, *wallet_list),
        ).fetchall()
    return {
        "sellers": len(rows),
        "last_sell_at": rows[0]["last_sell_at"] if rows else None,
        "wallets": [dict(row) for row in rows],
    }


def exit_counts(
    *, chain: str, tokens: Iterable[str], wallets: Iterable[str], since_iso: str
) -> dict[str, int]:
    """Seller counts for many tokens at once, for the board."""
    token_list = [token.lower() for token in tokens]
    wallet_list = list(wallets)
    if not token_list or not wallet_list:
        return {}
    token_marks = ", ".join("?" for _ in token_list)
    wallet_marks = ", ".join("?" for _ in wallet_list)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT token_address, COUNT(DISTINCT wallet_address) AS sellers
            FROM wallet_events
            WHERE chain = ? AND is_sell = 1 AND seen_at >= ?
              AND token_address IN ({token_marks})
              AND wallet_address IN ({wallet_marks})
            GROUP BY token_address
            """,
            (chain, since_iso, *token_list, *wallet_list),
        ).fetchall()
    return {row["token_address"]: int(row["sellers"]) for row in rows}


def distribution_board(
    *, chain: str, wallets: Iterable[str], since_iso: str, limit: int = 50
) -> list[dict[str, Any]]:
    """What the watched wallets are selling — the mirror of the buy board.

    Worth its own view rather than a column: the wallets leaving a token they
    were early to is a signal in its own right, and it is one the buy-side
    board can never show because nothing is being bought.
    """
    wallet_list = list(wallets)
    if not wallet_list:
        return []
    placeholders = ", ".join("?" for _ in wallet_list)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT token_address,
                   MAX(token_symbol) AS token_symbol,
                   COUNT(DISTINCT wallet_address) AS wallet_count,
                   COUNT(*) AS sell_count,
                   MIN(seen_at) AS first_sell_at,
                   MAX(seen_at) AS last_sell_at
            FROM wallet_events
            WHERE chain = ? AND is_sell = 1 AND seen_at >= ?
              AND wallet_address IN ({placeholders})
            GROUP BY token_address
            ORDER BY wallet_count DESC, last_sell_at DESC
            LIMIT ?
            """,
            (chain, since_iso, *wallet_list, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def save_brief(
    *, signal_id: int, chain: str, token_address: str, brief: dict[str, Any], model: str
) -> None:
    """Store a signal's brief. First one wins, like the outcome stamp.

    A signal that later gains buyers is the same opportunity, and re-briefing
    it would spend money to answer a question already answered.
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO signal_briefs (
                signal_id, chain, token_address, theme, what, read_note,
                risk_note, body, model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id, chain, token_address.lower(), brief.get("theme"),
                brief.get("what"), brief.get("read"), brief.get("risk"),
                brief.get("text") or "", model, utcnow_iso(),
            ),
        )


def get_brief(signal_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM signal_briefs WHERE signal_id = ?", (signal_id,)
        ).fetchone()
    return dict(row) if row else None


def theme_counts() -> list[dict[str, Any]]:
    """How the signalled tokens break down by theme.

    Answers the question a single brief cannot: where the cohorts actually
    have an edge, and where they are arriving late to someone else's trade.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT b.theme,
                   COUNT(*) AS signals,
                   AVG(CASE WHEN o.entry_price > 0 AND o.price_24h IS NOT NULL
                            THEN (o.price_24h - o.entry_price) / o.entry_price * 100
                       END) AS avg_return_24h
            FROM signal_briefs b
            LEFT JOIN signal_outcomes o ON o.signal_id = b.signal_id
            WHERE b.theme IS NOT NULL AND b.theme != ''
            GROUP BY b.theme
            ORDER BY signals DESC
            """
        ).fetchall()
    return [
        {
            "theme": row["theme"],
            "signals": int(row["signals"]),
            "avg_return_24h": (
                round(row["avg_return_24h"], 1)
                if row["avg_return_24h"] is not None else None
            ),
        }
        for row in rows
    ]
