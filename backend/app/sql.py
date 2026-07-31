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

from .models import Chain, HoldersRequest

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
