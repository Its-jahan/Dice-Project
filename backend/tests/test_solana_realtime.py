"""Solana live monitoring through Helius.

The parser is built from Helius's documented enhanced-webhook shape and is
covered here. The HTTP client is not exercised against a live account — that
needs an API key — so these tests pin the behaviour DICE controls: what counts
as a buy, what counts as a sale, and that a delivery nobody can authenticate
never reaches the event store.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import db, dexscreener, helius, main
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore
from app.models import Chain

WALLETS = [
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
]
# Deliberately not USDC or wSOL: those are on the quote-currency stoplist, so
# a "signal" on one would only ever mean somebody swapped.
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
SOL = helius.WRAPPED_SOL
OUTSIDER = "3nJ6mCLkNVSQVjB1RJZQnPCrfXvGXWNHo7HbTHYCu9Vy"
SEEN = "2026-08-04T12:00:00+00:00"


def _settle(client):
    """A delivery only records; the sweep turns it into a signal."""
    return client.post("/api/live/sweep").json()



def _tx(signature, *, token=(), native=()):
    return {
        "signature": signature,
        "slot": 300_000_000,
        "type": "SWAP",
        "tokenTransfers": [
            {
                "fromUserAccount": sender,
                "toUserAccount": recipient,
                "mint": mint,
                "tokenAmount": amount,
            }
            for sender, recipient, mint, amount in token
        ],
        "nativeTransfers": [
            {"fromUserAccount": sender, "toUserAccount": recipient, "amount": amount}
            for sender, recipient, amount in native
        ],
    }


# ------------------------------------------------------------------ parsing


def test_a_swap_is_a_buy():
    """Token in, SOL out, same transaction — the Solana form of paying for it."""
    events = helius.parse_activity(
        [
            _tx(
                "sig1",
                token=((OUTSIDER, WALLETS[0], MINT, 1500.0),),
                native=((WALLETS[0], OUTSIDER, 2_000_000_000),),
            )
        ],
        watched=set(WALLETS),
        seen_at=SEEN,
    )
    assert len(events) == 1
    event = events[0]
    assert event["chain"] == "solana"
    assert event["wallet_address"] == WALLETS[0]
    assert event["token_address"] == MINT
    assert event["tx_hash"] == "sig1"
    assert event["is_buy"] is True
    assert event["is_sell"] is False
    assert event["amount"] == 1500.0
    assert event["block_num"] == 300_000_000


def test_a_one_way_arrival_is_an_airdrop():
    events = helius.parse_activity(
        [_tx("sig2", token=((OUTSIDER, WALLETS[0], MINT, 9_999.0),))],
        watched=set(WALLETS),
        seen_at=SEEN,
    )
    assert events[0]["is_buy"] is False
    assert events[0]["is_sell"] is False


def test_selling_back_into_sol_is_a_sale():
    events = helius.parse_activity(
        [
            _tx(
                "sig3",
                token=((WALLETS[0], OUTSIDER, MINT, 1500.0),),
                native=((OUTSIDER, WALLETS[0], 3_000_000_000),),
            )
        ],
        watched=set(WALLETS),
        seen_at=SEEN,
    )
    assert len(events) == 1
    assert events[0]["is_sell"] is True
    assert events[0]["wallet_address"] == WALLETS[0]


def test_sending_a_token_away_for_nothing_is_not_a_sale():
    """Funding another wallet is not an exit, and must not read as one."""
    events = helius.parse_activity(
        [_tx("sig4", token=((WALLETS[0], OUTSIDER, MINT, 1500.0),))],
        watched=set(WALLETS),
        seen_at=SEEN,
    )
    assert events[0]["is_sell"] is False
    assert events[0]["is_buy"] is False


def test_a_move_between_watched_wallets_is_neither():
    events = helius.parse_activity(
        [_tx("sig5", token=((WALLETS[0], WALLETS[1], MINT, 1500.0),))],
        watched=set(WALLETS),
        seen_at=SEEN,
    )
    assert events == []


def test_a_swap_of_one_token_for_another_is_both_a_buy_and_a_sale():
    """The token coming back must not be mistaken for the one going out."""
    other = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
    events = helius.parse_activity(
        [
            _tx(
                "sig6",
                token=(
                    (WALLETS[0], OUTSIDER, MINT, 1500.0),
                    (OUTSIDER, WALLETS[0], other, 42.0),
                ),
            )
        ],
        watched=set(WALLETS),
        seen_at=SEEN,
    )
    sold = next(e for e in events if e["token_address"] == MINT)
    bought = next(e for e in events if e["token_address"] == other)
    assert sold["is_sell"] is True
    assert bought["is_buy"] is True
    # ...and neither is confused for the other.
    assert sold["is_buy"] is False
    assert bought["is_sell"] is False


def test_transactions_without_a_signature_are_dropped():
    assert helius.parse_activity(
        [{"tokenTransfers": [{"fromUserAccount": OUTSIDER, "toUserAccount": WALLETS[0], "mint": MINT}]}],
        watched=set(WALLETS),
        seen_at=SEEN,
    ) == []


def test_a_single_object_is_accepted_as_well_as_an_array():
    """Helius sends an array; a proxy that unwraps it must not break ingest."""
    events = helius.parse_activity(
        _tx("sig7", token=((OUTSIDER, WALLETS[0], MINT, 1.0),)),
        watched=set(WALLETS),
        seen_at=SEEN,
    )
    assert len(events) == 1


# ------------------------------------------------------------------ the secret


def test_the_delivery_secret_is_compared_exactly():
    secret = helius.new_auth_secret()
    assert len(secret) >= 32
    assert helius.verify_auth(secret, secret) is True
    assert helius.verify_auth(f"Bearer {secret}", secret) is True
    assert helius.verify_auth(secret[:-1], secret) is False
    assert helius.verify_auth(None, secret) is False
    # An empty stored secret must never authenticate anything.
    assert helius.verify_auth("", "") is False
    assert helius.verify_auth("anything", "") is False


# -------------------------------------------------------------------- endpoint


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "live_sweep_seconds", 3600)
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))

    async def market_data(chain, addresses, refresh=False):
        return {
            str(a): {
                "has_pair": True,
                "price_usd": 0.01,
                "liquidity_usd": 50_000.0,
                "volume_24h": 1000.0,
                "fdv": None,
                "symbol": "BONK",
                "name": None,
                "pair_url": None,
                "pair_created_at": None,
            }
            for a in addresses
        }

    monkeypatch.setattr(dexscreener, "market_data", market_data)
    with TestClient(main.app) as test_client:
        yield test_client


def _register(secret="s3cret-value-long-enough-to-pass"):
    created = db.create_watchlist(
        name="solana cohort",
        chain="solana",
        wallets=WALLETS,
        source_token_address=None,
        notes="",
        min_wallets=2,
        min_wallets_pct=0.0,
        buy_window_hours=48,
        monitor_interval_hours=24.0,
        min_buy_usd=0.0,
        auto_monitor=False,
        ignore_tokens=[],
        realtime=True,
    )
    db.save_webhook(
        chain="solana",
        network="solana",
        webhook_id="hook-1",
        signing_key=secret,
        webhook_url="https://dice.example/api/webhooks/helius",
        address_count=len(WALLETS),
    )
    return created


def test_a_delivery_without_the_secret_is_rejected(client):
    _register()
    response = client.post(
        "/api/webhooks/helius",
        content=json.dumps([_tx("sig8", token=((OUTSIDER, WALLETS[0], MINT, 1.0),))]),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401
    assert db.list_deliveries(limit=1)[0]["status"] == "bad_signature"


def test_a_delivery_before_any_webhook_exists_is_answered_not_retried(client):
    """A non-2xx makes providers retry and then disable the webhook."""
    response = client.post(
        "/api/webhooks/helius",
        content=json.dumps([]),
        headers={"Content-Type": "application/json", "Authorization": "whatever"},
    )
    assert response.status_code == 200
    assert response.json()["events"] == 0


def test_an_authenticated_delivery_becomes_events_and_a_signal(client):
    secret = "s3cret-value-long-enough-to-pass"
    _register(secret)
    body = json.dumps(
        [
            _tx(
                f"sig-{index}",
                token=((OUTSIDER, wallet, MINT, 100.0),),
                native=((wallet, OUTSIDER, 1_000_000_000),),
            )
            for index, wallet in enumerate(WALLETS)
        ]
    )
    response = client.post(
        "/api/webhooks/helius",
        content=body,
        headers={"Content-Type": "application/json", "Authorization": secret},
    )

    assert response.status_code == 200
    assert response.json()["events"] == 3
    # Three of three wallets bought the same mint: a signal on Solana, fired
    # by exactly the same pooled threshold every EVM chain uses.
    _settle(client)
    signals = client.get("/api/signals").json()
    assert len(signals) == 1
    assert signals[0]["chain"] == "solana"
    assert signals[0]["token_address"] == MINT


def test_the_sync_endpoint_says_which_key_is_missing(client):
    _register()
    db.delete_webhook("solana")
    result = client.post("/api/settings/realtime/sync").json()["synced"]
    solana = next(row for row in result if row.get("chain") == "solana")
    assert "Helius" in solana["error"]


def test_settings_report_solana_as_reachable(client):
    body = client.get("/api/settings/realtime").json()
    assert Chain.solana.value in body["supported_chains"]
    assert body["helius_configured"] is False
