"""Works out which balance table to read, and what shape it has.

Why this exists
---------------
There is no stable table name to hard-code. Reading one Dune account's
catalogue shows all of these at once:

* ``balances_ethereum`` holds only internal tables — ``stg_daily_updates``,
  ``raw_updates``, ``triggers`` — and *no* ``daily_updates``;
* the real table sits in a versioned build schema,
  ``balances_ethereum__spellbook_sqlmesh_490.daily_updates``, whose number
  rotates as Dune rebuilds;
* other chains expose an older dense shape instead, ``balances_polygon.erc20_day``.

Which of those a given API key can see depends on the account. So DICE asks
``information_schema`` what exists, picks the best candidate, and adapts its
SQL to that table's columns rather than assuming any particular name.

Two shapes are supported:

``interval``
    Sparse rows carrying ``[valid_from, valid_to)``. Needs a calendar join to
    expand into one row per day.

``daily``
    Dense rows already keyed by a day column. Used as-is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Chain

#: Table names worth reading, best first. Anything else in these schemas is
#: staging, failure-tracking or trigger bookkeeping.
TABLE_PREFERENCE = (
    "daily_updates",
    "erc20_day",
    "bep20_day",
    "daily_balances",
    "stg_daily_updates",
)

#: Column aliases, best first. Dune has used several spellings over time.
ADDRESS_COLUMNS = ("address", "wallet_address", "owner")
TOKEN_COLUMNS = ("token_address", "token_mint_address", "contract_address", "token")
BALANCE_COLUMNS = ("balance", "amount", "token_balance")
DAY_COLUMNS = ("day", "block_date", "snapshot_date", "date")

_SPELLBOOK_SUFFIX = re.compile(r"__spellbook_sqlmesh_(\d+)$")


class SourceNotFound(RuntimeError):
    """No table in the catalogue has the columns DICE needs."""


@dataclass(frozen=True)
class Source:
    """A resolved balance table and the column names to read from it."""

    schema: str
    table: str
    shape: str  # "interval" | "daily"
    address: str
    token: str
    balance: str
    day: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


def schema_pattern(chain: Chain) -> str:
    """LIKE pattern covering a chain's balance schemas, build schemas included."""
    if chain is Chain.solana:
        return "solana%"
    return f"balances_{chain.value}%"


def _pick(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _schema_rank(schema: str) -> tuple[int, int]:
    """Plain schemas beat build schemas; newer builds beat older ones.

    A plain ``balances_ethereum`` is the stable public name and is preferred
    whenever it actually carries the table. Failing that, the highest-numbered
    ``__spellbook_sqlmesh_N`` is the freshest build.
    """
    match = _SPELLBOOK_SUFFIX.search(schema)
    if not match:
        return (0, 0)
    return (1, -int(match.group(1)))


def resolve_source(catalog: dict[str, set[str]]) -> Source:
    """Choose the best table from ``{"schema.table": {columns}}``.

    Raises :class:`SourceNotFound` if nothing usable is present, which is a
    real answer — it means the key's plan does not include balance data.
    """
    scored: list[tuple[tuple[int, int, int], Source]] = []

    for qualified, columns in catalog.items():
        if "." not in qualified:
            continue
        schema, _, table = qualified.partition(".")
        if table not in TABLE_PREFERENCE:
            continue

        address = _pick(columns, ADDRESS_COLUMNS)
        token = _pick(columns, TOKEN_COLUMNS)
        balance = _pick(columns, BALANCE_COLUMNS)
        if not (address and token and balance):
            continue

        if "valid_from" in columns and "valid_to" in columns:
            source = Source(
                schema=schema,
                table=table,
                shape="interval",
                address=address,
                token=token,
                balance=balance,
                valid_from="valid_from",
                valid_to="valid_to",
            )
        else:
            day = _pick(columns, DAY_COLUMNS)
            if not day:
                continue
            source = Source(
                schema=schema,
                table=table,
                shape="daily",
                address=address,
                token=token,
                balance=balance,
                day=day,
            )

        # Table name ranks first: a published `daily_updates` in a build schema
        # is a better source than a `stg_` staging table in the plain schema,
        # and than the legacy dense `erc20_day`. Schema kind only breaks ties
        # between equally-good table names.
        name_rank = TABLE_PREFERENCE.index(table)
        schema_kind, schema_order = _schema_rank(schema)
        scored.append(((name_rank, schema_kind, schema_order), source))

    if not scored:
        raise SourceNotFound(
            "No readable balance table found for this chain. Your Dune plan may "
            "not include the curated balance tables — run 'Check data source' "
            "to see what the key can reach."
        )

    scored.sort(key=lambda item: item[0])
    return scored[0][1]


# --------------------------------------------------------------- contracts

#: Ways to tell "this address is a contract", best first. creation_traces is
#: the most complete: every contract ever deployed appears there, whereas a
#: decoded-contract mapping only covers projects someone has decoded.
CONTRACT_CANDIDATES = (
    ("{chain}", "creation_traces"),
    ("{chain}", "contracts"),
    ("contracts", "contract_mapping"),
)
CONTRACT_ADDRESS_COLUMNS = ("address", "contract_address")


@dataclass(frozen=True)
class ContractSource:
    """A table that says which addresses are contracts."""

    schema: str
    table: str
    address: str
    #: Set when the table spans chains and must be filtered by one.
    blockchain: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


def contract_candidates(chain: Chain) -> tuple[tuple[str, str], ...]:
    return tuple(
        (schema.format(chain=chain.value), table)
        for schema, table in CONTRACT_CANDIDATES
    )


def resolve_contract_source(
    chain: Chain, catalog: dict[str, set[str]]
) -> ContractSource:
    """Pick a table for the "exclude smart contracts" filter.

    Raises :class:`SourceNotFound` when none is readable, so the option fails
    loudly rather than silently returning contracts the caller asked to drop.
    """
    for schema, table in contract_candidates(chain):
        columns = catalog.get(f"{schema}.{table}")
        if not columns:
            continue
        address = _pick(columns, CONTRACT_ADDRESS_COLUMNS)
        if not address:
            continue
        return ContractSource(
            schema=schema,
            table=table,
            address=address,
            blockchain="blockchain" if "blockchain" in columns else None,
        )

    raise SourceNotFound(
        "No table available to identify smart-contract addresses on "
        f"{chain.value}. Re-tick 'Include smart-contract addresses' to run "
        "without that filter."
    )
