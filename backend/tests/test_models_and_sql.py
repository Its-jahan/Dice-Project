import pytest
from pydantic import ValidationError

from app.models import Chain, HoldersRequest
from app.sql import build_query_parameters, build_snapshot_sql

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


def test_evm_sql_targets_daily_balances_and_applies_filters():
    sql = build_snapshot_sql(make(min_balance=100))
    assert "tokens.balances_daily" in sql
    assert "b.token_address = '" + EVM_TOKEN.lower() + "'" in sql
    assert "b.balance > 100.0" in sql
    assert "date '2026-07-20'" in sql and "date '2026-07-31'" in sql
    # burn sinks excluded by default
    assert "0x0000000000000000000000000000000000000000" in sql
    # transfers must never be the source of truth for "who held"
    assert "transfers" not in sql.lower()


def test_contract_filter_only_joins_when_excluding_contracts():
    assert "contract_mapping" not in build_snapshot_sql(make(include_contracts=True))
    assert "contract_mapping" in build_snapshot_sql(make(include_contracts=False))


def test_solana_sql_aggregates_per_owner():
    sql = build_snapshot_sql(make(chain="solana", token_address=SOL_MINT))
    assert "tokens_solana.balances_daily" in sql
    assert "SUM(b.balance)" in sql
    assert "GROUP BY" in sql
    assert SOL_MINT in sql


def test_query_parameters_are_serialisable_for_saved_queries():
    params = build_query_parameters(make(min_balance=50))
    assert params == {
        "blockchain": "ethereum",
        "token_address": EVM_TOKEN.lower(),
        "start_date": "2026-07-20",
        "end_date": "2026-07-31",
        "minimum_balance": 50.0,
    }
