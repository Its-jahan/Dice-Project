"""Unit tests: trades SQL generation, trade-row parsing, signal thresholds."""

import pytest

from app.models import Chain
from app.monitor import (
    aggregate_candidates,
    effective_min_wallets,
    merge_buys,
    parse_position_rows,
    parse_trade_rows,
)
from app.source import Source
from app.sql import (
    DEFAULT_IGNORED_TOKENS,
    build_new_positions_sql,
    build_trades_sql,
    ignored_tokens_for,
)

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
DAILY_SOURCE = Source(
    schema="tokens_solana",
    table="balances_daily",
    shape="daily",
    address="address",
    token="token_mint_address",
    balance="balance",
    day="day",
)

WALLET_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TOKEN_1 = "0x1111111111111111111111111111111111111111"
TOKEN_2 = "0x2222222222222222222222222222222222222222"
SOL_WALLET_1 = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
SOL_WALLET_2 = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"


# ------------------------------------------------------------------- SQL: EVM


def test_evm_trades_sql_uses_unquoted_varbinary_literals():
    sql = build_trades_sql(Chain.base, [WALLET_A, WALLET_B], window_hours=48)

    assert "dex.trades" in sql
    assert "t.blockchain = 'base'" in sql
    assert "interval '48' hour" in sql
    # varbinary literals must NOT be quoted
    assert WALLET_A in sql and f"'{WALLET_A}'" not in sql
    # the built-in stoplist is applied (WETH on Base), unquoted as well
    weth_base = "0x4200000000000000000000000000000000000006"
    assert weth_base in sql and f"'{weth_base}'" not in sql
    assert "token_bought_address NOT IN" in sql


def test_evm_trades_sql_includes_extra_ignores_and_rejects_bad_wallets():
    sql = build_trades_sql(
        Chain.ethereum, [WALLET_A], window_hours=24, extra_ignore_tokens=[TOKEN_1]
    )
    assert TOKEN_1 in sql

    with pytest.raises(ValueError):
        build_trades_sql(Chain.ethereum, ["not-an-address"], window_hours=24)
    with pytest.raises(ValueError):
        build_trades_sql(Chain.ethereum, [], window_hours=24)


# ---------------------------------------------------------------- SQL: Solana


def test_solana_trades_sql_quotes_base58_strings():
    sql = build_trades_sql(
        Chain.solana, [SOL_WALLET_1, SOL_WALLET_2], window_hours=12
    )

    assert "dex_solana.trades" in sql
    assert "t.trader_id" in sql
    assert f"'{SOL_WALLET_1}'" in sql
    assert "interval '12' hour" in sql
    assert "'So11111111111111111111111111111111111111112'" in sql  # wSOL ignored


# -------------------------------------------------------- SQL: new positions


def test_interval_new_positions_sql_excludes_prior_holdings():
    sql = build_new_positions_sql(
        Chain.ethereum, INTERVAL_SOURCE, [WALLET_A, WALLET_B], window_hours=48
    )

    assert INTERVAL_SOURCE.qualified in sql
    assert "interval '48' hour" in sql
    # The anti-join is what makes it a *new* position rather than any holding.
    assert "NOT EXISTS" in sql
    assert "p.valid_from < now() - interval '48' hour" in sql
    assert WALLET_A in sql and f"'{WALLET_A}'" not in sql  # varbinary literal
    assert "dex.trades" not in sql


def test_daily_new_positions_sql_rounds_the_window_to_days():
    sql = build_new_positions_sql(
        Chain.solana, DAILY_SOURCE, [SOL_WALLET_1], window_hours=36
    )

    # 36h rounds up to 2 days on a day-granularity table.
    assert "date_add('day', -2, current_date)" in sql
    assert f"'{SOL_WALLET_1}'" in sql
    assert "'So11111111111111111111111111111111111111112'" in sql  # wSOL ignored
    assert "NOT EXISTS" in sql


def test_new_positions_sql_validates_wallets():
    with pytest.raises(ValueError):
        build_new_positions_sql(
            Chain.ethereum, INTERVAL_SOURCE, ["nope"], window_hours=24
        )
    with pytest.raises(ValueError):
        build_new_positions_sql(Chain.ethereum, INTERVAL_SOURCE, [], window_hours=24)


# ------------------------------------------------------------------- stoplist


def test_ignored_tokens_merges_normalises_and_drops_garbage():
    extra = [TOKEN_1.upper(), TOKEN_1, "garbage", ""]
    tokens = ignored_tokens_for(Chain.ethereum, extra)

    assert tokens.count(TOKEN_1) == 1  # deduped and lowercased
    assert "garbage" not in tokens
    for default in DEFAULT_IGNORED_TOKENS[Chain.ethereum]:
        assert default in tokens


# ------------------------------------------------------------------ threshold


def test_effective_min_wallets_combines_absolute_and_percentage():
    # 10% of 100 wallets = 10 beats the absolute floor of 5
    assert effective_min_wallets(5, 10.0, 100) == 10
    # small list: absolute floor wins
    assert effective_min_wallets(5, 10.0, 30) == 5
    # pct = 0 disables the percentage rule
    assert effective_min_wallets(7, 0.0, 1000) == 7
    # never below 2, whatever the inputs say
    assert effective_min_wallets(2, 0.0, 0) == 2


# ------------------------------------------------------------------- parsing


