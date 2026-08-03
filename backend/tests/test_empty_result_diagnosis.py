"""An empty result must say which stage emptied it, not guess at a cause."""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.cache import DiskCache
from app.config import settings
from app.dune import DuneAuthError
from app.jobs import JobStore

TOKEN = "0x1234567890abcdef1234567890abcdef12345678"
HEADERS = {"X-Dune-Api-Key": "k"}
WALLETS = [f"0x{i:040x}" for i in range(1, 4)]


class FakeDuneClient:
    rows: list[dict] = []
    catalog_rows: list[dict] = []

    def __init__(self, api_key, **_kwargs):
        if not api_key or not api_key.strip():
            raise DuneAuthError("no key")
        self.key_fingerprint = "fp"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def validate_key(self):
        return True

    async def create_query(self, *, name, query_sql):
        self._sql = query_sql
        FakeDuneClient.last_sql = query_sql
        return 1

    async def update_query(self, query_id, *, name=None, query_sql=None):
        if query_sql is not None:
            FakeDuneClient.last_sql = query_sql

    async def execute_query(self, query_id, *, parameters=None, performance="medium"):
        return "exec-1"

    async def wait_for_execution(self, execution_id):
        return {"state": "QUERY_STATE_COMPLETED"}

    async def fetch_results(self, execution_id, *, max_rows=None):
        if "information_schema.columns" in getattr(FakeDuneClient, "last_sql", ""):
            return FakeDuneClient.catalog_rows, False
        return FakeDuneClient.rows, False


@pytest.fixture
def client(monkeypatch, tmp_path):
    FakeDuneClient.rows = []
    FakeDuneClient.last_sql = ""
    FakeDuneClient.catalog_rows = [
        {
            "table_schema": "balances_ethereum__spellbook_sqlmesh_490",
            "table_name": "daily_updates",
            "column_name": column,
        }
        for column in ("address", "token_address", "balance", "valid_from", "valid_to")
    ]
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))
    monkeypatch.setattr(main, "DuneClient", FakeDuneClient)
    with TestClient(main.app) as test_client:
        yield test_client


def _run(client, **overrides):
    body = {
        "chain": "ethereum",
        "token_address": TOKEN,
        "start_date": "2023-01-07",
        "end_date": "2023-01-08",
        **overrides,
    }
    return client.post("/api/holders", json=body, headers=HEADERS).json()


def _row(wallet, day, balance):
    return {"wallet_address": wallet, "day": day, "balance": balance}


def test_nothing_from_dune_is_distinguishable(client):
    FakeDuneClient.rows = []

    stages = _run(client)["stages"]

    assert stages["dune_rows"] == 0
    assert stages["after_min_balance"] == 0


def test_everything_below_the_minimum_is_distinguishable(client):
    FakeDuneClient.rows = [_row(WALLETS[0], "2023-01-07", 5)]

    stages = _run(client, min_balance=100000)["stages"]

    # Dune had data; the minimum balance is what emptied the result.
    assert stages["dune_rows"] == 1
    assert stages["after_min_balance"] == 0


def test_the_wallet_filter_emptying_the_result_is_distinguishable(client):
    # Two wallets, both of which only ever bought.
    FakeDuneClient.rows = [
        _row(WALLETS[0], "2023-01-07", 500),
        _row(WALLETS[1], "2023-01-08", 900),
    ]

    stages = _run(client, wallet_filter="holders")["stages"]

    assert stages["dune_rows"] == 2
    assert stages["wallets_in_range"] == 2
    assert stages["buyers"] == 2
    assert stages["holders"] == 0
    # The data was there; "Holders only" is what removed it.
    assert stages["after_wallet_filter"] == 0


def test_holder_mode_emptying_the_result_is_distinguishable(client):
    # Held on the baseline day and the first day, but not the second.
    FakeDuneClient.rows = [
        _row(WALLETS[0], "2023-01-06", 500),
        _row(WALLETS[0], "2023-01-07", 500),
    ]

    stages = _run(client, holder_mode="continuous")["stages"]

    assert stages["after_wallet_filter"] == 1
    assert stages["after_holder_mode"] == 0   # continuous needs every day


def test_a_successful_run_reports_consistent_stages(client):
    FakeDuneClient.rows = [
        _row(WALLETS[0], "2023-01-06", 500),
        _row(WALLETS[0], "2023-01-07", 500),
        _row(WALLETS[0], "2023-01-08", 500),
        _row(WALLETS[1], "2023-01-08", 900),
    ]

    body = _run(client)
    stages = body["stages"]

    assert body["wallet_count"] == 2
    assert stages["holders"] == 1          # flat across the range
    assert stages["buyers"] == 1           # appeared inside it
    assert stages["after_holder_mode"] == 2
