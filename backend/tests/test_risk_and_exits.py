"""Contract screening, and watching the money leave as well as arrive."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import db, dexscreener, main, realtime, security
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore
from app.models import Chain

SIGNING_KEY = "whsec-risk"
WEBHOOK_ID = "wh_risk"
WALLETS = [f"0x{i:040x}" for i in range(1, 11)]
GEM = "0x" + "9" * 40
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def _settle(client):
    """A delivery only records; the sweep turns it into a signal."""
    return client.post("/api/live/sweep").json()



def _market():
    async def market_data(chain, addresses, refresh=False):
        return {
            str(address).lower(): {
                "has_pair": True,
                "price_usd": 0.01,
                "liquidity_usd": 50_000.0,
                "volume_24h": 10_000.0,
                "fdv": None,
                "symbol": "GEM",
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


def _screen(**verdict):
    async def screen(chain, addresses, refresh=False):
        return {str(a).lower(): dict(verdict) for a in addresses}

    return screen


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


def _deliver(client, activity):
    payload = {
        "webhookId": WEBHOOK_ID,
        "type": "ADDRESS_ACTIVITY",
        "event": {"network": "ETH_MAINNET", "activity": activity},
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(SIGNING_KEY.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/webhooks/alchemy",
        content=body,
        headers={"X-Alchemy-Signature": signature, "Content-Type": "application/json"},
    )


def _transfer(sender, recipient, token, tx, *, asset="GEM", category="erc20"):
    return {
        "fromAddress": sender,
        "toAddress": recipient,
        "hash": tx,
        "blockNum": "0x1",
        "value": 100.0,
        "asset": asset,
        "category": category,
        "rawContract": {"address": token},
    }


def _buy(client, wallet, token=GEM):
    tx = f"0xb{wallet[-6:]}{token[-4:]}"
    return _deliver(
        client,
        [
            _transfer("0x" + "1" * 40, wallet, token, tx),
            _transfer(wallet, "0x" + "d" * 40, None, tx, asset="ETH", category="external"),
        ],
    )


# ------------------------------------------------------------------ verdicts


def test_a_honeypot_is_the_only_thing_that_blocks():
    """Unsellable is impossible; expensive is merely unwise, and that is yours."""
    trap = security.assess({"is_honeypot": "1", "sell_tax": "0"})
    assert trap["blocked"] is True
    assert "Honeypot" in trap["blockers"][0]

    taxed = security.assess({"is_honeypot": "0", "sell_tax": "0.35", "buy_tax": "0"})
    assert taxed["blocked"] is False
    assert taxed["sell_tax"] == 35.0
    assert any("Sell tax 35.0%" in w for w in taxed["warnings"])


def test_the_ordinary_token_produces_no_noise():
    clean = security.assess(
        {
            "is_honeypot": "0",
            "buy_tax": "0",
            "sell_tax": "0",
            "is_open_source": "1",
            "is_mintable": "0",
            "holder_count": "42000",
        }
    )
    assert clean["blocked"] is False
    assert clean["warnings"] == []
    assert clean["holder_count"] == 42000


def test_owner_powers_warn_without_blocking():
    verdict = security.assess(
        {
            "is_honeypot": "0",
            "is_mintable": "1",
            "transfer_pausable": "1",
            "hidden_owner": "1",
            "is_open_source": "0",
        }
    )
    assert verdict["blocked"] is False
    assert len(verdict["warnings"]) == 4


def test_an_outage_is_never_read_as_a_scam():
    """Failing closed would take the whole system down with the API."""
    verdict = security.unchecked("the risk API did not answer")
    assert verdict["checked"] is False
    assert verdict["blocked"] is False


def test_an_unscreened_chain_says_so(monkeypatch):
    assert security.supported(Chain.ethereum) is True
    assert security.supported(Chain.solana) is False


# --------------------------------------------------------------------- gate


@pytest.mark.anyio
async def test_a_honeypot_never_becomes_a_signal(client, monkeypatch):
    monkeypatch.setattr(
        security,
        "screen",
        _screen(checked=True, blocked=True, blockers=["Honeypot"], warnings=[]),
    )
    _live_watchlist(client)
    for wallet in WALLETS[:6]:
        _buy(client, wallet)
    _settle(client)

    # Six of ten wallets bought — over the 50% threshold — and it still must
    # not fire, because the contract will not let anyone sell.
    assert client.get("/api/signals").json() == []

    # It stays *visible* on the board though, marked blocked. Hiding it would
    # be indistinguishable from a broken webhook, and the operator has a real
    # interest in knowing their wallets walked into a honeypot.
    board = client.get("/api/live/tokens").json()["tokens"]
    assert len(board) == 1
    assert board[0]["risk"]["blocked"] is True
    assert board[0]["signal_status"] is None


@pytest.mark.anyio
async def test_warnings_travel_with_the_signal_instead_of_blocking_it(client, monkeypatch):
    monkeypatch.setattr(
        security,
        "screen",
        _screen(
            checked=True,
            blocked=False,
            blockers=[],
            warnings=["Sell tax 30.0%", "Mint authority is open"],
        ),
    )
    _live_watchlist(client)
    for wallet in WALLETS[:6]:
        _buy(client, wallet)
    _settle(client)

    signals = client.get("/api/signals").json()
    assert len(signals) == 1
    board = client.get("/api/live/tokens").json()["tokens"]
    assert board[0]["risk"]["warnings"] == ["Sell tax 30.0%", "Mint authority is open"]


@pytest.mark.anyio
async def test_screening_can_be_switched_off(client, monkeypatch):
    called = False

    async def screen(chain, addresses, refresh=False):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(security, "screen", screen)
    client.put("/api/settings/pool", json={"risk_screening": False})

    _live_watchlist(client)
    for wallet in WALLETS[:6]:
        _buy(client, wallet)
    _settle(client)

    assert len(client.get("/api/signals").json()) == 1
    assert called is False


# -------------------------------------------------------------------- exits


def test_a_sale_needs_something_to_come_back():
    """A one-way departure is a transfer, not an exit."""
    watched = {WALLETS[0]}
    sale = realtime.parse_activity(
        {
            "event": {
                "activity": [
                    _transfer(WALLETS[0], "0x" + "d" * 40, GEM, "0xsell"),
                    _transfer(
                        "0x" + "d" * 40, WALLETS[0], WETH, "0xsell", asset="WETH"
                    ),
                ]
            }
        },
        chain=Chain.ethereum,
        watched=watched,
    )
    gem = next(e for e in sale if e["token_address"] == GEM)
    assert gem["is_sell"] is True
    assert gem["wallet_address"] == WALLETS[0]

    # The same departure with nothing coming back is funding another wallet.
    moved = realtime.parse_activity(
        {"event": {"activity": [_transfer(WALLETS[0], "0x" + "d" * 40, GEM, "0xmove")]}},
        chain=Chain.ethereum,
        watched=watched,
    )
    assert moved[0]["is_sell"] is False
    assert moved[0]["is_buy"] is False


def test_a_buy_is_not_mistaken_for_a_sale():
    """The token's own arrival must not count as 'something came back'."""
    events = realtime.parse_activity(
        {
            "event": {
                "activity": [
                    _transfer("0x" + "1" * 40, WALLETS[0], GEM, "0xbuy"),
                    _transfer(
                        WALLETS[0], "0x" + "d" * 40, None, "0xbuy",
                        asset="ETH", category="external",
                    ),
                ]
            }
        },
        chain=Chain.ethereum,
        watched={WALLETS[0]},
    )
    assert len(events) == 1
    assert events[0]["is_buy"] is True
    assert events[0]["is_sell"] is False


