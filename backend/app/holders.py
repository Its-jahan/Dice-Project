"""Turns raw Dune rows into snapshots, holder-mode selections and summaries."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from .models import HolderMode, HoldersRequest, Snapshot, WalletSummary, utc_today


def parse_rows(rows: Iterable[dict[str, Any]], req: HoldersRequest) -> list[Snapshot]:
    """Normalise Dune result rows into :class:`Snapshot` objects.

    Rows that are unusable (missing wallet/date, unparseable balance, balance at
    or below the requested minimum) are dropped rather than raising: a single
    odd row should not fail a job that returned a million good ones.
    """
    snapshots: list[Snapshot] = []
    for row in rows:
        wallet = _first(row, "wallet_address", "address", "owner")
        day = _first(row, "snapshot_date", "day", "date", "block_date")
        raw_balance = _first(row, "balance", "amount", "token_balance")
        if wallet is None or day is None or raw_balance is None:
            continue

        snapshot_date = _to_date(day)
        balance = _to_float(raw_balance)
        if snapshot_date is None or balance is None:
            continue
        # Strictly greater, matching the SQL filter. "Minimum balance: 100"
        # means more than 100, and 0 means any positive balance.
        if balance <= 0 or balance <= req.min_balance:
            continue
        if not (req.start_date <= snapshot_date <= req.effective_end_date):
            continue

        token = _first(row, "token_address", "token_mint_address") or req.token_address
        snapshots.append(
            Snapshot(
                wallet_address=str(wallet),
                token_address=str(token),
                snapshot_date=snapshot_date,
                balance=balance,
            )
        )

    snapshots.sort(key=lambda s: (s.snapshot_date, -s.balance, s.wallet_address))
    return snapshots


def apply_holder_mode(
    snapshots: list[Snapshot], req: HoldersRequest
) -> list[Snapshot]:
    """Filter snapshots down to the wallets the chosen mode considers holders.

    ``daily`` and ``any_time`` keep every wallet that appears at least once; the
    difference between them is presentational (the summary file), not which
    wallets qualify. ``continuous`` keeps only wallets present on *every* day of
    the requested range.
    """
    if req.holder_mode is not HolderMode.continuous:
        return snapshots
    if req.days <= 0:
        # No days in range at all; every wallet would vacuously "hold" on all
        # of them, which is the wrong answer.
        return []

    expected_days = {
        req.start_date + timedelta(days=offset) for offset in range(req.days)
    }
    days_by_wallet: dict[str, set[date]] = defaultdict(set)
    for snapshot in snapshots:
        days_by_wallet[snapshot.wallet_address].add(snapshot.snapshot_date)

    continuous = {
        wallet
        for wallet, days in days_by_wallet.items()
        if expected_days.issubset(days)
    }
    return [s for s in snapshots if s.wallet_address in continuous]


def build_summary(snapshots: Iterable[Snapshot]) -> list[WalletSummary]:
    """One row per wallet: window held, day count and balance envelope."""
    grouped: dict[str, list[Snapshot]] = defaultdict(list)
    for snapshot in snapshots:
        grouped[snapshot.wallet_address].append(snapshot)

    summaries: list[WalletSummary] = []
    for wallet, items in grouped.items():
        balances = [i.balance for i in items]
        days = {i.snapshot_date for i in items}
        summaries.append(
            WalletSummary(
                wallet_address=wallet,
                first_date=min(days),
                last_date=max(days),
                days_held=len(days),
                min_balance=min(balances),
                max_balance=max(balances),
                avg_balance=sum(balances) / len(balances),
            )
        )

    summaries.sort(key=lambda s: (-s.max_balance, s.wallet_address))
    return summaries


# --------------------------------------------------------------------- utils


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Dune emits e.g. "2026-07-20 00:00:00.000 UTC" or ISO-8601 with a Z.
    text = text.replace(" UTC", "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
