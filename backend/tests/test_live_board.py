"""Accumulation board and the sweep that catches signals events would miss."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import db, main, realtime
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore

SIGNING_KEY = "whsec-live"
WEBHOOK_ID = "wh_live"
WALLETS = [f"0x{i:040x}" for i in range(1, 11)]
GEM = "0x" + "9" * 40
OTHER = "0x" + "7" * 40
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "live_sweep_seconds", 3600)  # no background run
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", None, raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", None, raising=False)
    monkeypatch.setattr(settings, "public_base_url", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))
    with TestClient(main.app) as test_client:
        yield test_client


def _live_watchlist(client, *, min_wallets=5, wallets=None):
    created = client.post(
        "/api/watchlists",
        json={
            "name": "early buyers",
            "chain": "ethereum",
            "wallets": wallets or WALLETS,
            "min_wallets": min_wallets,
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


def _buy(client, wallet, token=GEM, tx=None):
    """Deliver a genuine purchase: the token in, and payment out, one tx."""
    tx = tx or f"0x{wallet[-6:]}{token[-4:]}"
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
                    "asset": "GEM" if token == GEM else "OTHER",
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
        headers={
            "X-Alchemy-Signature": signature,
            "Content-Type": "application/json",
        },
    )


# ------------------------------------------------------------------- board


def test_board_shows_tokens_below_the_threshold(client):
    _live_watchlist(client, min_wallets=5)
    for wallet in WALLETS[:3]:
        _buy(client, wallet)

    assert client.get("/api/signals").json() == []  # not enough buyers yet

    board = client.get("/api/live/tokens").json()["tokens"]

    # The build-up is visible even though no signal exists.
    assert len(board) == 1
    row = board[0]
    assert row["token_address"] == GEM
    assert row["wallet_count"] == 3
    assert row["required"] == 5
    assert row["signal_status"] is None
    assert row["window_hours"] == 48


def test_board_ranks_by_closeness_to_firing(client):
    _live_watchlist(client, min_wallets=5)
    for wallet in WALLETS[:2]:
        _buy(client, wallet, token=OTHER)
    for wallet in WALLETS[:4]:
        _buy(client, wallet, token=GEM)

    board = client.get("/api/live/tokens").json()["tokens"]

    assert [row["token_address"] for row in board] == [GEM, OTHER]
    assert board[0]["wallet_count"] == 4
    assert board[1]["wallet_count"] == 2


def test_board_marks_tokens_that_already_signalled(client):
    _live_watchlist(client, min_wallets=3)
    for wallet in WALLETS[:3]:
        _buy(client, wallet)

    board = client.get("/api/live/tokens").json()["tokens"]

    assert board[0]["signal_status"] == "active"


def test_board_hides_ignored_tokens(client):
    watchlist_id = _live_watchlist(client, min_wallets=5)
    for wallet in WALLETS[:3]:
        _buy(client, wallet, token=USDC)      # built-in stoplist
    for wallet in WALLETS[:2]:
        _buy(client, wallet, token=OTHER)
    client.patch(
        f"/api/watchlists/{watchlist_id}", json={"ignore_tokens": [OTHER]}
    )

    board = client.get("/api/live/tokens").json()["tokens"]

    assert board == []


def _airdrop(client, wallet, token=GEM, tx=None):
    """A one-way arrival: the wallet receives and pays nothing."""
    payload = {
        "webhookId": WEBHOOK_ID,
        "type": "ADDRESS_ACTIVITY",
        "event": {
            "network": "ETH_MAINNET",
            "activity": [
                {
                    "fromAddress": "0x" + "5" * 40,   # one spammer, many wallets
                    "toAddress": wallet,
                    "hash": tx or f"0xdrop{wallet[-6:]}",
                    "blockNum": "0x1",
                    "value": 1000000.0,
                    "asset": None,
                    "category": "erc20",
                    "rawContract": {"address": token},
                }
            ],
        },
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(SIGNING_KEY.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/webhooks/alchemy",
        content=body,
        headers={
            "X-Alchemy-Signature": signature,
            "Content-Type": "application/json",
        },
    )


def test_an_airdrop_to_many_wallets_never_reaches_the_board_or_a_signal(client):
    """The failure that made the board unusable: spam blasted at every wallet.

    Ten of ten watched wallets "receiving" the same token looks like maximal
    conviction and is in fact one spammer paying gas.
    """
    _live_watchlist(client, min_wallets=3)

    for wallet in WALLETS:
        _airdrop(client, wallet)

    assert client.get("/api/live/tokens").json()["tokens"] == []
    assert client.get("/api/signals").json() == []
    assert client.post("/api/live/sweep").json()["signals"] == 0

    # The events are still stored, so the filtered view can explain itself.
    shown = client.get("/api/live/tokens?include_airdrops=true").json()["tokens"]
    assert len(shown) == 1
    assert shown[0]["wallet_count"] == 10
    assert shown[0]["paid_count"] == 0
    assert shown[0]["sender_count"] == 1   # one spammer behind all of them


def test_a_paid_buy_survives_alongside_airdrops(client):
    _live_watchlist(client, min_wallets=3)
    for wallet in WALLETS:
        _airdrop(client, wallet, token=OTHER)
    for wallet in WALLETS[:3]:
        _buy(client, wallet, token=GEM)

    board = client.get("/api/live/tokens").json()["tokens"]

    assert [row["token_address"] for row in board] == [GEM]
    assert board[0]["wallet_count"] == 3
    assert len(client.get("/api/signals").json()) == 1


def test_board_only_covers_live_watchlists(client):
    watchlist_id = _live_watchlist(client, min_wallets=5)
    for wallet in WALLETS[:3]:
        _buy(client, wallet)
    assert client.get("/api/live/tokens").json()["tokens"]

    db.update_watchlist_fields(watchlist_id, {"realtime": 0})

    assert client.get("/api/live/tokens").json()["tokens"] == []


# ------------------------------------------------------------------- sweep


def test_sweep_fires_a_signal_the_event_path_could_not(client):
    """Lowering the threshold must not need a new buy to take effect."""
    watchlist_id = _live_watchlist(client, min_wallets=5)
    for wallet in WALLETS[:4]:
        _buy(client, wallet)
    assert client.get("/api/signals").json() == []

    # Four buyers already qualify at the new threshold, but no further event
    # will ever arrive to trigger the check.
    client.patch(f"/api/watchlists/{watchlist_id}", json={"min_wallets": 4})
    assert client.get("/api/signals").json() == []

    result = client.post("/api/live/sweep").json()

    assert result["signals"] == 1
    signals = client.get("/api/signals").json()
    assert len(signals) == 1
    assert signals[0]["wallet_count"] == 4


def test_sweep_fires_when_wallets_are_added_to_the_list_later(client):
    # A wallet that bought before it joined the watchlist still counts once
    # it is a member — its event is already in the store.
    watchlist_id = _live_watchlist(client, min_wallets=3, wallets=WALLETS[:3])
    for wallet in WALLETS[:2]:
        _buy(client, wallet)
    assert client.get("/api/signals").json() == []

    # WALLETS[3] bought too, but was not being watched at the time; it is
    # registered with Alchemy as part of another list on the same chain.
    db.record_events(
        [
            {
                "chain": "ethereum",
                "wallet_address": WALLETS[3],
                "token_address": GEM,
                "tx_hash": "0xlate",
                "token_symbol": "GEM",
                "amount": 5.0,
                "is_buy": True,
            }
        ]
    )
    client.patch(
        f"/api/watchlists/{watchlist_id}", json={"add_wallets": [WALLETS[3]]}
    )

    assert client.post("/api/live/sweep").json()["signals"] == 1
    assert client.get("/api/signals").json()[0]["wallet_count"] == 3


def test_sweep_is_idempotent_for_an_unchanged_signal(client):
    _live_watchlist(client, min_wallets=3)
    for wallet in WALLETS[:3]:
        _buy(client, wallet)
    assert len(client.get("/api/signals").json()) == 1

    # Nothing changed, so a re-check must not re-announce.
    first = client.post("/api/live/sweep").json()
    second = client.post("/api/live/sweep").json()

    assert first["signals"] == 0
    assert second["signals"] == 0
    assert len(client.get("/api/signals").json()) == 1


def test_sweep_reports_a_strengthened_signal_once(client):
    _live_watchlist(client, min_wallets=3)
    for wallet in WALLETS[:3]:
        _buy(client, wallet)

    db.record_events(
        [
            {
                "chain": "ethereum",
                "wallet_address": WALLETS[4],
                "token_address": GEM,
                "tx_hash": "0xextra",
                "token_symbol": "GEM",
                "is_buy": True,
            }
        ]
    )

    assert client.post("/api/live/sweep").json()["signals"] == 1   # buyer count grew
    assert client.post("/api/live/sweep").json()["signals"] == 0   # already reported
    assert client.get("/api/signals").json()[0]["wallet_count"] == 4


def test_sweep_ignores_tokens_below_threshold(client):
    _live_watchlist(client, min_wallets=5)
    for wallet in WALLETS[:4]:
        _buy(client, wallet)

    result = client.post("/api/live/sweep").json()

    assert result["checked"] == 0
    assert result["signals"] == 0
    assert client.get("/api/signals").json() == []


def test_sweep_with_nothing_live_is_a_no_op(client):
    assert client.post("/api/live/sweep").json() == {"checked": 0, "signals": 0}
