"""Source resolution, exercised against a real Dune catalogue.

The fixtures below mirror what ``information_schema`` actually returned for a
live Dune account: the plain ``balances_ethereum`` schema carries only internal
tables, the usable ``daily_updates`` lives in a rotating
``__spellbook_sqlmesh_NNN`` build schema, and some chains expose the older
dense ``erc20_day`` shape instead.
"""

import pytest

from app.models import Chain
from app.source import Source, SourceNotFound, resolve_source, schema_pattern

INTERVAL_COLUMNS = {
    "address",
    "token_address",
    "balance",
    "balance_usd",
    "valid_from",
    "valid_to",
}
DENSE_COLUMNS = {"day", "wallet_address", "token_address", "amount"}


def test_prefers_the_plain_schema_when_it_carries_the_table():
    source = resolve_source(
        {
            "balances_ethereum.daily_updates": INTERVAL_COLUMNS,
            "balances_ethereum__spellbook_sqlmesh_490.daily_updates": INTERVAL_COLUMNS,
        }
    )

    assert source.qualified == "balances_ethereum.daily_updates"
    assert source.shape == "interval"


def test_falls_back_to_the_newest_build_schema():
    """The real ethereum catalogue: no daily_updates in the plain schema."""
    source = resolve_source(
        {
            "balances_ethereum.stg_daily_updates": INTERVAL_COLUMNS,
            "balances_ethereum__spellbook_sqlmesh_469.daily_updates": INTERVAL_COLUMNS,
            "balances_ethereum__spellbook_sqlmesh_482.daily_updates": INTERVAL_COLUMNS,
            "balances_ethereum__spellbook_sqlmesh_490.daily_updates": INTERVAL_COLUMNS,
        }
    )

    # highest build number wins — it is the freshest rebuild
    assert source.qualified == "balances_ethereum__spellbook_sqlmesh_490.daily_updates"
    assert source.shape == "interval"


def test_reads_the_dense_shape_when_that_is_what_the_chain_offers():
    """The real polygon catalogue exposes erc20_day, not daily_updates."""
    source = resolve_source({"balances_polygon.erc20_day": DENSE_COLUMNS})

    assert source.qualified == "balances_polygon.erc20_day"
    assert source.shape == "daily"
    assert (source.address, source.token, source.balance, source.day) == (
        "wallet_address",
        "token_address",
        "amount",
        "day",
    )


def test_interval_shape_is_preferred_over_dense_when_both_exist():
    source = resolve_source(
        {
            "balances_arbitrum.erc20_day": DENSE_COLUMNS,
            "balances_arbitrum__spellbook_sqlmesh_490.daily_updates": INTERVAL_COLUMNS,
        }
    )

    assert source.table == "daily_updates"


def test_ignores_internal_bookkeeping_tables():
    """raw_updates, triggers, hotfix_gaps and friends are never a source."""
    with pytest.raises(SourceNotFound):
        resolve_source(
            {
                "balances_ethereum.raw_updates": INTERVAL_COLUMNS,
                "balances_ethereum.triggers": INTERVAL_COLUMNS,
                "balances_ethereum.hotfix_gaps": INTERVAL_COLUMNS,
                "balances_ethereum.genesis_balances": INTERVAL_COLUMNS,
            }
        )


def test_skips_a_candidate_missing_a_required_column():
    with pytest.raises(SourceNotFound):
        resolve_source({"balances_ethereum.daily_updates": {"address", "valid_from"}})


def test_empty_catalog_explains_the_plan_problem():
    with pytest.raises(SourceNotFound, match="plan"):
        resolve_source({})


def test_column_aliases_are_resolved_per_table():
    source = resolve_source(
        {
            "solana_utils.daily_balances": {
                "address",
                "token_mint_address",
                "token_balance",
                "day",
            }
        }
    )

    assert source.shape == "daily"
    assert source.token == "token_mint_address"
    assert source.balance == "token_balance"


@pytest.mark.parametrize(
    "chain,expected",
    [
        (Chain.ethereum, "balances_ethereum%"),
        (Chain.polygon, "balances_polygon%"),
        (Chain.solana, "solana%"),
    ],
)
def test_schema_pattern_covers_build_schemas(chain, expected):
    assert schema_pattern(chain) == expected


def test_qualified_name_round_trips():
    source = Source(
        schema="balances_base",
        table="daily_updates",
        shape="interval",
        address="address",
        token="token_address",
        balance="balance",
        valid_from="valid_from",
        valid_to="valid_to",
    )
    assert source.qualified == "balances_base.daily_updates"


def test_contract_source_prefers_creation_traces():
    """Every deployed contract appears there; a decoded mapping covers less."""
    from app.source import resolve_contract_source

    source = resolve_contract_source(
        Chain.ethereum,
        {
            "ethereum.creation_traces": {"address", "block_time"},
            "contracts.contract_mapping": {"contract_address", "blockchain"},
        },
    )

    assert source.qualified == "ethereum.creation_traces"
    assert source.address == "address"
    assert source.blockchain is None


def test_contract_source_falls_back_and_notes_the_chain_column():
    from app.source import resolve_contract_source

    source = resolve_contract_source(
        Chain.ethereum,
        {"contracts.contract_mapping": {"contract_address", "blockchain"}},
    )

    assert source.qualified == "contracts.contract_mapping"
    assert source.address == "contract_address"
    assert source.blockchain == "blockchain"


def test_contract_source_missing_fails_loudly():
    """Silently returning contracts the caller excluded would be worse."""
    from app.source import resolve_contract_source

    with pytest.raises(SourceNotFound, match="Include smart-contract"):
        resolve_contract_source(Chain.ethereum, {})
