"""When the wallets that got you in start getting out.

Everything else in DICE answers "should I buy this". Nothing answered "should
I still be holding it", which is the asymmetry that actually costs money: the
system tells you to enter and then goes quiet, and the cohort whose conviction
was the entire reason for the trade can walk out without a word.

The counting was already here — :func:`app.db.exit_counts` has been filling a
``sellers`` column on the live board for a while. But that column only exists
if you remember to open the page, and the moment you most need it is the one
where you are not looking. This turns the same number into a message.

Method
------
For each recent signal, take **the wallets that actually bought it** — not the
pool, the buyers named on that signal — and count how many have sold *since it
fired*. A wallet that sold last week and bought back in is not exiting, so the
signal's own timestamp is the floor.

Then one message, once, when that share crosses the threshold.

Why once
--------
An exit is news the first time. Repeating it every minute for a day trains you
to swipe the alert away, and the next one you swipe away is the one that
mattered. :func:`app.db.record_exit` is the flag, and it is an insert, so two
overlapping sweeps cannot both decide they are the one to send it.

Why a floor as well as a percentage
-----------------------------------
Same reason the buy side has ``pool_min_wallets``: a third of three buyers is
one wallet, and one wallet closing a position is a Tuesday, not a signal.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db, monitor
from .config import settings
from .models import Chain

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ------------------------------------------------------------------- settings
#
# Runtime-adjustable, following the idiom used throughout: SQLite first so a
# change needs no redeploy, falling back to the shipped default.


def exit_pct() -> float:
    """Share of a signal's buyers that must have sold before it is news."""
    stored = db.get_setting("exit_pct")
    try:
        return float(stored) if stored is not None else settings.exit_pct
    except ValueError:
        return settings.exit_pct


def exit_min_wallets() -> int:
    stored = db.get_setting("exit_min_wallets")
    try:
        return int(stored) if stored is not None else settings.exit_min_wallets
    except ValueError:
        return settings.exit_min_wallets


def exit_window_days() -> int:
    """How long after a signal an exit is still worth reporting."""
    stored = db.get_setting("exit_window_days")
    try:
        return int(stored) if stored is not None else settings.exit_window_days
    except ValueError:
        return settings.exit_window_days


# ---------------------------------------------------------------- the counting


def _buyers(signal: dict[str, Any]) -> list[str]:
    """The wallet addresses named on a signal.

    The column holds serialised :class:`app.models.TokenBuyer` records — a
    list of *objects*, each with a ``wallet_address`` — not a list of bare
    addresses. Reading it as addresses is a silent failure rather than a loud
    one: every wallet becomes the string form of a dict, matches nothing, and
    the exit count is zero forever while the code looks like it is working.
    Bare strings are still accepted so a hand-written row does not blow up.
    """
    raw = signal.get("buyers")
    if not raw:
        return []
    parsed = raw
    if not isinstance(parsed, list):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError):
            return []
    if not isinstance(parsed, list):
        return []

    addresses = []
    for entry in parsed:
        if isinstance(entry, dict):
            address = entry.get("wallet_address")
        elif isinstance(entry, str):
            address = entry
        else:
            continue
        if address:
            addresses.append(str(address).lower())
    return addresses


def measure(signal: dict[str, Any]) -> dict[str, Any] | None:
    """How much of this signal's cohort has left. None when unmeasurable.

    Returning None rather than a zero matters: a signal with no recorded
    buyers is *unknown*, not intact, and reporting "0% have exited" for it
    would be a reassurance nobody checked.
    """
    buyers = _buyers(signal)
    if not buyers:
        return None
    fired = signal.get("first_seen_at")
    if not fired:
        return None

    found = db.token_exits(
        chain=signal["chain"],
        token_address=signal["token_address"],
        wallets=buyers,
        # Sells *since the signal*. An earlier sale is not this position.
        since_iso=fired,
    )
    sellers = int(found.get("sellers") or 0)
    return {
        "signal_id": signal["id"],
        "sellers": sellers,
        "buyers": len(buyers),
        "pct": round(sellers / len(buyers) * 100, 1),
        "last_sell_at": found.get("last_sell_at"),
    }


def is_news(measured: dict[str, Any]) -> bool:
    """Both bars, and both for different reasons: the percentage says the
    cohort is leaving rather than one member rotating, the floor says there
    are enough of them for the percentage to mean anything.
    """
    return (
        measured["sellers"] >= exit_min_wallets()
        and measured["pct"] >= exit_pct()
    )


# ------------------------------------------------------------------- the sweep


async def check(limit: int = 100) -> dict[str, int]:
    """Look at recent signals and warn about the ones being sold out of.

    Called from the live sweep. Every failure is swallowed per-signal: an exit
    warning is worth having, but never at the price of stopping the sweep that
    finds the next entry.
    """
    since = _iso(_utcnow() - timedelta(days=exit_window_days()))
    try:
        candidates = db.signals_to_watch_for_exits(since, limit=limit)
    except Exception:  # pragma: no cover - defensive
        log.exception("listing signals for exit checks failed")
        return {"checked": 0, "alerted": 0}

    alerted = 0
    for signal in candidates:
        try:
            measured = measure(signal)
            if measured is None or not is_news(measured):
                continue
            # Claim it before sending. If the send fails the signal stays
            # claimed and goes unreported — the opposite ordering risks
            # sending the same warning on every sweep forever.
            fresh = db.record_exit(
                signal_id=measured["signal_id"],
                sellers=measured["sellers"],
                buyers=measured["buyers"],
                pct=measured["pct"],
                alerted_at=_iso(_utcnow()),
            )
            if not fresh:
                continue
            await monitor.send_exit_notification(
                chain=Chain(signal["chain"]),
                token_address=signal["token_address"],
                token_symbol=signal.get("token_symbol"),
                measured=measured,
            )
            alerted += 1
        except Exception:  # pragma: no cover - one bad signal must not stop the rest
            log.exception("exit check failed for signal %s", signal.get("id"))

    return {"checked": len(candidates), "alerted": alerted}


__all__ = [
    "check",
    "exit_min_wallets",
    "exit_pct",
    "exit_window_days",
    "is_news",
    "measure",
]
