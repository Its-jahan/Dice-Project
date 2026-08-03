"""Signal outcome tracking: was the signal right, and how early was it?"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, dexscreener, main, performance, realtime
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore
from app.models import Chain

SIGNING_KEY = "whsec-perf"
WEBHOOK_ID = "wh_perf"
WALLETS = [f"0x{i:040x}" for i in range(1, 11)]
GEM = "0x" + "9" * 40


def _market(price=0.01, *, created_at=None, liquidity=50_000.0):
    async def market_data(chain, addresses, refresh=False):
        return {
            str(address).lower(): {
                "has_pair": True,
                "price_usd": price,
                "liquidity_usd": liquidity,
                "volume_24h": 10_000.0,
                "fdv": None,
                "symbol": "GEM",
                "name": None,
                "pair_url": None,
                "pair_created_at": created_at,
            }
            for address in addresses
        }

    return market_data


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(dexscreener, "market_data", _market())
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "live_sweep_seconds", 3600)
    monkeypatch.setattr(settings, "pool_pct", 50.0)
    monkeypatch.setattr(settings, "pool_min_wallets", 3)
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", None, raising=False)
    monkeypatch.setattr(settings, "public_base_url", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))
    with TestClient(main.app) as test_client:
        yield test_client


def _live_watchlist(client):
    created = client.post(
        "/api/watchlists",
        json={
            "name": "early buyers",
            "chain": "ethereum",
            "wallets": WALLETS,
            "min_wallets": 5,
            "min_wallets_pct": 0,
            "buy_window_hours": 48,
        },
    ).json()
    db.update_watchlist_fields(created["id"], {"realtime": 1})
    db.save_webhook(
        chain="ethereum",
        network="ETH_MAINNET",
        webhook_id=WEBHOOK_ID,
        signing_key=SIGNING_KEY,
        webhook_url="https://dice.example/api/webhooks/alchemy",
        address_count=len(WALLETS),
    )
    return created["id"]


def _buy(client, wallet, token=GEM):
    tx = f"0x{wallet[-6:]}{token[-4:]}"
    payload = {
        "webhookId": WEBHOOK_ID,
        "type": "ADDRESS_ACTIVITY",
        "event": {
            "network": "ETH_MAINNET",
            "activity": [
                {
                    "fromAddress": "0x" + "1" * 40,
                    "toAddress": wallet,
                    "hash": tx,
                    "blockNum": "0x1",
                    "value": 100.0,
                    "asset": "GEM",
                    "category": "erc20",
                    "rawContract": {"address": token},
                },
                {
                    "fromAddress": wallet,
                    "toAddress": "0x" + "d" * 40,
                    "hash": tx,
                    "blockNum": "0x1",
                    "value": 0.4,
                    "asset": "ETH",
                    "category": "external",
                    "rawContract": {"address": None},
                },
            ],
        },
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(SIGNING_KEY.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/webhooks/alchemy",
        content=body,
        headers={"X-Alchemy-Signature": signature, "Content-Type": "application/json"},
    )


def _age(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# --------------------------------------------------------------- recording


def test_firing_a_signal_stamps_the_entry_market(client, monkeypatch):
    monkeypatch.setattr(
        dexscreener, "market_data", _market(price=0.02, created_at=_age(6))
    )
    _live_watchlist(client)
    for wallet in WALLETS[:5]:
        _buy(client, wallet)

    assert client.get("/api/signals").json()  # sanity: it fired

    outcomes = db.list_outcomes()
    assert len(outcomes) == 1
    stamped = outcomes[0]
    assert stamped["entry_price"] == 0.02
    assert stamped["entry_liquidity"] == 50_000.0
    # The pool was six hours old when the signal fired — that is how early
    # this system actually was, not how early it felt.
    assert 5.5 <= stamped["pool_age_hours"] <= 6.5
    assert stamped["wallet_count"] == 5


def test_entry_price_is_not_rebaselined_by_later_buyers(client, monkeypatch):
    """The first fire is the entry; a signal that grows is the same opportunity.

    Without this, a signal that keeps attracting buyers while the price runs
    would keep resetting its own entry and always look like it went nowhere.
    """
    monkeypatch.setattr(dexscreener, "market_data", _market(price=0.02))
    _live_watchlist(client)
    for wallet in WALLETS[:5]:
        _buy(client, wallet)

    monkeypatch.setattr(dexscreener, "market_data", _market(price=0.09))
    for wallet in WALLETS[5:8]:
        _buy(client, wallet)

    outcomes = db.list_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0]["entry_price"] == 0.02


# ---------------------------------------------------------------- horizons


@pytest.mark.anyio
async def test_horizons_fill_only_once_they_come_due(client, monkeypatch):
    monkeypatch.setattr(dexscreener, "market_data", _market(price=0.02))
    _live_watchlist(client)
    for wallet in WALLETS[:5]:
        _buy(client, wallet)
    signal_id = db.list_outcomes()[0]["signal_id"]

    # Nothing is measurable a moment after firing.
    monkeypatch.setattr(dexscreener, "market_data", _market(price=0.05))
    assert await performance.fill_horizons() == {}

    # Age the signal past the 1 h mark and it becomes measurable.
    with db.connect() as conn:
        conn.execute(
            "UPDATE signal_outcomes SET fired_at = ? WHERE signal_id = ?",
            (_age(2), signal_id),
        )
    assert await performance.fill_horizons() == {"price_1h": 1}
    assert db.list_outcomes()[0]["price_1h"] == 0.05
    # The 24 h horizon is still in the future and must stay empty.
    assert db.list_outcomes()[0]["price_24h"] is None


@pytest.mark.anyio
async def test_a_dead_pool_is_recorded_as_a_total_loss(client, monkeypatch):
    """A token that can no longer be sold is a -100%, not a missing datapoint."""
    monkeypatch.setattr(dexscreener, "market_data", _market(price=0.02))
    _live_watchlist(client)
    for wallet in WALLETS[:5]:
        _buy(client, wallet)
    signal_id = db.list_outcomes()[0]["signal_id"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE signal_outcomes SET fired_at = ? WHERE signal_id = ?",
            (_age(2), signal_id),
        )

    async def rugged(chain, addresses, refresh=False):
        return {
            str(address).lower(): {"has_pair": False, "price_usd": None}
            for address in addresses
        }

    monkeypatch.setattr(dexscreener, "market_data", rugged)
    await performance.fill_horizons()

    assert db.list_outcomes()[0]["price_1h"] == 0.0
    summary = performance.summarise()
    hour = next(h for h in summary["horizons"] if h["horizon"] == "1h")
    assert hour["median_return"] == -100.0
    assert hour["win_rate"] == 0.0


@pytest.mark.anyio
async def test_a_failed_lookup_leaves_the_horizon_pending(client, monkeypatch):
    """An API outage must not be recorded as a price of zero."""
    monkeypatch.setattr(dexscreener, "market_data", _market(price=0.02))
    _live_watchlist(client)
    for wallet in WALLETS[:5]:
        _buy(client, wallet)
    signal_id = db.list_outcomes()[0]["signal_id"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE signal_outcomes SET fired_at = ? WHERE signal_id = ?",
            (_age(2), signal_id),
        )

    async def broken(chain, addresses, refresh=False):
        raise RuntimeError("dexscreener is down")

    monkeypatch.setattr(dexscreener, "market_data", broken)
    assert await performance.fill_horizons() == {}
    assert db.list_outcomes()[0]["price_1h"] is None


# ----------------------------------------------------------------- summary


def test_summary_uses_the_median_not_the_mean():
    """One 40x must not make four losses look like a winning strategy."""
    rows = [
        {"entry_price": 1.0, "price_24h": 0.5, "pool_age_hours": 4.0},
        {"entry_price": 1.0, "price_24h": 0.6, "pool_age_hours": 8.0},
        {"entry_price": 1.0, "price_24h": 0.8, "pool_age_hours": 2.0},
        {"entry_price": 1.0, "price_24h": 0.9, "pool_age_hours": 6.0},
        {"entry_price": 1.0, "price_24h": 40.0, "pool_age_hours": 10.0},
    ]
    summary = performance.summarise(rows)
    day = next(h for h in summary["horizons"] if h["horizon"] == "24h")

    assert day["measured"] == 5
    assert day["win_rate"] == 20.0          # one winner in five
    assert day["median_return"] == -20.0    # the mean would read +760%
    assert day["best"] == 3900.0
    assert summary["median_pool_age_hours"] == 6.0


def test_summary_reports_nothing_rather_than_zero_when_unmeasured():
    summary = performance.summarise([])
    assert summary["signals"] == 0
    for horizon in summary["horizons"]:
        # An unmeasured win rate is unknown, and 0% would read as "always wrong".
        assert horizon["win_rate"] is None
        assert horizon["median_return"] is None


# ------------------------------------------------------------------- pool age


def test_pool_age_is_none_when_dexscreener_omits_it():
    assert performance.pool_age_hours(None) is None
    assert performance.pool_age_hours("not-a-date") is None
    assert 23.5 <= performance.pool_age_hours(_age(24)) <= 24.5


@pytest.mark.anyio
async def test_board_filters_on_pool_age_but_keeps_unknown_ages(client, monkeypatch):
    monkeypatch.setattr(
        dexscreener, "market_data", _market(created_at=_age(72))
    )
    _live_watchlist(client)
    for wallet in WALLETS[:3]:
        _buy(client, wallet)

    board = client.get("/api/live/tokens").json()["tokens"]
    assert board[0]["pool_age_hours"] > 71

    # A three-day-old pool is not an early entry; the filter says so.
    assert client.get("/api/live/tokens?max_pool_age_hours=24").json()["tokens"] == []

    # Missing metadata is not evidence of an old pool, so it survives.
    monkeypatch.setattr(dexscreener, "market_data", _market(created_at=None))
    kept = client.get("/api/live/tokens?max_pool_age_hours=24").json()["tokens"]
    assert len(kept) == 1
    assert kept[0]["pool_age_hours"] is None


# --------------------------------------------------------------------- API


def test_performance_endpoint_reports_pending_work(client, monkeypatch):
    monkeypatch.setattr(dexscreener, "market_data", _market(price=0.02))
    _live_watchlist(client)
    for wallet in WALLETS[:5]:
        _buy(client, wallet)

    body = client.get("/api/signals/performance").json()
    assert body["signals"] == 1
    assert body["pending"] == 1          # the 24 h mark has not arrived
    assert body["recent"][0]["entry_price"] == 0.02