def test_parse_trade_rows_filters_and_normalises():
    rows = [
        {  # good row, checksummed address comes back lowercased
            "wallet_address": WALLET_A.upper().replace("0X", "0x"),
            "token_address": TOKEN_1,
            "token_symbol": "GEM",
            "buy_count": 3,
            "amount_usd": "1,250.50",
            "first_buy_at": "2026-08-01 10:22:33.000 UTC",
            "last_buy_at": "2026-08-01 18:00:00.000 UTC",
        },
        {  # wallet not on the watchlist -> dropped
            "wallet_address": "0xcccccccccccccccccccccccccccccccccccccccc",
            "token_address": TOKEN_1,
            "buy_count": 1,
        },
        {  # ignored token -> dropped
            "wallet_address": WALLET_A,
            "token_address": TOKEN_2,
            "buy_count": 1,
        },
        {"wallet_address": "", "token_address": TOKEN_1},  # unusable -> dropped
    ]

    buys = parse_trade_rows(
        rows,
        chain=Chain.ethereum,
        wallets=[WALLET_A, WALLET_B],
        ignored_tokens=[TOKEN_2],
    )

    assert len(buys) == 1
    buy = buys[0]
    assert buy["wallet_address"] == WALLET_A
    assert buy["amount_usd"] == pytest.approx(1250.50)
    assert buy["first_buy_at"] == "2026-08-01T10:22:33+00:00"
    assert buy["via"] == "dex"


def test_parse_position_rows_labels_balance_and_leaves_usd_unknown():
    rows = [
        {
            "wallet_address": WALLET_A.upper().replace("0X", "0x"),
            "token_address": TOKEN_1,
            "first_seen_at": "2026-08-01 07:00:00.000 UTC",
            "balance": 4200.0,
        },
        {"wallet_address": WALLET_A, "token_address": TOKEN_2},  # ignored token
    ]

    buys = parse_position_rows(
        rows, chain=Chain.ethereum, wallets=[WALLET_A], ignored_tokens=[TOKEN_2]
    )

    assert len(buys) == 1
    assert buys[0]["via"] == "balance"
    assert buys[0]["amount_usd"] is None
    assert buys[0]["first_buy_at"] == "2026-08-01T07:00:00+00:00"


def test_merge_buys_prefers_the_dex_record_for_the_same_pair():
    dex = [
        {
            "wallet_address": WALLET_A,
            "token_address": TOKEN_1,
            "token_symbol": "GEM",
            "buy_count": 2,
            "amount_usd": 900.0,
            "first_buy_at": None,
            "last_buy_at": None,
            "via": "dex",
        }
    ]
    positions = [
        {
            "wallet_address": WALLET_A,   # same pair, seen both ways
            "token_address": TOKEN_1,
            "token_symbol": None,
            "buy_count": 1,
            "amount_usd": None,
            "first_buy_at": None,
            "last_buy_at": None,
            "via": "balance",
        },
        {
            "wallet_address": WALLET_B,   # position only
            "token_address": TOKEN_1,
            "token_symbol": None,
            "buy_count": 1,
            "amount_usd": None,
            "first_buy_at": None,
            "last_buy_at": None,
            "via": "balance",
        },
    ]

    merged = {b["wallet_address"]: b for b in merge_buys(dex, positions)}

    assert len(merged) == 2  # not three: the duplicate pair collapsed
    assert merged[WALLET_A]["via"] == "dex"
    assert merged[WALLET_A]["amount_usd"] == pytest.approx(900.0)
    assert merged[WALLET_B]["via"] == "balance"


# --------------------------------------------------------------- aggregation


def _buy(wallet, token, usd=100.0, symbol="TKN"):
    return {
        "wallet_address": wallet,
        "token_address": token,
        "token_symbol": symbol,
        "buy_count": 1,
        "amount_usd": usd,
        "first_buy_at": "2026-08-01T10:00:00+00:00",
        "last_buy_at": "2026-08-01T11:00:00+00:00",
    }


def test_aggregate_candidates_fires_only_above_threshold():
    wallets = [f"0x{i:040x}" for i in range(10)]
    buys = [_buy(w, TOKEN_1) for w in wallets[:3]]  # 3 buyers of TOKEN_1
    buys.append(_buy(wallets[0], TOKEN_2))  # 1 buyer of TOKEN_2

    candidates = aggregate_candidates(
        buys, watchlist_size=10, min_wallets=3, min_wallets_pct=0, min_buy_usd=0
    )

    assert [c["token_address"] for c in candidates] == [TOKEN_1]
    assert candidates[0]["wallet_count"] == 3
    assert candidates[0]["total_usd"] == pytest.approx(300.0)
    assert len(candidates[0]["buyers"]) == 3


def test_aggregate_candidates_min_buy_usd_keeps_unknown_values():
    buys = [
        _buy(WALLET_A, TOKEN_1, usd=5.0),  # dust: known and below floor
        _buy(WALLET_B, TOKEN_1, usd=None),  # unknown price: kept
        _buy("0x" + "c" * 40, TOKEN_1, usd=500.0),
    ]

    candidates = aggregate_candidates(
        buys, watchlist_size=10, min_wallets=2, min_wallets_pct=0, min_buy_usd=50
    )

    assert candidates and candidates[0]["wallet_count"] == 2
    kept = {b["wallet_address"] for b in candidates[0]["buyers"]}
    assert WALLET_A not in kept and WALLET_B in kept


def test_aggregate_candidates_percentage_threshold():
    wallets = [f"0x{i:040x}" for i in range(9)]
    buys = [_buy(w, TOKEN_1) for w in wallets]  # 9 distinct buyers

    # 10% of 100 = 10 required -> 9 buyers is not enough
    assert not aggregate_candidates(
        buys, watchlist_size=100, min_wallets=5, min_wallets_pct=10, min_buy_usd=0
    )
    # on a 90-wallet list, 10% = 9 -> fires
    assert aggregate_candidates(
        buys, watchlist_size=90, min_wallets=5, min_wallets_pct=10, min_buy_usd=0
    )
