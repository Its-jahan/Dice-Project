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

from typing import Any

from . import db

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


__all__ = ["DEFAULT_UNIVERSE", "overlap_matrix", "score_pair"]
