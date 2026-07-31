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


def test_evm_sql_targets_the_per_chain_balances_schema():
    sql = build_snapshot_sql(make(min_balance=100))
    assert "balances_ethereum.daily_updates" in sql
    assert "b.token_address = '" + EVM_TOKEN.lower() + "'" in sql
    assert "b.balance > 100.0" in sql
    assert "date '2026-07-20'" in sql and "date '2026-07-31'" in sql
    # burn sinks excluded by default
    assert "0x0000000000000000000000000000000000000000" in sql
    # transfers must never be the source of truth for "who held"
    assert "transfers" not in sql.lower()
    # the chain lives in the schema name now, so there is no blockchain column
    assert "b.blockchain" not in sql


def test_evm_sql_expands_validity_intervals_into_one_row_per_day():
    """The sparse table must be joined against a calendar.

    Without this, a wallet that bought before the window and held through it —
    a single row spanning the whole range — would yield one row instead of one
    per day, which is precisely the holder DICE exists to find.
    """
    sql = build_snapshot_sql(make())

    assert "sequence(" in sql and "interval '1' day" in sql
    assert "CROSS JOIN calendar cal" in sql
    assert "b.valid_from <= cal.day" in sql
    # valid_to is exclusive, and NULL means the balance is still current
    assert "(b.valid_to IS NULL OR b.valid_to > cal.day)" in sql
    assert "cal.day         AS snapshot_date" in sql


def test_evm_sql_prunes_intervals_before_the_calendar_join():
    sql = build_snapshot_sql(make())

    assert "b.valid_from <= date '2026-07-31'" in sql
    assert "(b.valid_to IS NULL OR b.valid_to > date '2026-07-20')" in sql


@pytest.mark.parametrize(
    "chain,expected",
    [
        ("ethereum", "balances_ethereum.daily_updates"),
        ("base", "balances_base.daily_updates"),
        ("arbitrum", "balances_arbitrum.daily_updates"),
        ("polygon", "balances_polygon.daily_updates"),
        ("optimism", "balances_optimism.daily_updates"),
    ],
)
def test_every_evm_chain_maps_to_its_own_schema(chain, expected):
    assert expected in build_snapshot_sql(make(chain=chain))


def test_contract_filter_only_joins_when_excluding_contracts():
    assert "contract_mapping" not in build_snapshot_sql(make(include_contracts=True))
    assert "contract_mapping" in build_snapshot_sql(make(include_contracts=False))


def test_solana_sql_aggregates_per_owner():
    sql = build_snapshot_sql(make(chain="solana", token_address=SOL_MINT))
    assert "solana_utils.daily_balances" in sql
    assert "SUM(b.token_balance)" in sql
    assert "GROUP BY" in sql
    assert SOL_MINT in sql
    # dense daily table — no interval expansion needed
    assert "valid_from" not in sql


def test_columns_probe_targets_the_same_table_as_the_real_query():
    from app.sql import build_columns_sql

    for chain in ("ethereum", "base", "solana"):
        token = SOL_MINT if chain == "solana" else EVM_TOKEN
        req = make(chain=chain, token_address=token)
        table = build_columns_sql(req.chain).split(" FROM ")[1].split(" LIMIT")[0]
        assert table in build_snapshot_sql(req)


def test_query_parameters_are_serialisable_for_saved_queries():
    params = build_query_parameters(make(min_balance=50))
    assert params == {
        "blockchain": "ethereum",
        "token_address": EVM_TOKEN.lower(),
        "start_date": "2026-07-20",
        "end_date": "2026-07-31",
        "minimum_balance": 50.0,
    }
