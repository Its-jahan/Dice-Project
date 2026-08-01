import pytest
from pydantic import ValidationError

from app.models import Chain, HoldersRequest
from app.source import Source
from app.sql import (
    build_catalog_sql,
    build_query_parameters,
    build_snapshot_sql,
)

EVM_TOKEN = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
SOL_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def make(**overrides):
    payload = {
        "chain": "ethereum",
        "token_address": EVM_TOKEN,
        "start_date": "2026-07-20",
        "end_date": "2026-07-31",
    }
    payload.update(overrides)
    return HoldersRequest(**payload)


def test_evm_address_is_normalised_to_lowercase():
    assert make().token_address == EVM_TOKEN.lower()


def test_rejects_bad_addresses_and_ranges():
    with pytest.raises(ValidationError):
        make(token_address="not-an-address")
    with pytest.raises(ValidationError):
        make(chain="solana", token_address=EVM_TOKEN)
    with pytest.raises(ValidationError):
        make(start_date="2026-07-31", end_date="2026-07-20")
    with pytest.raises(ValidationError):
        make(start_date="2020-01-01", end_date="2026-07-31")  # over the day cap


def test_solana_mint_accepted():
    req = make(chain="solana", token_address=SOL_MINT)
    assert req.chain is Chain.solana
    assert req.token_address == SOL_MINT  # base58 is case-sensitive, not lowered


def test_days_counts_both_endpoints():
    assert make(start_date="2026-07-20", end_date="2026-07-22").days == 3


INTERVAL_SOURCE = Source(
    schema="balances_ethereum__spellbook_sqlmesh_490",
    table="daily_updates",
    shape="interval",
    address="address",
    token="token_address",
    balance="balance",
    valid_from="valid_from",
    valid_to="valid_to",
)
DENSE_SOURCE = Source(
    schema="balances_polygon",
    table="erc20_day",
    shape="daily",
    address="wallet_address",
    token="token_address",
    balance="amount",
    day="day",
)


def test_interval_sql_reads_the_resolved_table_and_columns():
    sql = build_snapshot_sql(make(min_balance=100), INTERVAL_SOURCE)

    assert "FROM balances_ethereum__spellbook_sqlmesh_490.daily_updates b" in sql
    assert "b.token_address = '" + EVM_TOKEN.lower() + "'" in sql
    assert "b.balance > 100.0" in sql
    # transfers must never be the source of truth for "who held"
    assert "transfers" not in sql.lower()
    # the chain lives in the schema name, so there is no blockchain column
    assert "b.blockchain" not in sql


def test_interval_sql_expands_validity_intervals_into_one_row_per_day():
    """The sparse table must be joined against a calendar.

    Without this, a wallet that bought before the window and held through it —
    a single row spanning the whole range — would yield one row instead of one
    per day, which is precisely the holder DICE exists to find.
    """
    sql = build_snapshot_sql(make(), INTERVAL_SOURCE)

    assert "sequence(" in sql and "interval '1' day" in sql
    assert "CROSS JOIN calendar cal" in sql
    assert "b.valid_from <= cal.day" in sql
    # valid_to is exclusive, and NULL means the balance is still current
    assert "(b.valid_to IS NULL OR b.valid_to > cal.day)" in sql
    assert "cal.day AS snapshot_date" in sql


def test_interval_sql_prunes_before_the_calendar_join():
    sql = build_snapshot_sql(make(), INTERVAL_SOURCE)

    assert "b.valid_from <= date '2026-07-31'" in sql
    assert "(b.valid_to IS NULL OR b.valid_to > date '2026-07-20')" in sql


def test_dense_sql_uses_the_day_column_and_needs_no_calendar():
    sql = build_snapshot_sql(make(min_balance=5), DENSE_SOURCE)

    assert "FROM balances_polygon.erc20_day b" in sql
    assert "b.day >= date '2026-07-20'" in sql
    assert "SUM(b.amount) AS balance" in sql
    assert "b.wallet_address AS wallet_address" in sql
    assert "valid_from" not in sql
    assert "sequence(" not in sql
    # the minimum applies after summing, so split holdings are not lost
    assert "WHERE balance > 5.0" in sql


def test_column_names_come_from_the_source_not_from_assumptions():
    odd = Source(
        schema="solana_utils",
        table="daily_balances",
        shape="daily",
        address="owner",
        token="token_mint_address",
        balance="token_balance",
        day="block_date",
    )

    sql = build_snapshot_sql(make(chain="solana", token_address=SOL_MINT), odd)

    assert "b.owner AS wallet_address" in sql
    assert "SUM(b.token_balance)" in sql
    assert "b.block_date" in sql
    assert SOL_MINT in sql


@pytest.mark.parametrize("source", [INTERVAL_SOURCE, DENSE_SOURCE])
def test_contract_filter_only_joins_when_excluding_contracts(source):
    assert "contract_mapping" not in build_snapshot_sql(
        make(include_contracts=True), source
    )

    excluded = build_snapshot_sql(make(include_contracts=False), source)
    assert "contract_mapping" in excluded
    assert "cm.address IS NULL" in excluded


def test_catalog_sql_covers_build_schemas_and_only_candidate_tables():
    sql = build_catalog_sql(Chain.ethereum)

    assert "information_schema.columns" in sql
    # trailing % so rotating __spellbook_sqlmesh_NNN schemas are included
    assert "LIKE 'balances_ethereum%'" in sql
    assert "'daily_updates'" in sql
    assert "'erc20_day'" in sql


def test_query_parameters_are_serialisable_for_saved_queries():
    params = build_query_parameters(make(min_balance=50))
    assert params == {
        "blockchain": "ethereum",
        "token_address": EVM_TOKEN.lower(),
        "start_date": "2026-07-20",
        "end_date": "2026-07-31",
        "minimum_balance": 50.0,
    }
