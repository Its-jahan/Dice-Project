"""Did the signals actually work?

Everything else in DICE decides *when* to fire. This decides whether firing
was right — and without it the rest is unfalsifiable. A 10% pool threshold, a
48-hour window and a given set of cohorts are all just guesses until something
records what the price did afterwards.

Method
------
The price and liquidity at the moment of the signal are stamped by
:func:`app.db.record_outcome`. This module fills in the same token's price at
1 h, 24 h and 7 d, and reduces the set to numbers a human can act on: the
share of signals that went up, and the median return.

Median, not mean, on purpose: one token that went 40x would drag a mean into
looking like a strategy, when the median says most signals went nowhere.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db, dexscreener
from .models import Chain

log = logging.getLogger(__name__)

#: Horizon column -> how long after the signal it becomes measurable.
HORIZONS: dict[str, timedelta] = {
    "price_1h": timedelta(hours=1),
    "price_24h": timedelta(hours=24),
    "price_7d": timedelta(days=7),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pool_age_hours(pair_created_at: str | None) -> float | None:
    """How old the liquidity pool was, in hours. None when unknown."""
    if not pair_created_at:
        return None
    try:
        created = datetime.fromisoformat(pair_created_at)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return round((_utcnow() - created).total_seconds() / 3600, 1)


async def fill_horizons(limit: int = 50) -> dict[str, int]:
    """Look up the current price for signals that have come of age.

    Prices are fetched fresh rather than from the market cache: a cached entry
    could be up to half an hour old, which at the 1-hour horizon is a third of
    the measurement.
    """
    filled: dict[str, int] = {}
    for column, age in HORIZONS.items():
        due = db.outcomes_due(column, _iso(_utcnow() - age), limit=limit)
        if not due:
            continue
        by_chain: dict[str, list[str]] = {}
        for row in due:
            by_chain.setdefault(row["chain"], []).append(row["token_address"])

        prices: dict[tuple[str, str], float | None] = {}
        for chain_value, tokens in by_chain.items():
            try:
                markets = await dexscreener.market_data(
                    Chain(chain_value), tokens, refresh=True
                )
            except Exception:  # pragma: no cover - defensive
                log.exception("price lookup failed for %s", chain_value)
                continue
            for address, market in markets.items():
                prices[(chain_value, address)] = market.get("price_usd")

        for row in due:
            key = (row["chain"], row["token_address"])
            if key not in prices:
                continue  # lookup failed; leave it pending rather than guess
            # A token whose pool is gone reads as zero, which is the truth:
            # the position could not have been exited.
            db.set_outcome_price(row["signal_id"], column, prices[key] or 0.0)
            filled[column] = filled.get(column, 0) + 1
    return filled


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _returns(rows: list[dict[str, Any]], column: str) -> list[float]:
    out = []
    for row in rows:
        entry, later = row.get("entry_price"), row.get(column)
        if entry and later is not None and entry > 0:
            out.append((later - entry) / entry * 100)
    return out


def _due_at(fired_at: str | None, age: timedelta) -> datetime | None:
    """When a horizon becomes measurable for one signal."""
    if not fired_at:
        return None
    try:
        fired = datetime.fromisoformat(fired_at)
    except ValueError:
        return None
    if fired.tzinfo is None:
        fired = fired.replace(tzinfo=timezone.utc)
    return fired + age


def summarise(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Hit rate and median return per horizon, plus how early the signals were.

    Each horizon also reports how many signals are simply *too young* to have
    an answer and when the next one is due. Without that a fresh install shows
    three dashes and no way to tell "nothing has happened yet" from "this is
    broken" — which is the wrong conclusion to leave available, because the
    honest state of a new scoreboard is mostly empty for a day.
    """
    rows = db.list_outcomes(limit=1000) if rows is None else rows
    now = _utcnow()

    horizons = []
    for column, age in HORIZONS.items():
        returns = _returns(rows, column)
        # Split the unanswered into "waiting for the clock" and "due, and the
        # checker has not got to it" — only the second is ever a problem.
        waiting, overdue, soonest = 0, 0, None
        for row in rows:
            if row.get(column) is not None:
                continue
            due = _due_at(row.get("fired_at"), age)
            if due is None:
                continue
            if due > now:
                waiting += 1
                soonest = due if soonest is None else min(soonest, due)
            else:
                overdue += 1
        horizons.append(
            {
                "horizon": column.replace("price_", ""),
                "measured": len(returns),
                "waiting": waiting,
                "overdue": overdue,
                "next_due_in_hours": (
                    round((soonest - now).total_seconds() / 3600, 1)
                    if soonest else None
                ),
                # The number that matters: how often it went up at all.
                "win_rate": (
                    round(sum(1 for r in returns if r > 0) / len(returns) * 100, 1)
                    if returns else None
                ),
                "median_return": round(statistics.median(returns), 1) if returns else None,
                "best": round(max(returns), 1) if returns else None,
                "worst": round(min(returns), 1) if returns else None,
            }
        )

    ages = [r["pool_age_hours"] for r in rows if r.get("pool_age_hours") is not None]
    return {
        "signals": len(rows),
        # Scored at all, on any horizon — the number someone means by "is this
        # working". The old "pending" counted signals with no 24h price, which
        # read as "nothing measured" while a 1h column sat full beside it.
        "scored": sum(
            1 for r in rows
            if any(r.get(c) is not None for c in HORIZONS)
        ),
        "horizons": horizons,
        # How early the system actually is, rather than how early it feels.
        "median_pool_age_hours": round(statistics.median(ages), 1) if ages else None,
        "recent": rows[:25],
    }


__all__ = ["HORIZONS", "fill_horizons", "pool_age_hours", "summarise"]
