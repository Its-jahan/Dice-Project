"""End-to-end API tests with a stubbed Dune client (no network, no credits)."""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.dune import DuneAuthError
from app.sql import DISCOVERY_LIMIT

TOKEN = "0x1234567890abcdef1234567890abcdef12345678"
WALLET_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

BASE_REQUEST = {
    "chain": "ethereum",
    "token_address": TOKEN,
    "start_date": "2026-07-20",
    "end_date": "2026-07-21",
}


class FakeDuneClient:
    """Records what the backend asked Dune to do."""

    calls: dict = {}
    rows: list[dict] = []
    valid_key = True

    def __init__(self, api_key, **_kwargs):
        if not api_key or not api_key.strip():
            raise DuneAuthError("no Dune API key supplied")
        FakeDuneClient.calls["api_key"] = api_key

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def validate_key(self):
        return FakeDuneClient.valid_key

    async def create_query(self, *, name, query_sql):
        FakeDuneClient.calls["query_sql"] = query_sql
        FakeDuneClient.calls["query_name"] = name
        if "information_schema" not in query_sql:
            FakeDuneClient.calls["data_sql"] = query_sql
        return 4242

    async def execute_query(self, query_id, *, parameters=None, performance="medium"):
        FakeDuneClient.calls["query_id"] = query_id
        FakeDuneClient.calls["parameters"] = parameters
        return "exec-abc"

    async def wait_for_execution(self, execution_id):
        return {"state": "QUERY_STATE_COMPLETED"}

    catalog_rows: list[dict] = []

    async def fetch_results(self, execution_id, *, max_rows=None):
        sql = FakeDuneClient.calls.get("query_sql", "")
        if "information_schema.columns" in sql:
            return FakeDuneClient.catalog_rows, False
        return FakeDuneClient.rows, False


@pytest.fixture
def client(monkeypatch):
    FakeDuneClient.calls = {}
    FakeDuneClient.valid_key = True
    FakeDuneClient.rows = [
        {"wallet_address": WALLET_A, "day": "2026-07-20", "balance": 1540.25},
        {"wallet_address": WALLET_A, "day": "2026-07-21", "balance": 1300.0},
        {"wallet_address": WALLET_B, "day": "2026-07-21", "balance": 82.1},
    ]
    # A catalogue shaped like the real one: the usable table lives in a
    # rotating build schema, not the plain one.
    FakeDuneClient.catalog_rows = [
        {
            "table_schema": "balances_ethereum__spellbook_sqlmesh_490",
            "table_name": "daily_updates",
            "column_name": column,
        }
        for column in ("address", "token_address", "balance", "valid_from", "valid_to")
    ]
    main._SOURCE_CACHE.clear()
    monkeypatch.setattr(main, "DuneClient", FakeDuneClient)
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "dune_query_id", None, raising=False)
    with TestClient(main.app) as test_client:
        yield test_client


def test_holders_requires_an_api_key(client):
    response = client.post("/api/holders", json=BASE_REQUEST)

    assert response.status_code == 401
    assert "API key" in response.json()["detail"]


def test_header_key_is_used_and_never_echoed_back(client):
    response = client.post(
        "/api/holders", json=BASE_REQUEST, headers={"X-Dune-Api-Key": "secret-key"}
    )

    assert response.status_code == 200
    assert FakeDuneClient.calls["api_key"] == "secret-key"
    assert "secret-key" not in response.text


def test_config_reports_key_presence_but_not_the_key(client, monkeypatch):
    monkeypatch.setattr(settings, "dune_api_key", "server-side-secret", raising=False)

    body = client.get("/api/config").json()

    assert body["server_key_configured"] is True
    assert "server-side-secret" not in client.get("/api/config").text


def test_key_validation_endpoint(client):
    ok = client.post("/api/key/validate", headers={"X-Dune-Api-Key": "k"})
    assert ok.json() == {"valid": True}

    FakeDuneClient.valid_key = False
    bad = client.post("/api/key/validate", headers={"X-Dune-Api-Key": "k"})
    assert bad.json() == {"valid": False}


def test_holders_returns_preview_and_downloadable_job(client):
    response = client.post(
        "/api/holders", json=BASE_REQUEST, headers={"X-Dune-Api-Key": "k"}
    )
    body = response.json()

    assert body["row_count"] == 3
    assert body["wallet_count"] == 2
    assert body["execution_id"] == "exec-abc"
    assert len(body["preview"]) == 3

    export = client.get(f"/api/export/{body['job_id']}?format=csv")
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    assert WALLET_A in export.text


