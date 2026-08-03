"""Which of the watched wallets are actually worth watching.

The pooled signal counts heads: ten wallets bought, therefore fire. That
treats a fund that was four hours early to three tokens that tripled exactly
like a bot that buys four hundred things a month and is right by accident.
This module is the correction — it scores each wallet on its own record and
lets the head-count become a weighted one.

Three measurements, all derived from data DICE already stores:

**Hit rate** — of the tokens this wallet paid for, how many became signals.
This is the noise filter. A sprayer's hit rate collapses towards zero as its
buying volume rises, which is exactly the behaviour you want from a score.

**Return** — the median 24-hour return of the signals it bought into. The
direct question: does this wallet's buying predict price?

**Lead time** — how many hours before the signal fired the wallet bought. A
wallet consistently six hours ahead of the crowd is a leading indicator; one
that buys five minutes before the threshold is met is part of the crowd.

Shrinkage
---------
A wallet with one lucky signal would otherwise top the table forever. The
score pulls each wallet's return towards zero in proportion to how little
evidence supports it — with one signal most of the return is discounted, by
five it counts almost in full. This is the whole difference between a
leaderboard that ranks luck and one that ranks skill.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from . import db

#: Signals-worth-of-evidence at which a wallet's measured return is believed
#: roughly half. Low enough that a genuinely good wallet surfaces within a few
#: weeks, high enough that one lucky hit does not.
SHRINKAGE = 3.0

#: Below this many signals a wallet is shown but explicitly marked unproven.
MIN_SIGNALS_FOR_CONFIDENCE = 3

#: A wallet that has paid for at least this many distinct tokens with almost
#: nothing to show for it is spraying, not selecting.
SPRAY_TOKENS = 25
SPRAY_HIT_RATE = 5.0

#: Below this median lead, a wallet is arriving with the crowd rather than
#: ahead of it. Deliberately *not* folded into the score: a threshold signal
#: guarantees somebody crosses it last, so a single low lead means nothing —
#: it is only a verdict once several signals agree, which is what the flag
#: requires.
FOLLOWER_LEAD_HOURS = 0.5


def _hours_between(earlier: str | None, later: str | None) -> float | None:
    if not earlier or not later:
        return None
    try:
        start = datetime.fromisoformat(earlier)
        end = datetime.fromisoformat(later)
    except ValueError:
        return None
    if start.tzinfo is None or end.tzinfo is None:
        # Mixed awareness cannot be subtracted; treat as unknown rather than
        # inventing an offset.
        if (start.tzinfo is None) != (end.tzinfo is None):
            return None
    return round((end - start).total_seconds() / 3600, 1)


def _return_pct(entry: float | None, later: float | None) -> float | None:
    if not entry or later is None or entry <= 0:
        return None
    return (later - entry) / entry * 100


def score(median_return: float | None, signals: int) -> float | None:
    """The wallet's return, discounted by how thin the evidence is.

    ``None`` when there is nothing measured at all — an unscored wallet is not
    a zero-scoring one, and conflating the two would rank a brand-new wallet
    above a wallet with a genuine loss.
    """
    if median_return is None or signals <= 0:
        return None
    return round(median_return * (signals / (signals + SHRINKAGE)), 1)


def leaderboard(chain: str, *, limit: int = 100) -> dict[str, Any]:
    """Every watched wallet on a chain, ranked by what it has actually done."""
    cohorts = db.wallet_cohort_counts(chain)
    bought = db.wallet_buy_counts(chain)
    participation = db.wallet_signal_participation(chain)

    per_wallet: dict[str, dict[str, Any]] = {}
    for row in participation:
        entry = per_wallet.setdefault(
            row["wallet_address"], {"returns": [], "leads": [], "signals": 0, "tokens": set()}
        )
        entry["signals"] += 1
        entry["tokens"].add(row["signal_id"])
        pct = _return_pct(row.get("entry_price"), row.get("price_24h"))
        if pct is not None:
            entry["returns"].append(pct)
        lead = _hours_between(row.get("first_buy_at"), row.get("fired_at"))
        if lead is not None:
            entry["leads"].append(lead)

    rows: list[dict[str, Any]] = []
    for wallet in set(cohorts) | set(bought) | set(per_wallet):
        stats = per_wallet.get(wallet, {"returns": [], "leads": [], "signals": 0})
        signals = stats["signals"]
        tokens = bought.get(wallet, 0)
        measured = stats["returns"]
        median_return = round(statistics.median(measured), 1) if measured else None
        # Hit rate is only meaningful against tokens the wallet actually
        # bought in the retained window; without that denominator it is not
        # zero, it is unknown.
        hit_rate = round(signals / tokens * 100, 1) if tokens else None
        rows.append(
            {
                "wallet_address": wallet,
                "cohorts": cohorts.get(wallet, 0),
                "tokens_bought": tokens,
                "signals": signals,
                "hit_rate": hit_rate,
                "median_return": median_return,
                "median_lead_hours": (
                    round(statistics.median(stats["leads"]), 1) if stats["leads"] else None
                ),
                "score": score(median_return, signals),
                "proven": signals >= MIN_SIGNALS_FOR_CONFIDENCE,
                # Arrives with the crowd, not ahead of it. Such a wallet can
                # still show a fine return — it bought the same token — but it
                # never gives you time to act, so it is not worth watching for
                # its own sake.
                "follower": bool(
                    signals >= MIN_SIGNALS_FOR_CONFIDENCE
                    and stats["leads"]
                    and statistics.median(stats["leads"]) < FOLLOWER_LEAD_HOURS
                ),
                # Flagged rather than removed: a wallet that sprays may still
                # be worth keeping for one cohort, and silently dropping
                # wallets would make the pool size lie.
                "sprayer": (
                    tokens >= SPRAY_TOKENS
                    and (hit_rate is not None and hit_rate < SPRAY_HIT_RATE)
                ),
            }
        )

    # Scored wallets first, then the ones with the most cohorts behind them —
    # cohort membership is the only evidence available before signals exist.
    rows.sort(
        key=lambda row: (
            row["score"] is None,
            -(row["score"] or 0),
            -row["signals"],
            -row["cohorts"],
            row["wallet_address"],
        )
    )

    return {
        "chain": chain,
        "wallets": len(rows),
        "scored": sum(1 for row in rows if row["score"] is not None),
        "proven": sum(1 for row in rows if row["proven"]),
        "sprayers": sum(1 for row in rows if row["sprayer"]),
        "followers": sum(1 for row in rows if row["follower"]),
        "rows": rows[:limit],
    }


__all__ = [
    "FOLLOWER_LEAD_HOURS",
    "MIN_SIGNALS_FOR_CONFIDENCE",
    "SHRINKAGE",
    "leaderboard",
    "score",
]
