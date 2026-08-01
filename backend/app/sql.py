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

Dune's curated balance tables are in Open Beta; if a table name or column
changes upstream, this module is the only place that needs editing.
"""

from __future__ import annotations

from .models import EVM_ADDRESS_RE, SOLANA_MINT_RE, Chain, HoldersRequest

# Sinks that hold tokens but are never "holders" in any meaningful sense.
EVM_BURN_ADDRESSES = (
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
)
SOLANA_BURN_ADDRESSES = ("11111111111111111111111111111111",)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _address_list(addresses: tuple[str, ...]) -> str:
    return ", ".join(_quote(a) for a in addresses)


def _where(filters: list[str], indent: str) -> str:
    """Join predicates into an indented ``AND`` chain."""
    return f"\n{indent}  AND ".join(filters)


def build_snapshot_sql(req: HoldersRequest) -> str:
    """Return DuneSQL yielding ``wallet_address, snapshot_date, balance``.

    Every value interpolated here has already been validated by
    :class:`~app.models.HoldersRequest` (addresses against a strict regex,
    dates as ``datetime.date``), so no attacker-controlled text reaches the
    query text.
    """
    return _solana_sql(req) if req.chain is Chain.solana else _evm_sql(req)


def _evm_sql(req: HoldersRequest) -> str:
    filters = [
        f"b.blockchain = {_quote(req.chain.value)}",
        f"b.token_address = {_quote(req.token_address)}",
        f"b.day >= date {_quote(req.start_date.isoformat())}",
        f"b.day <= date {_quote(req.end_date.isoformat())}",
        f"b.balance > {req.min_balance!r}",
    ]
    if req.exclude_burn_addresses:
        filters.append(f"b.address NOT IN ({_address_list(EVM_BURN_ADDRESSES)})")
    if not req.include_contracts:
        filters.append("c.address IS NULL")

    contract_join = (
        "\n  LEFT JOIN contracts.contract_mapping c\n"
        "         ON c.blockchain = b.blockchain\n"
        "        AND c.address = b.address"
        if not req.include_contracts
        else ""
    )

    return f"""
-- DICE: historical holders (daily end-of-day balances)
SELECT
    b.address        AS wallet_address,
    b.token_address  AS token_address,
    b.day            AS snapshot_date,
    b.balance        AS balance
FROM tokens.balances_daily b{contract_join}
WHERE {_where(filters, "")}
ORDER BY b.day, b.balance DESC
""".strip()


def _solana_sql(req: HoldersRequest) -> str:
    """Solana needs the *owner*, not the token account.

    A single wallet can control many token accounts for the same mint, so we
    sum per owner per day; otherwise one holder shows up as several rows with
    fragmented balances.
    """
    filters = [
        f"b.token_mint_address = {_quote(req.token_address)}",
        f"b.day >= date {_quote(req.start_date.isoformat())}",
        f"b.day <= date {_quote(req.end_date.isoformat())}",
    ]
    if req.exclude_burn_addresses:
        filters.append(f"b.address NOT IN ({_address_list(SOLANA_BURN_ADDRESSES)})")

    return f"""
-- DICE: historical holders (daily end-of-day balances, Solana owners)
SELECT
    wallet_address,
    token_address,
    snapshot_date,
    balance
FROM (
    SELECT
        b.address                 AS wallet_address,
        b.token_mint_address      AS token_address,
        b.day                     AS snapshot_date,
        SUM(b.balance)            AS balance
    FROM tokens_solana.balances_daily b
    WHERE {_where(filters, "    ")}
    GROUP BY 1, 2, 3
)
WHERE balance > {req.min_balance!r}
ORDER BY snapshot_date, balance DESC
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
#: would fire a co-buy signal on USDC every single run.
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
    Chain.solana: (
        "So11111111111111111111111111111111111111112",  # wSOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
        "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # jitoSOL
    ),
}

def _evm_literal(address: str) -> str:
    """Unquoted VARBINARY literal; only regex-validated hex ever reaches here."""
    return address.lower()


def ignored_tokens_for(chain: Chain, extra: list[str] | None = None) -> list[str]:
    """Built-in stoplist plus user additions, deduplicated and format-checked."""
    tokens: list[str] = list(DEFAULT_IGNORED_TOKENS[chain])
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

    if chain.is_evm:
        pattern = EVM_ADDRESS_RE
        for wallet in wallets:
            if not pattern.match(wallet):
                raise ValueError(f"invalid EVM wallet address: {wallet!r}")
        wallet_list = ",\n        ".join(_evm_literal(w) for w in wallets)
        ignore_list = ", ".join(_evm_literal(t) for t in ignore)
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

    for wallet in wallets:
        if not SOLANA_MINT_RE.match(wallet):
            raise ValueError(f"invalid Solana wallet address: {wallet!r}")
    wallet_list = ",\n        ".join(_quote(w) for w in wallets)
    ignore_list = ", ".join(_quote(t) for t in ignore)
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
