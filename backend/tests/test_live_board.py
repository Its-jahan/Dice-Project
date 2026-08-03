"""Accumulation board and the sweep that catches signals events would miss."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import db, main, realtime
from app import dexscreener
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore

SIGNING_KEY = "whsec-live"
WEBHOOK_ID = "wh_live"
WALLETS = [f"0x{i:040x}" for i in range(1, 11)]
GEM = "0x" + "9" * 40
OTHER = "0x" + "7" * 40
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _fake_market(**_kwargs):
    """Stub DexScreener: every token in a test is tradeable unless said otherwise."""

    async def market_data(chain, addresses, refresh=False):
        return {
            str(address).lower(): {
                "has_pair": True,
                "price_usd": 0.01,
                "liquidity_usd": 50_000.0,
                "volume_24h": 10_000.0,
                "fdv": None,
                "symbol": None,
                "name": None,
                "pair_url": None,
                "pair_created_at": None,
            }
            for address in addresses
        }

    return market_data


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(dexscreener, "market_data", _fake_market())
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "live_sweep_seconds", 3600)  # no background run
    # Live signals are pooled: the threshold is a share of every live
    # wallet, not a per-watchlist count. 50% of the 10-wallet pool = 5.
    monkeypatch.setattr(settings, "pool_pct", 50.0)
    monkeypatch.setattr(settings, "pool_min_wallets", 3)
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
    _live_watchlist(client)
    for wallet in WALLETS[:5]:     # 50% of the pool
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
    _live_watchlist(client)
    for wallet in WALLETS:
        _airdrop(client, wallet, token=OTHER)
    for wallet in WALLETS[:5]:
        _buy(client, wallet, token=GEM)

    board = client.get("/api/live/tokens").json()["tokens"]

    assert [row["token_address"] for row in board] == [GEM]
    assert board[0]["wallet_count"] == 5
    assert len(client.get("/api/signals").json()) == 1


def _market_without(*poolless, liquidity=50_000.0):
    """DexScreener stub where the named tokens have no liquidity pool."""
    missing = {t.lower() for t in poolless}

    async def market_data(chain, addresses, refresh=False):
        return {
            str(a).lower(): {
                "has_pair": str(a).lower() not in missing,
                "price_usd": 0.01,
                "liquidity_usd": 0.0 if str(a).lower() in missing else liquidity,
                "volume_24h": 10_000.0,
                "fdv": None,
                "symbol": "REAL",
                "name": None,
                "pair_url": None,
                "pair_created_at": None,
            }
            for a in addresses
        }

    return market_data


def test_a_token_with_no_liquidity_pool_never_signals(client, monkeypatch):
    """The tokens that were 'not found' on DexScreener.

    A token without a pool has never been bought by anyone — it cannot be.
    Even a perfectly-formed paid transfer must not turn it into a signal.
    """
    monkeypatch.setattr(dexscreener, "market_data", _market_without(GEM))
    _live_watchlist(client, min_wallets=3)

    for wallet in WALLETS[:5]:
        _buy(client, wallet, token=GEM)

    assert client.get("/api/signals").json() == []
    assert client.get("/api/live/tokens").json()["tokens"] == []
    assert client.post("/api/live/sweep").json()["signals"] == 0

    # It is still inspectable, so "why is this missing" has an answer.
    shown = client.get("/api/live/tokens?include_untradeable=true").json()["tokens"]
    assert [row["token_address"] for row in shown] == [GEM]
    assert shown[0]["has_pair"] is False


def test_thin_liquidity_is_below_the_bar(client, monkeypatch):
    monkeypatch.setattr(
        dexscreener, "market_data", _market_without(liquidity=50.0)
    )
    monkeypatch.setattr(settings, "min_liquidity_usd", 1000.0)
    _live_watchlist(client, min_wallets=3)

    for wallet in WALLETS[:4]:
        _buy(client, wallet)

    # A pool exists, so the board shows it — but it is too thin to signal on.
    assert client.get("/api/live/tokens").json()["tokens"]
    assert client.get("/api/signals").json() == []


def test_market_data_enriches_the_board(client):
    _live_watchlist(client, min_wallets=3)
    for wallet in WALLETS[:3]:
        _buy(client, wallet)

    row = client.get("/api/live/tokens").json()["tokens"][0]

    assert row["has_pair"] is True
    assert row["price_usd"] == 0.01
    assert row["liquidity_usd"] == 50_000.0
    assert row["volume_24h"] == 10_000.0


def test_board_only_covers_live_watchlists(client):
    watchlist_id = _live_watchlist(client, min_wallets=5)
    for wallet in WALLETS[:3]:
        _buy(client, wallet)
    assert client.get("/api/live/tokens").json()["tokens"]

    db.update_watchlist_fields(watchlist_id, {"realtime": 0})

    assert client.get("/api/live/tokens").json()["tokens"] == []


# ----------------------------------------------------------------- pooling


def _second_watchlist(client, name, wallets):
    created = client.post(
        "/api/watchlists",
        json={
            "name": name,
            "chain": "ethereum",
            "wallets": wallets,
            "min_wallets": 2,
            "min_wallets_pct": 0,
            "buy_window_hours": 48,
        },
    ).json()
    db.update_watchlist_fields(created["id"], {"realtime": 1})
    return created["id"]


def test_wallets_from_several_watchlists_pool_into_one_threshold(client):
    """No single list reaches the threshold; together they do.

    This is the point of pooling — three buyers here and two there is five
    wallets buying the same token, and that is what matters.
    """
    _live_watchlist(client, wallets=WALLETS[:5])         # pool wallets 1-5
    _second_watchlist(client, "second list", WALLETS[5:])  # pool wallets 6-10

    for wallet in WALLETS[:3]:
        _buy(client, wallet)
    assert client.get("/api/signals").json() == []

    for wallet in WALLETS[5:7]:
        _buy(client, wallet)

    signals = client.get("/api/signals").json()
    assert len(signals) == 1
    signal = signals[0]
    assert signal["wallet_count"] == 5
    # A pooled signal belongs to no single watchlist.
    assert signal["watchlist_id"] is None
    assert signal["watchlist_size"] == 10


def test_a_pooled_signal_says_which_watchlists_it_came_from(client):
    first = _live_watchlist(client, wallets=WALLETS[:5])
    second = _second_watchlist(client, "second list", WALLETS[5:])

    for wallet in WALLETS[:3]:      # three from the first list
        _buy(client, wallet)
    for wallet in WALLETS[5:7]:     # two from the second
        _buy(client, wallet)

    signal = client.get("/api/signals").json()[0]
    shares = {share["name"]: share for share in signal["breakdown"]}

    assert shares["early buyers"]["wallets"] == 3
    assert shares["early buyers"]["share_pct"] == 60.0
    assert shares["second list"]["wallets"] == 2
    assert shares["second list"]["share_pct"] == 40.0
    # Largest contributor first, so the badge order reads sensibly.
    assert signal["breakdown"][0]["watchlist_id"] == first
    assert signal["breakdown"][1]["watchlist_id"] == second


def test_a_wallet_in_two_watchlists_counts_once_but_credits_both(client):
    _live_watchlist(client, wallets=WALLETS[:5])
    _second_watchlist(client, "overlapping", WALLETS[:5])   # the same wallets

    for wallet in WALLETS[:5]:
        _buy(client, wallet)

    signal = client.get("/api/signals").json()[0]

    # Five wallets bought, not ten: the pool is distinct wallets.
    assert signal["wallet_count"] == 5
    assert signal["watchlist_size"] == 5
    # Both lists are credited, so the shares total more than 100 — which is
    # honest for overlapping lists rather than silently picking one.
    assert [share["wallets"] for share in signal["breakdown"]] == [5, 5]


def test_the_pooled_percentage_is_adjustable(client):
    _live_watchlist(client)

    for wallet in WALLETS[:3]:
        _buy(client, wallet)
    assert client.get("/api/signals").json() == []   # 3 of 10 is under 50%

    settings_now = client.put("/api/settings/pool", json={"pool_pct": 30}).json()
    assert settings_now["pool_pct"] == 30.0
    assert settings_now["pools"][0]["required"] == 3

    assert client.post("/api/live/sweep").json()["signals"] == 1


def test_the_pooled_percentage_is_validated(client):
    assert client.put("/api/settings/pool", json={"pool_pct": 140}).status_code == 422
    assert (
        client.put("/api/settings/pool", json={"pool_min_wallets": 1}).status_code
        == 422
    )


def test_the_absolute_floor_stops_a_tiny_pool_firing_on_one_wallet(client, monkeypatch):
    monkeypatch.setattr(settings, "pool_pct", 10.0)
    monkeypatch.setattr(settings, "pool_min_wallets", 3)
    _live_watchlist(client)

    # 10% of ten wallets is one, but a single wallet is not a signal.
    _buy(client, WALLETS[0])
    assert client.get("/api/signals").json() == []

    for wallet in WALLETS[1:3]:
        _buy(client, wallet)
    assert len(client.get("/api/signals").json()) == 1


# ------------------------------------------------------------------- sweep


def test_sweep_fires_a_signal_the_event_path_could_not(client, monkeypatch):
    """Lowering the pooled percentage must not need a new buy to take effect."""
    _live_watchlist(client)
    for wallet in WALLETS[:4]:
        _buy(client, wallet)
    assert client.get("/api/signals").json() == []

    # Four buyers already clear 40% of the pool, but no further event will
    # ever arrive to trigger the check.
    client.put("/api/settings/pool", json={"pool_pct": 40})
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
    _live_watchlist(client)
    for wallet in WALLETS[:5]:
        _buy(client, wallet)
    assert len(client.get("/api/signals").json()) == 1

    # Nothing changed, so a re-check must not re-announce.
    first = client.post("/api/live/sweep").json()
    second = client.post("/api/live/sweep").json()

    assert first["signals"] == 0
    assert second["signals"] == 0
    assert len(client.get("/api/signals").json()) == 1


def test_sweep_reports_a_strengthened_signal_once(client):
    _live_watchlist(client)
    for wallet in WALLETS[:5]:
        _buy(client, wallet)

    db.record_events(
        [
            {
                "chain": "ethereum",
                "wallet_address": WALLETS[5],
                "token_address": GEM,
                "tx_hash": "0xextra",
                "token_symbol": "GEM",
                "is_buy": True,
            }
        ]
    )

    assert client.post("/api/live/sweep").json()["signals"] == 1   # buyer count grew
    assert client.post("/api/live/sweep").json()["signals"] == 0   # already reported
    assert client.get("/api/signals").json()[0]["wallet_count"] == 6


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
