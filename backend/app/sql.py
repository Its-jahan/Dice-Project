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

from .models import Chain, HoldersRequest
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
