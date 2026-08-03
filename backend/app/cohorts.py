"""Cohort overlap: which watchlists share wallets, and is that surprising?

The question this answers
------------------------
"Thirty of the wallets that farmed FWA are now in LOS." Each watchlist is a
cohort — a named set of wallets that shared some history — so the answer is a
set intersection over data already in SQLite. No Dune credit, no API call.

Why a raw count is not the answer
---------------------------------
Thirty shared wallets means nothing on its own. If one cohort has 5,000
wallets and the other 50,000, thirty is *fewer* than chance would produce, and
reporting it as a finding is worse than reporting nothing. Two numbers are
returned instead:

``containment``
    The share of the smaller cohort that ended up in the other. Needs no
    assumptions and is directly readable: "40% of the FWA farmers are in LOS".

``lift``
    Observed overlap divided by the overlap chance alone would give. Above 1
    means the cohorts are related; below 1 means they avoid each other. This
    one *does* depend on how many wallets could plausibly have been in either
    cohort, which nobody knows exactly — so the universe is an explicit,
    adjustable input and every result says which value produced it, rather
    than hiding an assumption inside a confident-looking number.
"""

from __future__ import annotations

import logging
from typing import Any

from . import db

log = logging.getLogger(__name__)

#: Wallets that could plausibly have joined either cohort. A modelling choice,
#: not a measurement: raise it and every overlap looks more surprising, lower
#: it and everything looks mundane. Exposed in the UI for exactly that reason.
DEFAULT_UNIVERSE = 1_000_000

#: Pairs below this are noise on any reading — two wallets in common says
#: nothing whatever the ratio works out to.
MIN_OVERLAP = 2


def score_pair(
    *, overlap: int, size_a: int, size_b: int, universe: int
) -> dict[str, Any]:
    """Turn a raw shared count into numbers that can be compared."""
    smaller = min(size_a, size_b) or 1
    union = size_a + size_b - overlap
    # Chance overlap if membership of one cohort said nothing about the other.
    expected = (size_a * size_b) / universe if universe else 0.0
    return {
        "overlap": overlap,
        "containment": round(overlap / smaller * 100, 1),
        "pct_of_a": round(overlap / (size_a or 1) * 100, 1),
        "pct_of_b": round(overlap / (size_b or 1) * 100, 1),
        "jaccard": round(overlap / union * 100, 2) if union else 0.0,
        "expected": round(expected, 2),
        # Guarded: a tiny expectation would otherwise divide into a headline
        # number that says more about the universe than about the cohorts.
        "lift": round(overlap / expected, 1) if expected >= 0.01 else None,
    }


def overlap_matrix(
    *, universe: int = DEFAULT_UNIVERSE, min_overlap: int = MIN_OVERLAP
) -> dict[str, Any]:
    """Every pair of cohorts that share wallets, most surprising first."""
    sizes = db.cohort_sizes()
    pairs: list[dict[str, Any]] = []

    for row in db.cohort_overlaps():
        a, b = sizes.get(row["a_id"]), sizes.get(row["b_id"])
        if not a or not b or row["overlap"] < min_overlap:
            continue
        scored = score_pair(
            overlap=row["overlap"],
            size_a=int(a["wallets"]),
            size_b=int(b["wallets"]),
            universe=universe,
        )
        pairs.append(
            {
                "a_id": row["a_id"],
                "a_name": a["name"],
                "a_size": int(a["wallets"]),
                "b_id": row["b_id"],
                "b_name": b["name"],
                "b_size": int(b["wallets"]),
                "chain": a["chain"],
                **scored,
            }
        )

    # Ranked by containment first, then lift — never by raw count, because a
    # big overlap between two huge cohorts is the least interesting thing
    # here. Containment leads because it needs no universe assumption, and
    # because ranking on lift alone buried exactly the pairs worth seeing:
    # small cohorts have a tiny expectation, so their lift is suppressed as
    # unreliable, which would sort "half of this cohort is in that one" last.
    pairs.sort(
        key=lambda p: (-p["containment"], -(p["lift"] or 0), -p["overlap"])
    )
    return {
        "universe": universe,
        "min_overlap": min_overlap,
        "cohorts": len(sizes),
        "pairs": pairs,
    }


# --------------------------------------------------------- derived cohorts

#: How many cohorts a wallet must appear in before it counts as a repeat.
#: Two is the loosest setting that means anything; three is where coincidence
#: stops being a comfortable explanation.
DEFAULT_MIN_COHORTS = 3

DERIVED_NAME = "Repeat wallets (auto)"


def min_cohorts() -> int:
    stored = db.get_setting("derived_min_cohorts")
    try:
        return max(2, int(stored)) if stored is not None else DEFAULT_MIN_COHORTS
    except ValueError:
        return DEFAULT_MIN_COHORTS


def refresh_derived(chain: str, *, threshold: int | None = None) -> dict[str, Any]:
    """Rebuild the chain's repeat-wallet cohort from every real cohort.

    The manual version of this was: read the overlap table, open a promising
    pair, copy the shared wallets, make a watchlist. That only ever compares
    two cohorts at a time and goes stale the moment another is added. This
    generalises it — appearing in three cohorts is a stronger claim than
    appearing in one particular pair — and re-runs whenever the inputs change.

    Membership is replaced rather than merged: a wallet that no longer meets
    the threshold, because a cohort was deleted, has to leave.
    """
    threshold = threshold or min_cohorts()
    repeats = db.repeat_wallets(chain, threshold)
    wallets = [row["wallet_address"] for row in repeats]

    existing = db.find_derived(chain)
    name = f"{DERIVED_NAME} ≥{threshold} cohorts"
    notes = (
        f"Rebuilt automatically from every {chain} cohort: wallets appearing "
        f"in at least {threshold} of them. Excluded from overlap analysis, "
        "since a subset would score 100% against its own sources."
    )

    if existing is None:
        if not wallets:
            return {"chain": chain, "wallets": 0, "watchlist_id": None,
                    "threshold": threshold, "created": False}
        watchlist_id = db.create_watchlist(
            name=name,
            chain=chain,
            wallets=wallets,
            source_token_address=None,
            notes=notes,
            min_wallets=3,
            min_wallets_pct=10.0,
            buy_window_hours=48,
            monitor_interval_hours=24.0,
            min_buy_usd=0.0,
            # Kept out of the Dune scheduler: this cohort changes whenever its
            # inputs do, and paying for a scheduled query on a moving set is
            # not what it is for. The live path picks it up if switched on.
            auto_monitor=False,
            ignore_tokens=[],
            derived=True,
        )
        created = True
    else:
        watchlist_id = int(existing["id"])
        db.replace_wallets(watchlist_id, wallets)
        db.update_watchlist_fields(watchlist_id, {"name": name, "notes": notes})
        created = False

    return {
        "chain": chain,
        "watchlist_id": watchlist_id,
        "wallets": len(wallets),
        "threshold": threshold,
        "created": created,
        "top": repeats[:10],
    }


def refresh_all_derived(threshold: int | None = None) -> list[dict[str, Any]]:
    """Refresh every chain that has real cohorts."""
    results = []
    for chain in sorted({c["chain"] for c in db.cohort_sizes().values()}):
        try:
            results.append(refresh_derived(chain, threshold=threshold))
        except Exception:  # pragma: no cover - defensive
            log.exception("derived cohort refresh failed for %s", chain)
    return results


__all__ = [
    "DEFAULT_UNIVERSE",
    "min_cohorts",
    "overlap_matrix",
    "refresh_all_derived",
    "refresh_derived",
    "score_pair",
]
