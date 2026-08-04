"""Addresses that are never a trader must never reach a cohort or a count."""

import pytest

from app import db, infra
from app.config import settings
from app.models import Chain, WatchlistCreate

POOL_MANAGER = "0x000000000004444c5dc75cb358380d2e3de08a90"
BINANCE = "0x28c6c06298d514db089934071355e5743bf21d60"
#: How a block explorer hands it to you — the 0x stays lowercase.
BINANCE_CHECKSUMMED = "0x" + BINANCE[2:].upper()
REAL = ["0x" + c * 40 for c in "abcdef"]


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    with db.connect():
        pass
    return db


def test_a_router_never_enters_a_watchlist():
    created = WatchlistCreate(
        name="cohort", chain=Chain.ethereum,
        wallets=[*REAL, POOL_MANAGER, BINANCE_CHECKSUMMED],
    )
    assert POOL_MANAGER not in created.wallets
    assert BINANCE not in created.wallets
    assert len(created.wallets) == len(REAL)


def test_case_does_not_let_one_through():
    """Addresses arrive checksummed from block explorers."""
    assert infra.is_never_watched(BINANCE_CHECKSUMMED)
    assert infra.drop([BINANCE_CHECKSUMMED]) == []


def test_an_existing_cohort_stops_contributing_one(store):
    """The repair for cohorts built before the filter existed.

    Everything counts against realtime_wallets: the pool size, the threshold
    a signal must clear, and the buyers credited to it. Filtering there fixes
    stored cohorts without rewriting anyone's data.
    """
    watchlist_id = db.create_watchlist(
        name="legacy", chain="ethereum", wallets=[],
        source_token_address=None, notes="", min_wallets=3, min_wallets_pct=0.0,
        buy_window_hours=48, monitor_interval_hours=24.0, min_buy_usd=0.0,
        auto_monitor=False, ignore_tokens=[], realtime=True,
    )
    # Written straight to the table, as a pre-filter cohort would have been.
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO watchlist_wallets (watchlist_id, wallet_address, added_at)"
            " VALUES (?, ?, datetime('now'))",
            [(watchlist_id, a) for a in [*REAL, POOL_MANAGER, BINANCE]],
        )

    live = db.realtime_wallets("ethereum")
    assert POOL_MANAGER not in live
    assert BINANCE not in live
    assert len(live) == len(REAL)


def test_the_reason_a_cohort_shrank_can_be_reported():
    found = infra.found_in([*REAL, POOL_MANAGER, BINANCE])
    assert found == {
        POOL_MANAGER: "Uniswap v4 PoolManager",
        BINANCE: "Binance hot wallet",
    }


def test_ordinary_wallets_are_untouched():
    assert infra.drop(REAL) == REAL
    assert infra.found_in(REAL) == {}
    assert infra.label(REAL[0]) is None
