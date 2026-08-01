"""Request/response schemas for the DICE API."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from enum import Enum

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
    ethereum = "ethereum"
    base = "base"
    arbitrum = "arbitrum"
    optimism = "optimism"
    polygon = "polygon"
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
