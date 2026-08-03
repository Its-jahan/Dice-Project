"""Alchemy Notify client: batching, pagination and the reconcile flow."""

import asyncio
import json

import httpx
import pytest

from app import db, main
from app.alchemy import ADDRESS_BATCH, AlchemyError, AlchemyNotifyClient
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore

from fastapi.testclient import TestClient


def _client_with(handler) -> AlchemyNotifyClient:
    """A client whose HTTP layer is a scripted mock instead of the network."""
    client = AlchemyNotifyClient("test-token")
    client._client = httpx.AsyncClient(
        base_url="https://dashboard.alchemy.com/api",
        transport=httpx.MockTransport(handler),
        headers={"X-Alchemy-Token": "test-token"},
    )
    return client


def _call(handler, action):
    """Run one client coroutine against a mock transport, synchronously."""

    async def main_coro():
        async with _client_with(handler) as client:
            return await action(client)

    return asyncio.run(main_coro())


def test_auth_token_is_required():
    with pytest.raises(AlchemyError):
        AlchemyNotifyClient("   ")


def test_update_addresses_batches_and_sends_removals_once():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={})

    wallets = [f"0x{i:040x}" for i in range(ADDRESS_BATCH + 20)]
    _call(
        handler,
        lambda client: client.update_addresses(
            "wh_1", add=wallets, remove=["0xdead"]
        ),
    )

    assert len(calls) == 2
    assert len(calls[0]["addresses_to_add"]) == ADDRESS_BATCH
    assert len(calls[1]["addresses_to_add"]) == 20
    # Removals must not be repeated per batch.
    assert calls[0]["addresses_to_remove"] == ["0xdead"]
    assert calls[1]["addresses_to_remove"] == []


def test_update_addresses_with_only_removals_makes_one_call():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={})

    _call(
        handler,
        lambda client: client.update_addresses("wh_1", add=[], remove=["0xdead"]),
    )

    assert len(calls) == 1
    assert calls[0]["addresses_to_add"] == []
    assert calls[0]["addresses_to_remove"] == ["0xdead"]


def test_list_addresses_follows_the_cursor():
    pages = {
        None: (["0xa", "0xb"], "cursor-1"),
        "cursor-1": (["0xc"], None),
    }

    def handler(request):
        after = request.url.params.get("after")
        data, next_cursor = pages[after]
        body = {"data": data, "pagination": {"cursors": {}}}
        if next_cursor:
            body["pagination"]["cursors"]["after"] = next_cursor
        return httpx.Response(200, json=body)

    addresses = _call(handler, lambda client: client.list_addresses("wh_1"))

    assert addresses == ["0xa", "0xb", "0xc"]


def test_list_addresses_stops_if_the_cursor_stalls():
    """A cursor that never advances must not spin forever against the API."""

    def handler(_request):
        return httpx.Response(
            200,
            json={"data": ["0xa"], "pagination": {"cursors": {"after": "stuck"}}},
        )

    addresses = _call(handler, lambda client: client.list_addresses("wh_1"))

    assert addresses == ["0xa", "0xa"]  # first page, one retry, then bail


def test_bad_token_is_explained():
    def handler(_request):
        return httpx.Response(401, json={"message": "unauthorized"})

    with pytest.raises(AlchemyError) as excinfo:
        _call(handler, lambda client: client.list_addresses("wh_1"))

    assert "webhooks dashboard" in str(excinfo.value)


# ------------------------------------------------------------ reconcile flow


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "public_base_url", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))
    with TestClient(main.app) as test_client:
        yield test_client


