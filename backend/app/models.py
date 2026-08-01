"""Request/response schemas for the DICE API."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Solana mints are base58, 32-44 chars.
SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Maximum span we allow in a single job. Daily snapshots for a wide range on a
# popular token get expensive fast, both in Dune credits and in export size.
MAX_RANGE_DAYS = 366


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


class Chain(str, Enum):
    """Chains DICE can query.

    Values are Dune's own schema names (``balances_<value>``), so they must
    match the catalogue exactly — ``avalanche_c``, not ``avalanche``. A chain
    listed here that the caller's plan cannot reach reports a clear "no
    readable balance table" rather than failing obscurely.
    """

    ethereum = "ethereum"
    arbitrum = "arbitrum"
    avalanche_c = "avalanche_c"
    base = "base"
    bnb = "bnb"
    celo = "celo"
    flare = "flare"
    gnosis = "gnosis"
    hyperevm = "hyperevm"
    ink = "ink"
    kaia = "kaia"
    linea = "linea"
    mantle = "mantle"
    monad = "monad"
    optimism = "optimism"
    plasma = "plasma"
    polygon = "polygon"
    sei = "sei"
    sonic = "sonic"
    unichain = "unichain"
    solana = "solana"

    @property
    def is_evm(self) -> bool:
        return self is not Chain.solana


class HolderMode(str, Enum):
    #: every wallet with a positive end-of-day balance, one row per (wallet, day)
    daily = "daily"
    #: wallets that held at any point in the range (one summary row per wallet)
    any_time = "any_time"
    #: wallets that held on *every* day of the range
    continuous = "continuous"


class ExportFormat(str, Enum):
    csv = "csv"
    xlsx = "xlsx"
    json = "json"
    html = "html"


class HoldersRequest(BaseModel):
    chain: Chain
    token_address: str
    start_date: date
    end_date: date
    min_balance: float = Field(default=0.0, ge=0)
    holder_mode: HolderMode = HolderMode.daily
    #: keep rows whose wallet is a known smart contract
    include_contracts: bool = True
    #: drop the zero address and common burn sinks
    exclude_burn_addresses: bool = True

    @field_validator("token_address")
    @classmethod
    def _strip_token(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate(self) -> "HoldersRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        # A future start date makes the generated calendar run backwards, which
        # Dune rejects with an opaque sequence error. Catch it here instead.
        if self.start_date > utc_today():
            raise ValueError(
                f"start_date is in the future (today is {utc_today()} UTC)"
            )
        span = (self.end_date - self.start_date).days + 1
        if span > MAX_RANGE_DAYS:
            raise ValueError(
                f"date range of {span} days exceeds the {MAX_RANGE_DAYS}-day limit"
            )

        if self.chain.is_evm:
            if not EVM_ADDRESS_RE.match(self.token_address):
                raise ValueError("token_address must be a 0x-prefixed 20-byte address")
            object.__setattr__(self, "token_address", self.token_address.lower())
        elif not SOLANA_MINT_RE.match(self.token_address):
            raise ValueError("token_address must be a base58 Solana mint address")
        return self

    @property
    def effective_end_date(self) -> date:
        """The last day that can actually have data: never later than today.

        A sparse balance row that is still current carries ``valid_to IS NULL``,
        which matches *every* future calendar day. Left unclamped, a future end
        date makes DICE project today's balances forward and emit snapshots for
        days that have not happened. Clamping keeps the output to observed data.
        """
        return min(self.end_date, utc_today())

    @property
    def end_date_clamped(self) -> bool:
        return self.effective_end_date < self.end_date

    @property
    def days(self) -> int:
        """Days actually covered, after clamping to today."""
        return max((self.effective_end_date - self.start_date).days + 1, 0)

    @property
    def requested_days(self) -> int:
        return (self.end_date - self.start_date).days + 1


class Snapshot(BaseModel):
    """One end-of-day balance for one wallet."""

    wallet_address: str
    token_address: str
    snapshot_date: date
    balance: float


class WalletSummary(BaseModel):
    wallet_address: str
    first_date: date
    last_date: date
    days_held: int
    min_balance: float
    max_balance: float
    avg_balance: float


class HoldersResponse(BaseModel):
    request: HoldersRequest
    execution_id: str | None = None
    row_count: int
    wallet_count: int
    snapshots: list[Snapshot]
    summary: list[WalletSummary]
    truncated: bool = False


# ---------------------------------------------------------------- watchlists

#: ``dex``: only swaps from the DEX trade tables count — cleanest, misses
#: OTC/CEX routes. ``balance``: any token whose balance went from zero to
#: positive — catches everything, including airdrops and self-transfers.
#: ``both``: run the two together and label each wallet with how it was seen.
BuyDetection = Literal["dex", "balance", "both"]


def normalize_addresses(chain: Chain, addresses: list[str]) -> list[str]:
    """Validate and canonicalise addresses (wallets or tokens) for one chain.

    EVM addresses are lowercased (Dune stores them lowercase); Solana addresses
    are base58 and case-sensitive, so they pass through unchanged. Duplicates
    are dropped while preserving order. Raises ``ValueError`` on the first bad
    address so the caller can report exactly which one failed.
    """
    seen: set[str] = set()
    result: list[str] = []
    for raw in addresses:
        address = raw.strip()
        if not address:
            continue
        if chain.is_evm:
            if not EVM_ADDRESS_RE.match(address):
                raise ValueError(f"invalid {chain.value} address: {address!r}")
            address = address.lower()
        elif not SOLANA_MINT_RE.match(address):
            raise ValueError(f"invalid Solana address: {address!r}")
        if address not in seen:
            seen.add(address)
            result.append(address)
    return result


class WatchlistSettings(BaseModel):
    """Tunable monitoring/signal parameters, shared by create and update.

    The signal threshold is ``max(min_wallets, ceil(min_wallets_pct% of the
    watchlist size))`` — the absolute floor keeps tiny lists from firing on two
    wallets, the percentage keeps big lists honest. Set ``min_wallets_pct`` to
    0 to use the absolute count alone.
    """

    #: distinct buyers required before a token becomes a signal (absolute floor)
    min_wallets: int = Field(default=5, ge=2, le=10_000)
    #: distinct buyers required as a percentage of the watchlist size
    min_wallets_pct: float = Field(default=10.0, ge=0, le=100)
    #: how far back each monitor run looks for buys ("bought within N hours")
    buy_window_hours: int = Field(default=48, ge=1, le=24 * 14)
    #: scheduled gap between automatic runs; each run costs Dune credits
    monitor_interval_hours: float = Field(default=24.0, ge=1, le=24 * 7)
    #: ignore a wallet's buys of one token when they total less than this (USD)
    min_buy_usd: float = Field(default=0.0, ge=0)
    #: whether the background scheduler runs this list (needs a server key)
    auto_monitor: bool = True
    #: token addresses to ignore on top of the built-in quote/stable stoplist
    ignore_tokens: list[str] = Field(default_factory=list)
    #: how a "buy" is detected — confirmed DEX swaps, any new balance, or both
    #: (labelled per wallet). ``both`` costs two Dune executions per run.
    buy_detection: BuyDetection = "both"
    #: stream this list's wallets through Alchemy webhooks as well, so signals
    #: fire seconds after a buy instead of at the next scheduled check
    realtime: bool = False

    @field_validator("ignore_tokens")
    @classmethod
    def _strip_ignores(cls, value: list[str]) -> list[str]:
        cleaned = []
        for token in value:
            token = token.strip()
            if token:
                cleaned.append(token)
        return cleaned


class WatchlistCreate(WatchlistSettings):
    name: str = Field(min_length=1, max_length=120)
    chain: Chain
    wallets: list[str] = Field(min_length=1)
    #: the token whose early buyers these wallets are (informational)
    source_token_address: str | None = None
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _validate_wallets(self) -> "WatchlistCreate":
        normalized = normalize_addresses(self.chain, self.wallets)
        if not normalized:
            raise ValueError("wallets contained no valid addresses")
        object.__setattr__(self, "wallets", normalized)
        object.__setattr__(
            self, "ignore_tokens", normalize_addresses(self.chain, self.ignore_tokens)
        )
        if self.source_token_address:
            source = normalize_addresses(self.chain, [self.source_token_address])
            object.__setattr__(self, "source_token_address", source[0])
        return self


class WatchlistFromJob(WatchlistSettings):
    """Create a watchlist from a finished holders job (its full wallet set)."""

    name: str | None = Field(default=None, max_length=120)
    #: keep only the N wallets with the highest max balance (summary order)
    top_n: int | None = Field(default=None, ge=1)


class WatchlistUpdate(BaseModel):
    """Partial update; only supplied fields change."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)
    min_wallets: int | None = Field(default=None, ge=2, le=10_000)
    min_wallets_pct: float | None = Field(default=None, ge=0, le=100)
    buy_window_hours: int | None = Field(default=None, ge=1, le=24 * 14)
    monitor_interval_hours: float | None = Field(default=None, ge=1, le=24 * 7)
    min_buy_usd: float | None = Field(default=None, ge=0)
    auto_monitor: bool | None = None
    ignore_tokens: list[str] | None = None
    buy_detection: BuyDetection | None = None
    realtime: bool | None = None
    add_wallets: list[str] | None = None
    remove_wallets: list[str] | None = None


