"""Live monitoring: signature checks, payload parsing, and instant signals."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import alchemy, db, main, realtime
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore
from app.models import Chain

SIGNING_KEY = "whsec-test-key"
WEBHOOK_ID = "wh_test123"
WALLETS = [f"0x{i:040x}" for i in range(1, 7)]
GEM = "0x" + "9" * 40
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"   # on the built-in stoplist
OUTSIDER = "0x" + "e" * 40


def _activity(to_address, token=GEM, tx="0xabc", **overrides):
    item = {
        "fromAddress": OUTSIDER,
        "toAddress": to_address,
        "blockNum": "0x1312d00",
        "hash": tx,
        "value": 1500.0,
        "asset": "GEM",
        "category": "erc20",
        "rawContract": {"address": token, "decimals": 18, "rawValue": "0x1"},
    }
    item.update(overrides)
    return item


def _payload(*activities):
    return {
        "webhookId": WEBHOOK_ID,
        "id": "whevt_1",
        "createdAt": "2026-08-01T12:00:00.000Z",
        "type": "ADDRESS_ACTIVITY",
        "event": {"network": "ETH_MAINNET", "activity": list(activities)},
    }


def _sign(body: bytes) -> str:
    return hmac.new(SIGNING_KEY.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", None, raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", None, raising=False)
    monkeypatch.setattr(settings, "public_base_url", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))
    with TestClient(main.app) as test_client:
        yield test_client


def _live_watchlist(client, **overrides):
    """A realtime watchlist, created without touching Alchemy."""
    body = {
        "name": "early buyers",
        "chain": "ethereum",
        "wallets": WALLETS,
        "min_wallets": 3,
        "min_wallets_pct": 0,
        "buy_window_hours": 48,
        **overrides,
    }
    created = client.post("/api/watchlists", json=body)
    assert created.status_code == 201, created.text
    watchlist_id = created.json()["id"]
    # Flip the flag directly: turning it on through the API would try to reach
    # Alchemy, which these tests deliberately do not stub.
    db.update_watchlist_fields(watchlist_id, {"realtime": 1})
    db.save_webhook(
        chain="ethereum",
        network="ETH_MAINNET",
        webhook_id=WEBHOOK_ID,
        signing_key=SIGNING_KEY,
        webhook_url="https://dice.example/api/webhooks/alchemy",
        address_count=len(WALLETS),
    )
    return watchlist_id


def _deliver(client, payload, *, signature=None):
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Alchemy-Signature": signature if signature is not None else _sign(body),
    }
    return client.post("/api/webhooks/alchemy", content=body, headers=headers)


# ------------------------------------------------------------------ signature


def test_verify_signature_accepts_only_the_exact_body():
    body = b'{"hello":"world"}'
    good = hmac.new(SIGNING_KEY.encode(), body, hashlib.sha256).hexdigest()

    assert alchemy.verify_signature(body, good, SIGNING_KEY) is True
    assert alchemy.verify_signature(body, good, "other-key") is False
    assert alchemy.verify_signature(b'{"hello": "world"}', good, SIGNING_KEY) is False
    assert alchemy.verify_signature(body, None, SIGNING_KEY) is False


def test_delivery_without_a_valid_signature_is_rejected(client):
    _live_watchlist(client)

    response = _deliver(client, _payload(_activity(WALLETS[0])), signature="deadbeef")

    assert response.status_code == 401
    assert db.events_in_window(
        chain="ethereum", token_address=GEM, since_iso="2000-01-01", wallets=WALLETS
    ) == []


def test_delivery_for_an_unknown_webhook_is_rejected(client):
    payload = _payload(_activity(WALLETS[0]))
    payload["webhookId"] = "wh_not_ours"

    assert _deliver(client, payload).status_code == 404


# -------------------------------------------------------------------- parsing


def test_parse_activity_keeps_only_incoming_third_party_token_transfers():
    watched = {w.lower() for w in WALLETS}
    payload = _payload(
        _activity(WALLETS[0]),                                   # keep
        _activity(OUTSIDER, tx="0x2"),                           # not watched
        _activity(WALLETS[1], tx="0x3", fromAddress=WALLETS[2]),  # internal shuffle
        _activity(WALLETS[1], tx="0x4", category="external"),     # native ETH in
        _activity(WALLETS[1], tx="0x5", removed=True),            # reorg'd away
    )

    events = realtime.parse_activity(payload, chain=Chain.ethereum, watched=watched)

    assert len(events) == 1
    event = events[0]
    assert event["wallet_address"] == WALLETS[0]
    assert event["token_address"] == GEM
    assert event["token_symbol"] == "GEM"
    assert event["block_num"] == 0x1312D00


# ------------------------------------------------------------ end to end


def test_third_buyer_fires_a_signal_immediately(client):
    _live_watchlist(client)

    # Two buyers: below the threshold of 3, so nothing yet.
    for index in range(2):
        response = _deliver(
            client, _payload(_activity(WALLETS[index], tx=f"0x{index}"))
        )
        assert response.status_code == 200
        assert response.json()["signals"] == 0
    assert client.get("/api/signals").json() == []

    # The third buyer crosses it — the signal exists the moment the event lands.
    response = _deliver(client, _payload(_activity(WALLETS[2], tx="0x2c")))

    assert response.json()["signals"] == 1
    signals = client.get("/api/signals").json()
    assert len(signals) == 1
    signal = signals[0]
    assert signal["token_address"] == GEM
    assert signal["wallet_count"] == 3
    assert {buyer["via"] for buyer in signal["buyers"]} == {"live"}
    # The transfer feed carries no price, and the signal says so rather than
    # inventing one.
    assert signal["total_usd"] is None


def test_redelivery_does_not_double_count_a_buyer(client):
    _live_watchlist(client)
    payload = _payload(
        _activity(WALLETS[0], tx="0xa"),
        _activity(WALLETS[1], tx="0xb"),
        _activity(WALLETS[2], tx="0xc"),
    )

    first = _deliver(client, payload).json()
    # Alchemy retries anything not answered 2xx; the same events must be inert.
    second = _deliver(client, payload).json()

    assert first["stored"] == 3
    assert second["stored"] == 0
    assert client.get("/api/signals").json()[0]["wallet_count"] == 3


def test_stoplisted_token_never_fires(client):
    _live_watchlist(client)

    for index in range(4):
        _deliver(
            client,
            _payload(_activity(WALLETS[index], token=USDC, tx=f"0xu{index}")),
        )

    assert client.get("/api/signals").json() == []


def test_events_only_count_inside_the_buy_window(client):
    watchlist_id = _live_watchlist(client)
    for index in range(3):
        _deliver(client, _payload(_activity(WALLETS[index], tx=f"0x{index}")))
    assert len(client.get("/api/signals").json()) == 1

    # Shrink the window so the stored events fall outside it; a re-evaluation
    # must then find nothing rather than re-report stale buyers.
    db.update_watchlist_fields(watchlist_id, {"buy_window_hours": 1})
    with db.connect() as conn:
        conn.execute(
            "UPDATE wallet_events SET seen_at = '2020-01-01T00:00:00+00:00'"
        )

    watchlist = db.get_watchlist(watchlist_id)
    assert realtime.evaluate_token(watchlist, GEM) is None


def test_non_address_activity_payloads_are_ignored(client):
    _live_watchlist(client)
    payload = _payload(_activity(WALLETS[0]))
    payload["type"] = "NFT_ACTIVITY"

    assert _deliver(client, payload).json() == {"ignored": "NFT_ACTIVITY"}


def test_wallets_of_non_realtime_watchlists_are_not_watched(client):
    watchlist_id = _live_watchlist(client)
    db.update_watchlist_fields(watchlist_id, {"realtime": 0})

    response = _deliver(client, _payload(_activity(WALLETS[0])))

    assert response.json() == {"events": 0, "stored": 0, "signals": 0}


# --------------------------------------------------------------- settings API


def test_realtime_settings_roundtrip_and_masking(client):
    empty = client.get("/api/settings/realtime").json()
    assert empty["configured"] is False
    assert "ethereum" in empty["supported_chains"]

    saved = client.put(
        "/api/settings/realtime",
        json={
            "auth_token": "alch-secret-token-abcd",
            "public_base_url": "https://dice.example/",
        },
    ).json()

    assert saved["configured"] is True
    assert saved["token_hint"] == "…abcd"
    assert "secret" not in str(saved)
    assert saved["public_base_url"] == "https://dice.example"

    config = client.get("/api/config").json()
    assert config["realtime"]["configured"] is True
    assert config["realtime"]["public_url_set"] is True


def test_public_url_must_be_absolute(client):
    response = client.put(
        "/api/settings/realtime", json={"public_base_url": "dice.example"}
    )

    assert response.status_code == 422


def test_sync_without_a_token_is_reported_not_raised(client):
    _live_watchlist(client)

    body = client.post("/api/settings/realtime/sync").json()

    assert body["synced"][0]["error"]
