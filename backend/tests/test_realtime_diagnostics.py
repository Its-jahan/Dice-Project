"""Webhook diagnostics: delivery log, reachability probe, simulated signal."""

import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, main
from app import dexscreener
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore

SIGNING_KEY = "whsec-test"
WEBHOOK_ID = "wh_diag"
WALLETS = [f"0x{i:040x}" for i in range(1, 6)]


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
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", None, raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", None, raising=False)
    monkeypatch.setattr(settings, "public_base_url", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))
    with TestClient(main.app) as test_client:
        yield test_client


def _live_watchlist(client, **overrides):
    body = {
        "name": "live list",
        "chain": "ethereum",
        "wallets": WALLETS,
        "min_wallets": 3,
        "min_wallets_pct": 0,
        **overrides,
    }
    created = client.post("/api/watchlists", json=body).json()
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


# ----------------------------------------------------------- delivery log


def test_every_delivery_outcome_is_logged(client):
    _live_watchlist(client)
    payload = {
        "webhookId": WEBHOOK_ID,
        "type": "ADDRESS_ACTIVITY",
        "event": {
            "network": "ETH_MAINNET",
            "activity": [
                {
                    "fromAddress": "0x" + "1" * 40,
                    "toAddress": WALLETS[0],
                    "hash": "0xaa",
                    "blockNum": "0x1",
                    "value": 1.0,
                    "asset": "GEM",
                    "category": "erc20",
                    "rawContract": {"address": "0x" + "9" * 40},
                }
            ],
        },
    }
    body = json.dumps(payload).encode()
    good = hmac.new(SIGNING_KEY.encode(), body, hashlib.sha256).hexdigest()

    client.post(
        "/api/webhooks/alchemy",
        content=body,
        headers={"X-Alchemy-Signature": good, "Content-Type": "application/json"},
    )
    client.post(
        "/api/webhooks/alchemy",
        content=body,
        headers={"X-Alchemy-Signature": "wrong", "Content-Type": "application/json"},
    )
    stranger = json.dumps({**payload, "webhookId": "wh_other"}).encode()
    client.post(
        "/api/webhooks/alchemy",
        content=stranger,
        headers={"X-Alchemy-Signature": "x", "Content-Type": "application/json"},
    )

    data = client.get("/api/settings/realtime/deliveries").json()
    statuses = [row["status"] for row in data["deliveries"]]

    # Newest first: unknown webhook, bad signature, then the good one.
    assert statuses == ["unknown_webhook", "bad_signature", "ok"]
    accepted = data["deliveries"][-1]
    assert accepted["activity_count"] == 1
    assert accepted["stored"] == 1
    assert data["last_delivery_at"] == accepted["received_at"]


def test_delivery_history_is_capped(client, monkeypatch):
    monkeypatch.setattr(db, "DELIVERY_HISTORY_LIMIT", 5)
    for index in range(8):
        db.record_delivery(chain="ethereum", status="ok", stored=index)

    rows = db.list_deliveries(limit=100)

    assert len(rows) == 5
    assert rows[0]["stored"] == 7  # newest kept


# --------------------------------------------------------- reachability


def test_check_url_reports_success(client, monkeypatch):
    client.put(
        "/api/settings/realtime", json={"public_base_url": "https://dice.example"}
    )
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"probe": "ok"})

    _mock_httpx(monkeypatch, handler)

    result = client.post("/api/settings/realtime/check-url").json()

    assert result["reachable"] is True
    assert seen["url"] == "https://dice.example/api/webhooks/alchemy"
    assert seen["body"]["webhookId"] == main.PROBE_WEBHOOK_ID


def test_check_url_reports_an_unreachable_host(client, monkeypatch):
    client.put(
        "/api/settings/realtime", json={"public_base_url": "https://dice.example"}
    )

    def handler(_request):
        raise httpx.ConnectError("name resolution failed")

    _mock_httpx(monkeypatch, handler)

    response = client.post("/api/settings/realtime/check-url")

    assert response.status_code == 502
    assert "Could not reach" in response.json()["detail"]


