"""Builds the DuneSQL that produces daily balance snapshots.

Design note
-----------
DICE always asks Dune for *daily end-of-day balances*, never for transfers.
A wallet that received the token before ``start_date`` and simply held it
through the window emits no transfers inside the window, so a transfer-based
query would silently drop exactly the holders we care about most.

The three holder modes (daily / any_time / continuous) are all derived from the
same daily snapshot set in :mod:`app.holders`, so one Dune execution answers
all of them and we only pay for the query once.

Table shapes
------------
There is no fixed table name to read. :mod:`app.source` resolves one from
Dune's catalogue at runtime and hands it here as a
:class:`~app.source.Source`, which also carries the column names. Two shapes
are generated:

``interval``
    Sparse rows carrying ``[valid_from, valid_to)``. A holder who does nothing
    for a month is a *single* row spanning that month, so the query cross joins
    a generated calendar to expand each interval into one row per day. That is
    exactly the case that matters — and the reason a transfer-based query gets
    it wrong.

``daily``
    Dense rows already keyed by a day column, summed per address and day.
"""

from __future__ import annotations

import re

from .models import EVM_ADDRESS_RE, SOLANA_MINT_RE, Chain, HoldersRequest
from .source import (
    TABLE_PREFERENCE,
    ContractSource,
    Source,
    contract_candidates,
    schema_pattern,
)

# Sinks that hold tokens but are never "holders" in any meaningful sense.
EVM_BURN_ADDRESSES = (
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
)
SOLANA_BURN_ADDRESSES = ("11111111111111111111111111111111",)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _address_literal(chain: Chain, address: str) -> str:
    """Render an address the way the engine's column type expects.

    EVM address columns are ``varbinary``, and DuneSQL compares those against
    bare hex literals — ``0xabc``, not ``'0xabc'``. Quoting one yields
    "Cannot apply operator: varbinary = varchar". Solana addresses are base58
    text, so those stay quoted.
    """
    return _quote(address) if chain is Chain.solana else address


def _address_list(chain: Chain, addresses: tuple[str, ...]) -> str:
    return ", ".join(_address_literal(chain, a) for a in addresses)


def _where(filters: list[str], indent: str) -> str:
    """Join predicates into an indented ``AND`` chain."""
    return f"\n{indent}  AND ".join(filters)


def build_catalog_sql(chain: Chain) -> str:
    """Read the columns of every balance table this key can see for a chain.

    One query answers both open questions at once — which table exists, and
    what its columns are called — so :func:`app.source.resolve_source` can pick
    a table instead of DICE hard-coding a name that keeps moving.
    """
    return f"""
-- DICE: catalogue of readable balance tables for {chain.value}
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE lower(table_schema) LIKE '{schema_pattern(chain)}'
  AND table_name IN ({", ".join(_quote(t) for t in TABLE_PREFERENCE)})
ORDER BY table_schema, table_name, column_name
LIMIT 2000
""".strip()


def build_contracts_catalog_sql(chain: Chain) -> str:
    """Columns of any table that could identify smart-contract addresses."""
    pairs = " OR ".join(
        f"(table_schema = {_quote(schema)} AND table_name = {_quote(table)})"
        for schema, table in contract_candidates(chain)
    )
    return f"""
-- DICE: readable contract-identifying tables for {chain.value}
SELECT table_schema, table_name, column_name
FROM information_schema.columns
WHERE {pairs}
ORDER BY table_schema, table_name, column_name
LIMIT 500
""".strip()


def build_snapshot_sql(
    req: HoldersRequest,
    source: Source,
    contracts: ContractSource | None = None,
) -> str:
    """Return DuneSQL yielding ``wallet_address, snapshot_date, balance``.

    Every value interpolated here has already been validated by
    :class:`~app.models.HoldersRequest` (addresses against a strict regex,
    dates as ``datetime.date``); column and table names come from
    ``information_schema`` via :class:`~app.source.Source`. Neither path
    carries free text from the caller.
    """
    if source.shape == "interval":
        return _interval_sql(req, source, contracts)
    return _daily_sql(req, source, contracts)


def _calendar_cte(req: HoldersRequest) -> str:
    """One row per day in the requested range, in UTC."""
    return f"""WITH calendar AS (
    SELECT day
    FROM UNNEST(
        sequence(
            date {_quote(req.start_date.isoformat())},
            date {_quote(req.effective_end_date.isoformat())},
            interval '1' day
        )
    ) AS t(day)
)"""


def _burn_addresses(req: HoldersRequest) -> tuple[str, ...]:
    return SOLANA_BURN_ADDRESSES if req.chain is Chain.solana else EVM_BURN_ADDRESSES


