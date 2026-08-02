import pytest
from pydantic import ValidationError

from app.models import Chain, HoldersRequest
from app.source import ContractSource, Source
from app.sql import (
    build_catalog_sql,
    build_contracts_catalog_sql,
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
    assert "b.token_address = " + EVM_TOKEN.lower() in sql
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
    assert "cal.day AS snapshot_date" in sql


def test_membership_is_tested_at_end_of_day_not_at_midnight():
    """"Holder on day D" means holding at the *end* of D.

    If valid_from carries a time, a wallet that bought at 14:00 on the 21st
    holds at end-of-day on the 21st — but `valid_from <= date '2026-07-21'`
    compares against midnight and would report the 22nd as its first day,
    shifting every intra-day acquisition one day late.
    """
    sql = build_snapshot_sql(make(), INTERVAL_SOURCE)

    assert "b.valid_from < cal.day + interval '1' day" in sql
    assert (
        "(b.valid_to IS NULL OR b.valid_to >= cal.day + interval '1' day)" in sql
    )
    # the midnight comparison must be gone
    assert "b.valid_from <= cal.day" not in sql


def test_interval_sql_prunes_before_the_calendar_join():
    sql = build_snapshot_sql(make(), INTERVAL_SOURCE)

    assert "b.valid_from < date '2026-07-31' + interval '1' day" in sql
    # Bounded by the baseline day (start - 1), which is read so a buyer can be
    # told from a wallet that was already holding on day one.
    assert "(b.valid_to IS NULL OR b.valid_to > date '2026-07-19')" in sql


def test_dense_sql_uses_the_day_column_and_needs_no_calendar():
    sql = build_snapshot_sql(make(min_balance=5), DENSE_SOURCE)

    assert "FROM balances_polygon.erc20_day b" in sql
    assert "b.day >= date '2026-07-19'" in sql   # the baseline day
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


CONTRACT_SOURCE = ContractSource(
    schema="ethereum", table="creation_traces", address="address"
)
MULTICHAIN_CONTRACT_SOURCE = ContractSource(
    schema="contracts",
    table="contract_mapping",
    address="contract_address",
    blockchain="blockchain",
)


@pytest.mark.parametrize("source", [INTERVAL_SOURCE, DENSE_SOURCE])
def test_contract_filter_is_absent_unless_excluding_contracts(source):
    """The default path must not depend on any contract table existing."""
    sql = build_snapshot_sql(make(include_contracts=True), source, CONTRACT_SOURCE)

    assert "creation_traces" not in sql
    assert "NOT EXISTS" not in sql


@pytest.mark.parametrize("source", [INTERVAL_SOURCE, DENSE_SOURCE])
def test_contract_filter_uses_the_resolved_table_and_column(source):
    sql = build_snapshot_sql(make(include_contracts=False), source, CONTRACT_SOURCE)

    assert "NOT EXISTS" in sql
    assert "FROM ethereum.creation_traces c" in sql
    assert f"c.address = b.{source.address}" in sql
    # a LEFT JOIN would fan out on CREATE2 redeploys before the filter applied
    assert "LEFT JOIN" not in sql


def test_multichain_contract_table_is_filtered_by_chain():
    sql = build_snapshot_sql(
        make(include_contracts=False), INTERVAL_SOURCE, MULTICHAIN_CONTRACT_SOURCE
    )

    assert "c.contract_address = b.address" in sql
    assert "c.blockchain = 'ethereum'" in sql


def test_contracts_catalog_sql_probes_every_candidate():
    sql = build_contracts_catalog_sql(Chain.ethereum)

    assert "information_schema.columns" in sql
    assert "table_schema = 'ethereum'" in sql
    assert "'creation_traces'" in sql
    assert "'contract_mapping'" in sql


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


def test_evm_addresses_are_bare_hex_literals_not_quoted_strings():
    """EVM address columns are varbinary in DuneSQL.

    Quoting the literal yields "Cannot apply operator: varbinary = varchar",
    so addresses must be emitted as 0xabc, never '0xabc'.
    """
    sql = build_snapshot_sql(make(), INTERVAL_SOURCE)

    assert f"b.token_address = {EVM_TOKEN.lower()}" in sql
    assert f"'{EVM_TOKEN.lower()}'" not in sql
    # burn sinks go through the same path
    assert "(0x0000000000000000000000000000000000000000," in sql
    assert "'0x0000000000000000000000000000000000000000'" not in sql


def test_solana_addresses_stay_quoted_because_they_are_text():
    solana_source = Source(
        schema="solana_utils",
        table="daily_balances",
        shape="daily",
        address="address",
        token="token_mint_address",
        balance="token_balance",
        day="day",
    )

    sql = build_snapshot_sql(
        make(chain="solana", token_address=SOL_MINT), solana_source
    )

    assert f"b.token_mint_address = '{SOL_MINT}'" in sql