def test_check_url_detects_a_different_app_on_the_path(client, monkeypatch):
    client.put(
        "/api/settings/realtime", json={"public_base_url": "https://dice.example"}
    )

    def handler(_request):
        return httpx.Response(200, json={"hello": "some other service"})

    _mock_httpx(monkeypatch, handler)

    response = client.post("/api/settings/realtime/check-url")

    assert response.status_code == 502
    assert "different application" in response.json()["detail"]


def test_check_url_needs_the_public_url_configured(client):
    assert client.post("/api/settings/realtime/check-url").status_code == 422


def test_probe_delivery_is_labelled_not_treated_as_a_stray_webhook(client):
    response = client.post(
        "/api/webhooks/alchemy",
        json={"webhookId": main.PROBE_WEBHOOK_ID, "type": "ADDRESS_ACTIVITY"},
    )

    assert response.json() == {"probe": "ok"}
    assert client.get("/api/settings/realtime/deliveries").json()["deliveries"][0][
        "status"
    ] == "probe"


# ------------------------------------------------------------- simulation


def test_simulate_fires_a_real_signal_through_the_live_path(client):
    watchlist_id = _live_watchlist(client)

    result = client.post(
        "/api/settings/realtime/simulate", json={"watchlist_id": watchlist_id}
    ).json()

    assert result["wallets_used"] == 3       # the threshold, not every wallet
    assert result["signals"] == 1
    assert result["token_address"] == main.SIMULATED_TOKEN

    signals = client.get("/api/signals").json()
    assert len(signals) == 1
    assert signals[0]["token_address"] == main.SIMULATED_TOKEN
    assert signals[0]["wallet_count"] == 3
    assert {buyer["via"] for buyer in signals[0]["buyers"]} == {"live"}

    assert client.get("/api/settings/realtime/deliveries").json()["deliveries"][0][
        "status"
    ] == "simulated"


def test_repeat_simulations_are_not_deduplicated_away(client):
    watchlist_id = _live_watchlist(client)

    first = client.post(
        "/api/settings/realtime/simulate", json={"watchlist_id": watchlist_id}
    ).json()
    second = client.post(
        "/api/settings/realtime/simulate", json={"watchlist_id": watchlist_id}
    ).json()

    # Fresh tx hashes each run, so the events land even though the signal for
    # the token already exists.
    assert first["stored"] == 3
    assert second["stored"] == 3


def test_simulate_requires_a_live_watchlist(client):
    created = client.post(
        "/api/watchlists",
        json={
            "name": "not live",
            "chain": "ethereum",
            "wallets": WALLETS,
            "min_wallets": 3,
            "min_wallets_pct": 0,
        },
    ).json()

    response = client.post(
        "/api/settings/realtime/simulate", json={"watchlist_id": created["id"]}
    )

    assert response.status_code == 422
    assert "Switch Live on" in response.json()["detail"]


def test_simulate_explains_an_unreachable_threshold(client):
    watchlist_id = _live_watchlist(client, min_wallets=3)
    # Leave fewer wallets than the threshold needs.
    db.remove_wallets(watchlist_id, WALLETS[1:])

    response = client.post(
        "/api/settings/realtime/simulate", json={"watchlist_id": watchlist_id}
    )

    assert response.status_code == 422
    assert "lower the threshold" in response.json()["detail"]


def test_simulate_rejects_a_missing_watchlist(client):
    assert (
        client.post(
            "/api/settings/realtime/simulate", json={"watchlist_id": 4242}
        ).status_code
        == 404
    )
    assert (
        client.post("/api/settings/realtime/simulate", json={}).status_code == 422
    )


# ------------------------------------------------------------------ helper


def _mock_httpx(monkeypatch, handler):
    """Route main's outbound httpx calls through a scripted transport."""
    real_client = httpx.AsyncClient

    def factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", factory)