def test_sync_creates_then_reconciles_the_address_list(api, monkeypatch):
    api.put(
        "/api/settings/realtime",
        json={"auth_token": "tok", "public_base_url": "https://dice.example"},
    )
    wallets = [f"0x{i:040x}" for i in range(1, 4)]
    created = api.post(
        "/api/watchlists",
        json={
            "name": "live",
            "chain": "ethereum",
            "wallets": wallets,
            "min_wallets": 2,
            "min_wallets_pct": 0,
        },
    ).json()
    db.update_watchlist_fields(created["id"], {"realtime": 1})

    state = {"registered": [], "created": 0, "patches": []}

    def handler(request):
        path = request.url.path
        if path.endswith("/create-webhook"):
            body = json.loads(request.content)
            state["created"] += 1
            state["registered"] = list(body["addresses"])
            return httpx.Response(
                200, json={"data": {"id": "wh_new", "signing_key": "sk"}}
            )
        if path.endswith("/webhook-addresses"):
            return httpx.Response(
                200, json={"data": state["registered"], "pagination": {}}
            )
        if path.endswith("/update-webhook-addresses"):
            body = json.loads(request.content)
            state["patches"].append(body)
            for address in body["addresses_to_add"]:
                state["registered"].append(address)
            for address in body["addresses_to_remove"]:
                state["registered"].remove(address)
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected call to {path}")

    monkeypatch.setattr(
        main.alchemy, "AlchemyNotifyClient", lambda token: _client_with(handler)
    )

    first = api.post("/api/settings/realtime/sync").json()["synced"][0]
    assert state["created"] == 1
    assert first["addresses"] == 3
    assert sorted(state["registered"]) == sorted(wallets)

    # Drop a wallet: the next sync removes it from Alchemy, without recreating
    # the webhook.
    api.patch(
        f"/api/watchlists/{created['id']}", json={"remove_wallets": [wallets[0]]}
    )
    second = api.post("/api/settings/realtime/sync").json()["synced"][0]

    assert state["created"] == 1
    assert second["addresses"] == 2
    assert wallets[0] not in state["registered"]


def test_sync_deletes_the_webhook_when_nothing_is_live(api, monkeypatch):
    api.put(
        "/api/settings/realtime",
        json={"auth_token": "tok", "public_base_url": "https://dice.example"},
    )
    db.save_webhook(
        chain="ethereum",
        network="ETH_MAINNET",
        webhook_id="wh_old",
        signing_key="sk",
        webhook_url="https://dice.example/api/webhooks/alchemy",
        address_count=5,
    )
    deleted = []

    def handler(request):
        if request.url.path.endswith("/delete-webhook"):
            deleted.append(request.url.params.get("webhook_id"))
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected call to {request.url.path}")

    monkeypatch.setattr(
        main.alchemy, "AlchemyNotifyClient", lambda token: _client_with(handler)
    )

    api.post("/api/settings/realtime/sync")

    assert deleted == ["wh_old"]
    assert db.get_webhook("ethereum") is None


def _live_watchlist_on(api, chain, wallets):
    created = api.post(
        "/api/watchlists",
        json={
            "name": chain,
            "chain": chain,
            "wallets": wallets,
            "min_wallets": 2,
            "min_wallets_pct": 0,
        },
    ).json()
    db.update_watchlist_fields(created["id"], {"realtime": 1})
    return created["id"]


def test_unsupported_chain_is_reported(api, monkeypatch):
    api.put(
        "/api/settings/realtime",
        json={"auth_token": "tok", "public_base_url": "https://dice.example"},
    )
    _live_watchlist_on(api, "flare", ["0x" + "a" * 40, "0x" + "b" * 40])

    result = api.post("/api/settings/realtime/sync").json()["synced"][0]

    assert "not wired up for flare" in result["error"]


def test_solana_asks_for_the_provider_that_actually_covers_it(api, monkeypatch):
    """Alchemy Notify is EVM-only; saying "unsupported" would be misleading."""
    api.put(
        "/api/settings/realtime",
        json={"auth_token": "tok", "public_base_url": "https://dice.example"},
    )
    _live_watchlist_on(api, "solana", ["9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"])

    result = api.post("/api/settings/realtime/sync").json()["synced"][0]

    assert "Helius" in result["error"]