def _contract_filter(
    req: HoldersRequest, src: Source, contracts: ContractSource | None
) -> str:
    """Optional predicate dropping smart-contract addresses.

    Applied only when the caller unticks "include smart-contract addresses",
    so the default path depends on no extra table. The table and its column
    names are resolved from the catalogue, like the balance source — an
    earlier hard-coded ``contracts.contract_mapping.address`` did not exist
    and failed at execution time.

    Written as NOT EXISTS rather than a LEFT JOIN: an address redeployed via
    CREATE2 appears in creation_traces more than once, and a join would
    multiply rows before the filter removed them.
    """
    if req.include_contracts or contracts is None:
        return ""
    predicates = [f"c.{contracts.address} = b.{src.address}"]
    if contracts.blockchain:
        predicates.append(f"c.{contracts.blockchain} = {_quote(req.chain.value)}")
    conditions = "\n          AND ".join(predicates)
    return (
        f"NOT EXISTS (\n"
        f"        SELECT 1 FROM {contracts.qualified} c\n"
        f"        WHERE {conditions}\n"
        f"      )"
    )


def _interval_sql(
    req: HoldersRequest, src: Source, contracts: ContractSource | None
) -> str:
    """Sparse table: expand each ``[valid_from, valid_to)`` over the calendar."""
    start = f"date {_quote(req.start_date.isoformat())}"
    end = f"date {_quote(req.effective_end_date.isoformat())}"

    # A holder "on day D" means: holding at the *end* of day D, which is the
    # instant D + 1 day. Testing against the end of the day rather than its
    # start matters when valid_from carries a time: a wallet that bought at
    # 14:00 on the 21st holds at end-of-day on the 21st, but `valid_from <=
    # date '2026-07-21'` (i.e. midnight) would miss it and report the 22nd as
    # its first day. Written this way the predicate is correct whether the
    # column is a date or a timestamp.
    day_end = f"cal.day + interval '1' day"
    filters = [
        f"b.{src.token} = {_address_literal(req.chain, req.token_address)}",
        # Narrow to intervals that can overlap the window at all, so the engine
        # prunes before the calendar cross join rather than after it.
        f"b.{src.valid_from} < {end} + interval '1' day",
        f"(b.{src.valid_to} IS NULL OR b.{src.valid_to} > {start})",
        # Expand each interval to the days it covers. valid_to is exclusive,
        # and NULL means the balance is still current.
        f"b.{src.valid_from} < {day_end}",
        f"(b.{src.valid_to} IS NULL OR b.{src.valid_to} >= {day_end})",
        f"b.{src.balance} > {req.min_balance!r}",
    ]
    if req.exclude_burn_addresses:
        filters.append(
            f"b.{src.address} NOT IN "
            f"({_address_list(req.chain, _burn_addresses(req))})"
        )
    contract_filter = _contract_filter(req, src, contracts)
    if contract_filter:
        filters.append(contract_filter)

    return f"""
-- DICE: historical holders (daily end-of-day balances)
-- {src.qualified} is sparse: each row is a balance that held over
-- [{src.valid_from}, {src.valid_to}). The calendar join expands those
-- intervals into the one-row-per-day shape DICE exports.
{_calendar_cte(req)}
SELECT
    b.{src.address} AS wallet_address,
    b.{src.token} AS token_address,
    cal.day AS snapshot_date,
    b.{src.balance} AS balance
FROM {src.qualified} b
CROSS JOIN calendar cal
WHERE {_where(filters, "")}
ORDER BY cal.day, b.{src.balance} DESC
""".strip()


def _daily_sql(
    req: HoldersRequest, src: Source, contracts: ContractSource | None
) -> str:
    """Dense table: one row per address, token and day already.

    Balances are summed per address and day. On Solana that is essential —
    one wallet can own several token accounts for a mint, and without the sum
    a single holder appears as several fragmented rows. On EVM the group-by is
    a no-op over an already-unique key, so one code path serves both.
    """
    filters = [
        f"b.{src.token} = {_address_literal(req.chain, req.token_address)}",
        f"b.{src.day} >= date {_quote(req.start_date.isoformat())}",
        f"b.{src.day} <= date {_quote(req.effective_end_date.isoformat())}",
    ]
    if req.exclude_burn_addresses:
        filters.append(
            f"b.{src.address} NOT IN "
            f"({_address_list(req.chain, _burn_addresses(req))})"
        )
    contract_filter = _contract_filter(req, src, contracts)
    if contract_filter:
        filters.append(contract_filter)

    return f"""
-- DICE: historical holders (daily end-of-day balances)
-- {src.qualified} already has one row per address, token and day,
-- so no interval expansion is needed here.
SELECT
    wallet_address,
    token_address,
    snapshot_date,
    balance
FROM (
    SELECT
        b.{src.address} AS wallet_address,
        b.{src.token} AS token_address,
        b.{src.day} AS snapshot_date,
        SUM(b.{src.balance}) AS balance
    FROM {src.qualified} b
    WHERE {_where(filters, "    ")}
    GROUP BY 1, 2, 3
)
WHERE balance > {req.min_balance!r}
ORDER BY snapshot_date, balance DESC
""".strip()