@pytest.mark.parametrize("fmt", ["csv", "xlsx", "json", "html"])
def test_every_export_format_downloads(client, fmt):
    job_id = client.post(
        "/api/holders", json=BASE_REQUEST, headers={"X-Dune-Api-Key": "k"}
    ).json()["job_id"]

    response = client.get(f"/api/export/{job_id}?format={fmt}")

    assert response.status_code == 200
    assert len(response.content) > 0
    assert f".{fmt}" in response.headers["content-disposition"]


def test_unknown_job_id_is_a_clean_404(client):
    assert client.get("/api/export/does-not-exist?format=csv").status_code == 404


def test_ad_hoc_mode_creates_a_query_from_generated_sql(client):
    client.post("/api/holders", json=BASE_REQUEST, headers={"X-Dune-Api-Key": "k"})

    sql = FakeDuneClient.calls["data_sql"]
    assert "balances_ethereum__spellbook_sqlmesh_490.daily_updates" in sql
    assert FakeDuneClient.calls["query_id"] == 4242


def test_saved_query_mode_passes_parameters_instead(client, monkeypatch):
    monkeypatch.setattr(settings, "dune_query_id", 999, raising=False)

    client.post("/api/holders", json=BASE_REQUEST, headers={"X-Dune-Api-Key": "k"})

    assert FakeDuneClient.calls["query_id"] == 999
    assert FakeDuneClient.calls["parameters"]["token_address"] == TOKEN
    assert "query_sql" not in FakeDuneClient.calls


def test_sql_preview_uses_the_resolved_source(client):
    response = client.post(
        "/api/sql", json=BASE_REQUEST, headers={"X-Dune-Api-Key": "k"}
    )

    body = response.json()
    assert response.status_code == 200
    assert body["source"] == "balances_ethereum__spellbook_sqlmesh_490.daily_updates"
    assert body["shape"] == "interval"
    assert body["source"] in body["sql"]


def test_invalid_request_is_rejected_before_touching_dune(client):
    response = client.post(
        "/api/holders",
        json={**BASE_REQUEST, "token_address": "nope"},
        headers={"X-Dune-Api-Key": "k"},
    )

    assert response.status_code == 422
    assert "api_key" not in FakeDuneClient.calls


def test_discover_lists_reachable_tables_and_what_dice_expects(client):
    FakeDuneClient.rows = [
        {"table_schema": "balances_ethereum", "table_name": "latest"},
        {"table_schema": "tokens_ethereum", "table_name": "balances_daily"},
    ]

    body = client.get("/api/discover", headers={"X-Dune-Api-Key": "k"}).json()

    assert body["count"] == 2
    assert body["truncated"] is False
    assert "balances_ethereum.latest" in body["tables"]

    sql = FakeDuneClient.calls["query_sql"]
    assert "information_schema.tables" in sql
    # Match on the schema, not free text: Dune hosts a vast number of decoded
    # per-contract tables and "%balance%" on table_name drowns in them.
    assert "lower(table_schema) LIKE 'balances%'" in sql
    assert r"NOT LIKE '%\_call\_%'" in sql
    assert r"NOT LIKE '%\_evt\_%'" in sql


def test_discover_rejects_a_pattern_that_could_break_out_of_the_sql(client):
    response = client.get(
        "/api/discover", params={"pattern": "x' OR '1'='1"}, headers={"X-Dune-Api-Key": "k"}
    )

    assert response.status_code == 422
    assert "query_sql" not in FakeDuneClient.calls


def test_source_endpoint_reports_the_resolved_table_and_columns(client):
    body = client.get(
        "/api/source", params={"chain": "ethereum"}, headers={"X-Dune-Api-Key": "k"}
    ).json()

    assert body["table"] == "balances_ethereum__spellbook_sqlmesh_490.daily_updates"
    assert body["shape"] == "interval"
    assert body["columns"]["token"] == "token_address"


def test_source_resolution_is_cached_across_requests(client):
    client.get("/api/source", headers={"X-Dune-Api-Key": "k"})
    first = FakeDuneClient.calls["query_name"]
    client.get("/api/source", headers={"X-Dune-Api-Key": "k"})

    # second call did not issue another catalogue query
    assert FakeDuneClient.calls["query_name"] == first


def test_no_readable_table_is_a_clear_404(client):
    FakeDuneClient.catalog_rows = []
    main._SOURCE_CACHE.clear()

    response = client.post(
        "/api/holders", json=BASE_REQUEST, headers={"X-Dune-Api-Key": "k"}
    )

    assert response.status_code == 404
    assert "plan" in response.json()["detail"]


def test_discover_flags_a_truncated_listing(client):
    """A silently truncated list reads as "this is everything", and is not."""
    FakeDuneClient.rows = [
        {"table_schema": f"s{i:04d}", "table_name": "t"} for i in range(DISCOVERY_LIMIT)
    ]

    body = client.get("/api/discover", headers={"X-Dune-Api-Key": "k"}).json()

    assert body["truncated"] is True
