"""Watchlist/monitor/signal API tests with a stubbed Dune client and a tmp DB."""

import pytest
from fastapi.testclient import TestClient

from app import main
from app import monitor as monitor_module
from app.cache import DiskCache
from app.config import settings
from app.dune import DuneAuthError
from app.jobs import JobStore

TOKEN = "0x1234567890abcdef1234567890abcdef12345678"
GEM = "0x9999999999999999999999999999999999999999"
WALLETS = [f"0x{i:040x}" for i in range(1, 7)]  # six valid EVM addresses

HEADERS = {"X-Dune-Api-Key": "test-key"}


class FakeDuneClient:
    """Serves canned rows for holder, catalogue and monitor trade queries."""

    rows: list[dict] = []
    catalog_rows: list[dict] = []
    calls: dict = {}

    def __init__(self, api_key, **_kwargs):
        if not api_key or not api_key.strip():
            raise DuneAuthError("no Dune API key supplied")
        FakeDuneClient.calls["api_key"] = api_key

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def validate_key(self):
        return True

    async def create_query(self, *, name, query_sql):
        FakeDuneClient.calls["query_name"] = name
        FakeDuneClient.calls["query_sql"] = query_sql
        if "information_schema" not in query_sql:
            FakeDuneClient.calls["data_sql"] = query_sql
        return 777

    async def execute_query(self, query_id, *, parameters=None, performance="medium"):
        return "exec-monitor"

    async def wait_for_execution(self, execution_id):
        return {"state": "QUERY_STATE_COMPLETED"}

    async def fetch_results(self, execution_id, *, max_rows=None):
        sql = FakeDuneClient.calls.get("query_sql", "")
        if "information_schema.columns" in sql:
            return FakeDuneClient.catalog_rows, False
        return FakeDuneClient.rows, False


def _trade_row(wallet, token=GEM, usd=1000.0, symbol="GEM"):
    return {
        "wallet_address": wallet,
        "token_address": token,
        "token_symbol": symbol,
        "buy_count": 2,
        "amount_usd": usd,
        "first_buy_at": "2026-08-01 08:00:00.000 UTC",
        "last_buy_at": "2026-08-01 09:30:00.000 UTC",
    }


@pytest.fixture
def client(monkeypatch, tmp_path):
    FakeDuneClient.rows = []
    FakeDuneClient.calls = {}
    # Catalogue rows so /api/holders can resolve a balance source (from-job).
    FakeDuneClient.catalog_rows = [
        {
            "table_schema": "balances_ethereum__spellbook_sqlmesh_490",
            "table_name": "daily_updates",
            "column_name": column,
        }
        for column in ("address", "token_address", "balance", "valid_from", "valid_to")
    ]
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice-test.db"))
    monkeypatch.setattr(settings, "monitor_enabled", False)  # no background loop
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "dune_query_id", None, raising=False)
    monkeypatch.setattr(settings, "monitor_max_wallets", 2000)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))
    monkeypatch.setattr(main, "DuneClient", FakeDuneClient)
    monkeypatch.setattr(monitor_module, "DuneClient", FakeDuneClient)
    with TestClient(main.app) as test_client:
        yield test_client