#: Discovery patterns are interpolated into SQL, so they are restricted to a
#: conservative character set rather than escaped.
_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,40}$")

#: Curated namespaces where Dune keeps balance data. Matching on the *schema*
#: is essential: Dune hosts hundreds of thousands of user-decoded contract
#: tables, and a free-text search for "balance" drowns in `*_call_balanceof`.
CURATED_SCHEMA_PREFIXES = ("balances", "tokens", "solana_utils")

#: Row cap for the discovery query, shared with the API so the two cannot drift.
DISCOVERY_LIMIT = 500


def build_discovery_sql(pattern: str | None = None) -> str:
    """Ask Dune which balance tables this API key can actually see.

    Dune's balance tables are Open Beta: they get renamed, and some are gated
    behind plan tiers, which surfaces as "does not exist or it is private".
    Rather than guessing at names, this reads ``information_schema`` — which
    only lists what the caller is entitled to — and reports the truth.

    Decoded contract tables (``*_call_*``, ``*_evt_*``) are excluded: they are
    per-contract ABI decodings, never a source of balances, and there are far
    too many of them to page through.
    """
    if pattern is not None:
        if not _SAFE_PATTERN.match(pattern):
            raise ValueError("pattern must be 1-40 characters of [A-Za-z0-9_]")
        schema_match = f"lower(table_schema) LIKE '%{pattern.lower()}%'"
    else:
        schema_match = "\n       OR ".join(
            f"lower(table_schema) LIKE '{prefix}%'"
            for prefix in CURATED_SCHEMA_PREFIXES
        )

    return f"""
-- DICE: which curated balance tables can this key see?
SELECT table_schema, table_name
FROM information_schema.tables
WHERE ({schema_match})
  AND table_name NOT LIKE '%\\_call\\_%' ESCAPE '\\'
  AND table_name NOT LIKE '%\\_evt\\_%' ESCAPE '\\'
ORDER BY table_schema, table_name
LIMIT {DISCOVERY_LIMIT}
""".strip()


def build_query_parameters(req: HoldersRequest) -> dict[str, str | float]:
    """Parameter map for the *saved query* execution path.

    Used when ``DUNE_QUERY_ID`` is configured: the saved query declares these
    parameter names and DICE only supplies values.
    """
    return {
        "blockchain": req.chain.value,
        "token_address": req.token_address,
        "start_date": req.start_date.isoformat(),
        "end_date": req.end_date.isoformat(),
        "minimum_balance": req.min_balance,
    }


# --------------------------------------------------------- watchlist monitoring
#
# The monitor asks a different question than the holder query: not "who held
# token X" but "what did these wallets *buy* recently". That comes from Dune's
# curated DEX trade tables (``dex.trades`` / ``dex_solana.trades``), where a
# buy is a swap whose bought side is the token in question.
#
# Type note: in DuneSQL the EVM address columns of dex.trades (taker,
# token_bought_address, ...) are VARBINARY, so literals are written unquoted
# (0xabc...); Solana identifiers are base58 VARCHAR and quoted normally.

