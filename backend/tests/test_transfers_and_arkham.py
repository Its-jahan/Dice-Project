"""Transfer-based history reconstruction, and Arkham address labelling."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import arkham, db, main
from app.cache import DiskCache
from app.config import settings
from app.dune import DuneAuthError
from app.jobs import JobStore
from app.models import Chain, HoldersRequest
from app.sql import build_transfer_snapshot_sql

TOKEN = "0x6f40d4a6237c257fff2db00fa0510deeecd303eb"
HEADERS = {"X-Dune-Api-Key": "k"}
WALLETS = [f"0x{i:040x}" for i in range(1, 4)]


# ------------------------------------------------- transfer reconstruction


def _request(**overrides):
    return HoldersRequest(
        chain="ethereum",
        token_address=TOKEN,
        start_date="2023-01-07",
        end_date="2023-01-08",
        **overrides,
    )


def test_transfer_sql_reconstructs_balances_from_both_directions():
    sql = build_transfer_snapshot_sql(_request())

    assert "tokens.transfers" in sql
    # A balance is inflows minus outflows, so the token's transfers are read
    # twice with opposite signs.
    assert 't."to" AS wallet' in sql
    assert 't."from" AS wallet' in sql
    assert "-t.amount AS delta" in sql
    # Everything before the window collapses into an opening balance.
    assert "opening AS (" in sql
    assert "block_time < date '2023-01-06'" in sql   # the baseline day
    # And the running total is carried across the calendar.
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in sql
    assert "t.blockchain = 'ethereum'" in sql


def test_transfer_sql_emits_the_same_shape_as_the_balance_query():
    sql = build_transfer_snapshot_sql(_request())

    # Downstream code must not care which source produced the rows.
    for column in ("wallet_address", "token_address", "snapshot_date", "balance"):
        assert column in sql


def test_transfer_sql_applies_the_minimum_and_burn_filters():
    sql = build_transfer_snapshot_sql(_request(min_balance=250))

    assert "WHERE balance > 250.0" in sql
    assert "wallet NOT IN (0x0000000000000000000000000000000000000000" in sql

    kept = build_transfer_snapshot_sql(_request(exclude_burn_addresses=False))
    assert "wallet NOT IN" not in kept


def test_solana_transfer_sql_quotes_the_mint():
    sql = build_transfer_snapshot_sql(
        HoldersRequest(
            chain="solana",
            token_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            start_date="2023-01-07",
            end_date="2023-01-08",
        )
    )

    assert "'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'" in sql


# ------------------------------------------------------------- the fallback


class FakeDuneClient:
    balance_rows: list[dict] = []
    transfer_rows: list[dict] = []
    catalog_rows: list[dict] = []
    sqls: list[str] = []

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
        FakeDuneClient.last_sql = query_sql
        FakeDuneClient.sqls.append(query_sql)
        return 1

    async def update_query(self, query_id, *, name=None, query_sql=None):
        if query_sql is not None:
            FakeDuneClient.last_sql = query_sql
            FakeDuneClient.sqls.append(query_sql)

    async def execute_query(self, query_id, *, parameters=None, performance="medium"):
        return "exec"

    async def wait_for_execution(self, execution_id):
        return {"state": "QUERY_STATE_COMPLETED"}

    async def fetch_results(self, execution_id, *, max_rows=None):
        sql = getattr(FakeDuneClient, "last_sql", "")
        if "information_schema.columns" in sql:
            return FakeDuneClient.catalog_rows, False
        if "tokens.transfers" in sql:
            return FakeDuneClient.transfer_rows, False
        return FakeDuneClient.balance_rows, False


@pytest.fixture
def client(monkeypatch, tmp_path):
    FakeDuneClient.balance_rows = []
    FakeDuneClient.transfer_rows = []
    FakeDuneClient.sqls = []
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


def test_auto_falls_back_to_transfers_when_balances_are_empty(client):
    """The exact failure reported: a range the balance table does not cover."""
    FakeDuneClient.balance_rows = []
    FakeDuneClient.transfer_rows = [
        _row(WALLETS[0], "2023-01-06", 500),
        _row(WALLETS[0], "2023-01-07", 500),
        _row(WALLETS[0], "2023-01-08", 500),
    ]

    body = _run(client)

    assert body["stages"]["source"] == "transfers"
    assert body["wallet_count"] == 1
    assert any("tokens.transfers" in sql for sql in FakeDuneClient.sqls)


def test_auto_does_not_pay_for_transfers_when_balances_answer(client):
    FakeDuneClient.balance_rows = [_row(WALLETS[0], "2023-01-07", 500)]

    body = _run(client)

    assert body["stages"]["source"] == "balances"
    # The expensive query must not run when the cheap one worked.
    assert not any("tokens.transfers" in sql for sql in FakeDuneClient.sqls)


def test_balances_only_never_falls_back(client):
    FakeDuneClient.balance_rows = []
    FakeDuneClient.transfer_rows = [_row(WALLETS[0], "2023-01-07", 500)]

    body = _run(client, history_source="balances")

    assert body["stages"]["source"] == "balances"
    assert body["wallet_count"] == 0
    assert not any("tokens.transfers" in sql for sql in FakeDuneClient.sqls)


def test_transfers_can_be_forced(client):
    FakeDuneClient.balance_rows = [_row(WALLETS[0], "2023-01-07", 999)]
    FakeDuneClient.transfer_rows = [_row(WALLETS[1], "2023-01-07", 500)]

    body = _run(client, history_source="transfers")

    assert body["stages"]["source"] == "transfers"
    assert body["preview"][0]["wallet_address"] == WALLETS[1]


def test_reconstructed_rows_classify_like_any_other(client):
    """Buyer/holder logic must not care which source produced the rows."""
    FakeDuneClient.balance_rows = []
    FakeDuneClient.transfer_rows = [
        _row(WALLETS[0], "2023-01-06", 500),   # flat: a holder
        _row(WALLETS[0], "2023-01-07", 500),
        _row(WALLETS[0], "2023-01-08", 500),
        _row(WALLETS[1], "2023-01-08", 900),   # appears inside: a buyer
    ]

    body = _run(client)

    assert body["stages"]["holders"] == 1
    assert body["stages"]["buyers"] == 1


# ------------------------------------------------------------------ arkham


def _mock_arkham(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(arkham.httpx, "AsyncClient", factory)


def test_arkham_key_roundtrip_and_masking(client):
    assert client.get("/api/settings/arkham").json()["configured"] is False

    saved = client.put(
        "/api/settings/arkham", json={"api_key": "arkham-secret-key-9876"}
    ).json()

    assert saved["configured"] is True
    assert saved["key_hint"] == "…9876"
    assert "secret" not in str(saved)


def test_arkham_lookup_returns_the_entity(client, monkeypatch):
    client.put("/api/settings/arkham", json={"api_key": "k"})

    def handler(request):
        assert request.headers["API-Key"] == "k"
        return httpx.Response(
            200,
            json={
                "arkhamEntity": {"name": "Binance", "type": "CEX"},
                "arkhamLabel": {"name": "Binance Hot Wallet 7"},
            },
        )

    _mock_arkham(monkeypatch, handler)

    body = client.get(f"/api/arkham/address?address={WALLETS[0]}").json()

    assert body["entity"] == "Binance"
    assert body["label"] == "Binance Hot Wallet 7"
    # An exchange wallet is a customer deposit, not a conviction buy.
    assert body["is_service"] is True


def test_arkham_parses_a_chain_nested_response(client, monkeypatch):
    """Arkham nests the body per chain in some responses and not others."""
    client.put("/api/settings/arkham", json={"api_key": "k"})

    def handler(_request):
        return httpx.Response(
            200, json={"ethereum": {"arkhamEntity": {"name": "Jump Trading"}}}
        )

    _mock_arkham(monkeypatch, handler)

    body = client.get(f"/api/arkham/address?address={WALLETS[0]}").json()

    assert body["entity"] == "Jump Trading"
    assert body["is_service"] is False


def test_arkham_unknown_shape_yields_empty_labels_not_an_error():
    # A shape we have never seen must not raise: a missing label is not a
    # reason to fail a signal.
    assert arkham.describe_address({"something": "else"}, Chain.ethereum) == {
        "entity": None,
        "entity_type": None,
        "label": None,
        "is_service": False,
    }
    assert arkham.describe_address(None, Chain.ethereum)["entity"] is None


def test_arkham_raw_mode_returns_the_untouched_payload(client, monkeypatch):
    client.put("/api/settings/arkham", json={"api_key": "k"})
    payload = {"weird": {"nested": ["shape"]}}
    _mock_arkham(monkeypatch, lambda _r: httpx.Response(200, json=payload))

    body = client.get(
        f"/api/arkham/address?address={WALLETS[0]}&raw=true"
    ).json()

    assert body["raw"] == payload


def test_arkham_without_a_key_is_a_clean_401(client):
    response = client.get(f"/api/arkham/address?address={WALLETS[0]}")

    assert response.status_code == 401
    assert "No Arkham API key" in response.json()["detail"]


def test_arkham_rejected_key_explains_where_to_get_one(client, monkeypatch):
    client.put("/api/settings/arkham", json={"api_key": "bad"})
    _mock_arkham(
        monkeypatch,
        lambda _r: httpx.Response(401, json={"message": "Could not verify API Key"}),
    )

    response = client.get(f"/api/arkham/address?address={WALLETS[0]}")

    assert response.status_code == 401
    assert "intel.arkm.com" in response.json()["detail"]