class WatchlistOut(BaseModel):
    id: int
    name: str
    chain: Chain
    source_token_address: str | None
    notes: str
    wallet_count: int
    min_wallets: int
    min_wallets_pct: float
    effective_min_wallets: int
    buy_window_hours: int
    monitor_interval_hours: float
    min_buy_usd: float
    auto_monitor: bool
    ignore_tokens: list[str]
    buy_detection: BuyDetection
    realtime: bool
    created_at: str
    last_run_at: str | None
    last_run_status: str | None
    last_run_error: str | None
    next_run_at: str | None
    active_signals: int


class TokenBuyer(BaseModel):
    """One watched wallet's aggregate buying of one token in the window."""

    wallet_address: str
    buy_count: int
    amount_usd: float | None
    first_buy_at: str | None
    last_buy_at: str | None
    #: how this wallet was detected: ``dex`` (confirmed swap), ``balance``
    #: (new position found by the Dune monitor) or ``live`` (token arrival
    #: pushed by Alchemy within seconds of the block)
    via: Literal["dex", "balance", "live"] = "dex"


class SignalOut(BaseModel):
    id: int
    watchlist_id: int
    watchlist_name: str | None = None
    chain: Chain
    token_address: str
    token_symbol: str | None
    wallet_count: int
    watchlist_size: int
    total_usd: float | None
    buyers: list[TokenBuyer]
    first_seen_at: str
    last_updated_at: str
    status: str


class MonitorRunOut(BaseModel):
    id: int
    watchlist_id: int
    trigger: str
    started_at: str
    finished_at: str | None
    status: str
    error: str | None
    window_hours: int
    wallets_checked: int
    wallets_truncated: bool
    buy_rows: int
    tokens_seen: int
    signals_fired: int
    execution_id: str | None


class MonitorResult(BaseModel):
    """What a single monitor run produced, returned by the run-now endpoint."""

    run: MonitorRunOut
    new_signals: list[SignalOut]
    updated_signals: list[SignalOut]