def _create_watchlist(client, **overrides):
    body = {
        "name": "early buyers",
        "chain": "ethereum",
        "wallets": WALLETS,
        "source_token_address": TOKEN,
        "min_wallets": 3,
        "min_wallets_pct": 0,
        **overrides,
    }
    response = client.post("/api/watchlists", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ----------------------------------------------------------------------- CRUD


def test_create_and_list_watchlists(client):
    created = _create_watchlist(client)

    assert created["wallet_count"] == len(WALLETS)
    assert created["effective_min_wallets"] == 3
    # the source token is auto-ignored so it cannot fire a trivial signal
    assert TOKEN in created["ignore_tokens"]
    assert created["auto_monitor"] is True
    assert created["next_run_at"] is not None

    listed = client.get("/api/watchlists").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "early buyers"

    wallets = client.get(f"/api/watchlists/{created['id']}/wallets").json()
    assert wallets["wallet_count"] == len(WALLETS)
    assert set(wallets["wallets"]) == set(WALLETS)


def test_create_rejects_bad_wallets_and_too_many(client, monkeypatch):
    bad = client.post(
        "/api/watchlists",
        json={"name": "x", "chain": "ethereum", "wallets": ["nope"]},
    )
    assert bad.status_code == 422

    monkeypatch.setattr(settings, "monitor_max_wallets", 3)
    over = client.post(
        "/api/watchlists",
        json={"name": "x", "chain": "ethereum", "wallets": WALLETS},
    )
    assert over.status_code == 422
    assert "cap" in over.json()["detail"]


def test_update_settings_and_wallets(client):
    watchlist = _create_watchlist(client)
    watchlist_id = watchlist["id"]

    new_wallet = "0x" + "f" * 40
    response = client.patch(
        f"/api/watchlists/{watchlist_id}",
        json={
            "min_wallets": 4,
            "min_wallets_pct": 50,
            "monitor_interval_hours": 2,
            "auto_monitor": False,
            "add_wallets": [new_wallet.upper().replace("0X", "0x")],
            "remove_wallets": [WALLETS[0]],
        },
    )
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["min_wallets"] == 4
    assert body["monitor_interval_hours"] == 2
    assert body["auto_monitor"] is False
    assert body["wallet_count"] == len(WALLETS)  # one added, one removed
    # 50% of 6 wallets = 3, absolute floor 4 wins
    assert body["effective_min_wallets"] == 4

    wallets = client.get(f"/api/watchlists/{watchlist_id}/wallets").json()["wallets"]
    assert new_wallet in wallets and WALLETS[0] not in wallets

    bad = client.patch(
        f"/api/watchlists/{watchlist_id}", json={"add_wallets": ["garbage"]}
    )
    assert bad.status_code == 422


def test_delete_watchlist(client):
    watchlist_id = _create_watchlist(client)["id"]

    assert client.delete(f"/api/watchlists/{watchlist_id}").status_code == 204
    assert client.delete(f"/api/watchlists/{watchlist_id}").status_code == 404
    assert client.get("/api/watchlists").json() == []


# ------------------------------------------------------------------- from-job


def test_watchlist_from_holders_job(client):
    FakeDuneClient.rows = [
        {"wallet_address": w, "day": "2026-07-20", "balance": 100.0 + i}
        for i, w in enumerate(WALLETS)
    ]
    job = client.post(
        "/api/holders",
        json={
            "chain": "ethereum",
            "token_address": TOKEN,
            "start_date": "2026-07-20",
            "end_date": "2026-07-21",
        },
        headers=HEADERS,
    ).json()

    created = client.post(
        f"/api/watchlists/from-job/{job['job_id']}",
        json={"top_n": 4, "min_wallets": 2, "min_wallets_pct": 0},
    )
    body = created.json()

    assert created.status_code == 201, created.text
    assert body["wallet_count"] == 4
    assert body["source_token_address"] == TOKEN
    assert TOKEN in body["ignore_tokens"]
    assert body["chain"] == "ethereum"

    missing = client.post("/api/watchlists/from-job/unknown", json={})
    assert missing.status_code == 404


# ------------------------------------------------------------------ monitoring


def test_monitor_run_creates_signals_and_updates_them(client):
    watchlist_id = _create_watchlist(client)["id"]

    # 4 of 6 wallets bought GEM (threshold 3); one bought another token (below)
    FakeDuneClient.rows = [_trade_row(w) for w in WALLETS[:4]] + [
        _trade_row(WALLETS[0], token="0x" + "8" * 40, symbol="MEH")
    ]
    result = client.post(
        f"/api/watchlists/{watchlist_id}/monitor", headers=HEADERS
    ).json()

    assert result["run"]["status"] == "ok"
    assert result["run"]["wallets_checked"] == len(WALLETS)
    assert result["run"]["buy_rows"] == 5
    assert result["run"]["signals_fired"] == 1
    assert len(result["new_signals"]) == 1
    signal = result["new_signals"][0]
    assert signal["token_address"] == GEM
    assert signal["wallet_count"] == 4
    assert signal["watchlist_size"] == len(WALLETS)
    assert len(signal["buyers"]) == 4

    # monitor SQL went through the trades table with the source token ignored
    assert "dex.trades" in FakeDuneClient.calls["query_sql"]
    assert TOKEN in FakeDuneClient.calls["query_sql"]

    # a second run with one more buyer updates the same signal in place
    FakeDuneClient.rows = [_trade_row(w) for w in WALLETS[:5]]
    second = client.post(
        f"/api/watchlists/{watchlist_id}/monitor", headers=HEADERS
    ).json()

    assert second["new_signals"] == []
    assert len(second["updated_signals"]) == 1
    assert second["updated_signals"][0]["wallet_count"] == 5

    signals = client.get("/api/signals").json()
    assert len(signals) == 1
    assert signals[0]["wallet_count"] == 5
    assert signals[0]["watchlist_name"] == "early buyers"

    runs = client.get(f"/api/watchlists/{watchlist_id}/runs").json()
    assert [run["status"] for run in runs] == ["ok", "ok"]


def test_monitor_requires_key_and_valid_watchlist(client):
    watchlist_id = _create_watchlist(client)["id"]

    no_key = client.post(f"/api/watchlists/{watchlist_id}/monitor")
    assert no_key.status_code == 401

    missing = client.post("/api/watchlists/424242/monitor", headers=HEADERS)
    assert missing.status_code == 404


def test_dismissed_signal_stays_dismissed_on_retrigger(client):
    watchlist_id = _create_watchlist(client)["id"]
    FakeDuneClient.rows = [_trade_row(w) for w in WALLETS[:4]]
    first = client.post(
        f"/api/watchlists/{watchlist_id}/monitor", headers=HEADERS
    ).json()
    signal_id = first["new_signals"][0]["id"]

    dismissed = client.post(f"/api/signals/{signal_id}/dismiss").json()
    assert dismissed["status"] == "dismissed"
    assert client.get("/api/signals").json() == []

    # the token triggers again -> still dismissed, but stats refresh
    FakeDuneClient.rows = [_trade_row(w) for w in WALLETS[:5]]
    client.post(f"/api/watchlists/{watchlist_id}/monitor", headers=HEADERS)

    assert client.get("/api/signals").json() == []
    everything = client.get("/api/signals?include_dismissed=true").json()
    assert everything[0]["status"] == "dismissed"
    assert everything[0]["wallet_count"] == 5

    restored = client.post(f"/api/signals/{signal_id}/restore").json()
    assert restored["status"] == "active"
    assert len(client.get("/api/signals").json()) == 1


def test_monitor_error_is_recorded(client, monkeypatch):
    watchlist_id = _create_watchlist(client)["id"]

    async def boom(self, *, name, query_sql):
        raise RuntimeError("dune exploded")

    monkeypatch.setattr(FakeDuneClient, "create_query", boom)
    # TestClient re-raises unhandled server exceptions; what matters here is
    # that the run row still records the failure before the error propagates.
    with pytest.raises(RuntimeError, match="dune exploded"):
        client.post(f"/api/watchlists/{watchlist_id}/monitor", headers=HEADERS)

    runs = client.get(f"/api/watchlists/{watchlist_id}/runs").json()
    assert runs[0]["status"] == "error"
    assert "dune exploded" in runs[0]["error"]

    listed = client.get("/api/watchlists").json()[0]
    assert listed["last_run_status"] == "error"


def test_config_reports_monitor_block(client):
    config = client.get("/api/config").json()

    assert config["monitor"]["max_wallets"] == 2000
    assert config["monitor"]["auto_possible"] is False  # no server key in tests
