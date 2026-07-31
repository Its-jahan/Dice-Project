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
EVM balances live in a per-chain schema, ``balances_<chain>.daily_updates``.
That table is *sparse*: one row per balance change, carrying a validity
interval ``[valid_from, valid_to)`` rather than one row per day. A holder who
does nothing for a month is a single row spanning that month. So to get one row
per day we expand each interval against a generated calendar — which is exactly
the case that matters, and the reason a transfer-based query gets it wrong.

Solana balances live in ``solana_utils.daily_balances``, which *is* dense (one
row per address, mint and day), so it needs no interval expansion — only
aggregation per owner.

Dune's balance tables are in Open Beta. If a name changes upstream, this module
is the only place that needs editing, and ``GET /api/columns`` will tell you
what the table actually looks like now.
"""

from __future__ import annotations

import re

from .models import Chain, HoldersRequest

# Sinks that hold tokens but are never "holders" in any meaningful sense.
EVM_BURN_ADDRESSES = (
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
)
SOLANA_BURN_ADDRESSES = ("11111111111111111111111111111111",)

SOLANA_BALANCES_TABLE = "solana_utils.daily_balances"


def evm_table(chain: Chain) -> str:
    """Per-chain balances schema, e.g. ``balances_ethereum.daily_updates``."""
    return f"balances_{chain.value}.daily_updates"


def table_for(chain: Chain) -> str:
    return SOLANA_BALANCES_TABLE if chain is Chain.solana else evm_table(chain)


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


def _calendar_cte(req: HoldersRequest) -> str:
    """One row per day in the requested range, in UTC."""
    return f"""WITH calendar AS (
    SELECT day
    FROM UNNEST(
        sequence(
            date {_quote(req.start_date.isoformat())},
            date {_quote(req.end_date.isoformat())},
            interval '1' day
        )
    ) AS t(day)
)"""


def _evm_sql(req: HoldersRequest) -> str:
    start = f"date {_quote(req.start_date.isoformat())}"
    end = f"date {_quote(req.end_date.isoformat())}"

    filters = [
        f"b.token_address = {_quote(req.token_address)}",
        # Narrow to intervals that can overlap the window at all, so the engine
        # prunes before the calendar cross join rather than after it.
        f"b.valid_from <= {end}",
        f"(b.valid_to IS NULL OR b.valid_to > {start})",
        # Expand each interval to the days it actually covers. valid_to is
        # exclusive, and NULL means "still valid".
        "b.valid_from <= cal.day",
        "(b.valid_to IS NULL OR b.valid_to > cal.day)",
        f"b.balance > {req.min_balance!r}",
    ]
    if req.exclude_burn_addresses:
        filters.append(f"b.address NOT IN ({_address_list(EVM_BURN_ADDRESSES)})")
    if not req.include_contracts:
        filters.append("cm.address IS NULL")

    contract_join = (
        "\n  LEFT JOIN contracts.contract_mapping cm\n"
        f"         ON cm.blockchain = {_quote(req.chain.value)}\n"
        "        AND cm.address = b.address"
        if not req.include_contracts
        else ""
    )

    return f"""
-- DICE: historical holders (daily end-of-day balances)
-- {evm_table(req.chain)} is sparse: each row is a balance that held over
-- [valid_from, valid_to). The calendar join expands those intervals into the
-- one-row-per-day shape DICE exports.
{_calendar_cte(req)}
SELECT
    b.address       AS wallet_address,
    b.token_address AS token_address,
    cal.day         AS snapshot_date,
    b.balance       AS balance
FROM {evm_table(req.chain)} b
CROSS JOIN calendar cal{contract_join}
WHERE {_where(filters, "")}
ORDER BY cal.day, b.balance DESC
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
-- {SOLANA_BALANCES_TABLE} already has one row per address, mint and day,
-- so no interval expansion is needed here.
SELECT
    wallet_address,
    token_address,
    snapshot_date,
    balance
FROM (
    SELECT
        b.address              AS wallet_address,
        b.token_mint_address   AS token_address,
        b.day                  AS snapshot_date,
        SUM(b.token_balance)   AS balance
    FROM {SOLANA_BALANCES_TABLE} b
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


def build_columns_sql(chain: Chain) -> str:
    """Probe query: what does the balance table for this chain look like now?

    Cheap escape hatch for the Open Beta problem — if Dune renames a column,
    this shows the real schema without guessing.
    """
    return f"SELECT * FROM {table_for(chain)} LIMIT 1"


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