#: Quote currencies, stables and liquid staking tokens per chain. Every wallet
#: constantly "buys" these as the other leg of ordinary swaps; counting them
#: would fire a co-buy signal on USDC every single run. Chains without an entry
#: fall back to no built-in stoplist — add per-watchlist ignores there instead.
DEFAULT_IGNORED_TOKENS: dict[Chain, tuple[str, ...]] = {
    Chain.ethereum: (
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
        "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
        "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC
        "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0",  # wstETH
        "0xae7ab96520de3a18e5e111b5eaab095312d7fe84",  # stETH
        "0x4c9edd5852cd905f086c759e8383e09bff1e68b3",  # USDe
    ),
    Chain.base: (
        "0x4200000000000000000000000000000000000006",  # WETH
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
        "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",  # cbETH
        "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
        "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452",  # wstETH
    ),
    Chain.arbitrum: (
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC
        "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",  # USDC.e
        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
        "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1",  # DAI
        "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f",  # WBTC
    ),
    Chain.optimism: (
        "0x4200000000000000000000000000000000000006",  # WETH
        "0x0b2c639c533813f4aa9d7837caf62653d097ff85",  # USDC
        "0x7f5c764cbc14f9669b88837ca1490cca17c31607",  # USDC.e
        "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58",  # USDT
        "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1",  # DAI
        "0x68f180fcce6836688e9084f035309e29bf0a2095",  # WBTC
    ),
    Chain.polygon: (
        "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",  # WPOL (ex-WMATIC)
        "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619",  # WETH
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # USDC
        "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # USDC.e
        "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",  # USDT
        "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",  # DAI
        "0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6",  # WBTC
    ),
    Chain.bnb: (
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
        "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
        "0x2170ed0880ac9a755fd29b2688956bd959f933f8",  # ETH (peg)
        "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",  # BTCB
    ),
    Chain.avalanche_c: (
        "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7",  # WAVAX
        "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e",  # USDC
        "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7",  # USDT
        "0x49d5c2bdffac6ce2bfdb6640f4f80f226bc10bab",  # WETH.e
        "0xd586e7f844cea2f87f50152665bcbc2c279d8d70",  # DAI.e
    ),
    Chain.solana: (
        "So11111111111111111111111111111111111111112",  # wSOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
        "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # jitoSOL
    ),
}


def ignored_tokens_for(chain: Chain, extra: list[str] | None = None) -> list[str]:
    """Built-in stoplist plus user additions, deduplicated and format-checked."""
    tokens: list[str] = list(DEFAULT_IGNORED_TOKENS.get(chain, ()))
    pattern = EVM_ADDRESS_RE if chain.is_evm else SOLANA_MINT_RE
    for raw in extra or []:
        token = raw.strip()
        if chain.is_evm:
            token = token.lower()
        if pattern.match(token) and token not in tokens:
            tokens.append(token)
    return tokens


def build_trades_sql(
    chain: Chain,
    wallets: list[str],
    *,
    window_hours: int,
    extra_ignore_tokens: list[str] | None = None,
) -> str:
    """DuneSQL: what did these wallets buy on DEXes in the last N hours?

    Returns one row per (wallet, token bought) with buy count, USD volume and
    the first/last buy time inside the window. Wallets and token filters are
    interpolated only after passing the strict address regexes, exactly like
    the holder query.
    """
    if not wallets:
        raise ValueError("build_trades_sql needs at least one wallet")
    window_hours = int(window_hours)
    ignore = ignored_tokens_for(chain, extra_ignore_tokens)

    wallet_pattern = EVM_ADDRESS_RE if chain.is_evm else SOLANA_MINT_RE
    for wallet in wallets:
        if not wallet_pattern.match(wallet):
            raise ValueError(f"invalid {chain.value} wallet address: {wallet!r}")

    wallet_list = ",\n        ".join(_address_literal(chain, w) for w in wallets)
    ignore_list = ", ".join(_address_literal(chain, t) for t in ignore)

    if chain.is_evm:
        return f"""
-- DICE: watchlist DEX buys, last {window_hours}h ({chain.value})
SELECT
    t.taker                    AS wallet_address,
    t.token_bought_address     AS token_address,
    MAX(t.token_bought_symbol) AS token_symbol,
    COUNT(*)                   AS buy_count,
    SUM(t.amount_usd)          AS amount_usd,
    MIN(t.block_time)          AS first_buy_at,
    MAX(t.block_time)          AS last_buy_at
FROM dex.trades t
WHERE t.blockchain = {_quote(chain.value)}
  AND t.block_time >= now() - interval '{window_hours}' hour
  AND t.taker IN (
        {wallet_list})
  AND t.token_bought_address NOT IN ({ignore_list})
GROUP BY 1, 2
ORDER BY amount_usd DESC
""".strip()

    return f"""
-- DICE: watchlist DEX buys, last {window_hours}h (solana)
SELECT
    t.trader_id                   AS wallet_address,
    t.token_bought_mint_address   AS token_address,
    MAX(t.token_bought_symbol)    AS token_symbol,
    COUNT(*)                      AS buy_count,
    SUM(t.amount_usd)             AS amount_usd,
    MIN(t.block_time)             AS first_buy_at,
    MAX(t.block_time)             AS last_buy_at
FROM dex_solana.trades t
WHERE t.block_time >= now() - interval '{window_hours}' hour
  AND t.trader_id IN (
        {wallet_list})
  AND t.token_bought_mint_address NOT IN ({ignore_list})
GROUP BY 1, 2
ORDER BY amount_usd DESC
""".strip()