def test_moving_a_token_between_watched_wallets_is_neither(client):
    events = realtime.parse_activity(
        {"event": {"activity": [_transfer(WALLETS[0], WALLETS[1], GEM, "0xshuffle")]}},
        chain=Chain.ethereum,
        watched={WALLETS[0], WALLETS[1]},
    )
    # One position moving is not a seller and not a buyer.
    assert events == []


@pytest.mark.anyio
async def test_the_board_shows_how_many_buyers_have_left(client):
    _live_watchlist(client)
    for wallet in WALLETS[:6]:
        _buy(client, wallet)
    assert client.get("/api/live/tokens").json()["tokens"][0]["sellers"] == 0

    # Two of the six sell out.
    for wallet in WALLETS[:2]:
        _deliver(
            client,
            [
                _transfer(wallet, "0x" + "d" * 40, GEM, f"0xs{wallet[-6:]}"),
                _transfer(
                    "0x" + "d" * 40, wallet, WETH, f"0xs{wallet[-6:]}", asset="WETH"
                ),
            ],
        )

    board = client.get("/api/live/tokens").json()["tokens"]
    row = next(r for r in board if r["token_address"] == GEM)
    assert row["sellers"] == 2
    # The buy count is untouched — they did buy, and then they left.
    assert row["wallet_count"] == 6


@pytest.mark.anyio
async def test_the_exit_board_lists_what_is_being_sold(client):
    _live_watchlist(client)
    for wallet in WALLETS[:6]:
        _buy(client, wallet)
    assert client.get("/api/live/exits").json()["tokens"] == []

    for wallet in WALLETS[:3]:
        _deliver(
            client,
            [
                _transfer(wallet, "0x" + "d" * 40, GEM, f"0xs{wallet[-6:]}"),
                _transfer(
                    "0x" + "d" * 40, wallet, WETH, f"0xs{wallet[-6:]}", asset="WETH"
                ),
            ],
        )

    exits = client.get("/api/live/exits").json()["tokens"]
    assert len(exits) == 1
    assert exits[0]["token_address"] == GEM
    assert exits[0]["wallet_count"] == 3
    assert exits[0]["pool_size"] == len(WALLETS)


def test_one_transaction_can_be_both_a_buy_and_a_sale(client, monkeypatch):
    """A router moving a token through a wallet must not lose half the story."""
    monkeypatch.setattr(settings, "db_path", str(main.settings.db_path))
    db.record_events(
        [
            {
                "chain": "ethereum",
                "wallet_address": WALLETS[0],
                "token_address": GEM,
                "tx_hash": "0xboth",
                "token_symbol": "GEM",
                "amount": 1.0,
                "block_num": 1,
                "seen_at": "2026-08-04T10:00:00+00:00",
                "from_address": "0x" + "1" * 40,
                "is_buy": True,
            }
        ]
    )
    db.record_events(
        [
            {
                "chain": "ethereum",
                "wallet_address": WALLETS[0],
                "token_address": GEM,
                "tx_hash": "0xboth",
                "token_symbol": "GEM",
                "amount": 1.0,
                "block_num": 1,
                "seen_at": "2026-08-04T10:00:00+00:00",
                "from_address": "0x" + "1" * 40,
                "is_sell": True,
            }
        ]
    )
    with db.connect() as conn:
        row = conn.execute(
            "SELECT is_buy, is_sell FROM wallet_events WHERE tx_hash = '0xboth'"
        ).fetchone()
    assert row["is_buy"] == 1 and row["is_sell"] == 1
