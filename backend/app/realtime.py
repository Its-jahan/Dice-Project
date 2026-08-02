"""Real-time wallet monitoring: ingest Alchemy events, fire signals instantly.

How this differs from the Dune monitor
--------------------------------------
:mod:`app.monitor` *asks* Dune what the watched wallets bought, so a signal is
at most one polling interval old — with a 24 h interval that averages 12 h of
delay, an order of magnitude more than Dune's own data lag. Here, Alchemy
pushes every token arrival at a watched wallet within seconds of the block; the
rolling-window count runs locally against SQLite, so evaluating "have enough
wallets bought this token?" costs nothing and happens on every event.

The two paths converge on the same ``signals`` table and the same Telegram
notification, so a watchlist can run either or both.

What counts as a buy here
-------------------------
A token *arriving* at a watched wallet. Two arrivals are deliberately dropped:

* transfers **from another wallet in the same watchlist** — a token passed
  around the group is one position, not N independent buyers;
* the usual stablecoin / wrapped-native / watchlist-source stoplist.

Without a swap decode this is the same semantics as the Dune "new position"
mode, so buyers are labelled ``live``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import db
from .config import settings
from .models import Chain, SignalOut
from .monitor import effective_min_wallets, send_signal_notification, signal_to_out
from .sql import ignored_tokens_for

log = logging.getLogger(__name__)

#: Events older than the longest window any watchlist can ask for are dead
#: weight; pruned opportunistically as new events arrive.
EVENT_RETENTION_DAYS = 15

#: Categories Alchemy reports that represent a token landing in a wallet.
#: ``external``/``internal`` are native-currency moves (plain ETH in), which are
#: not token buys and would otherwise make every gas top-up a "buy".
TOKEN_CATEGORIES = {"erc20", "token"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ------------------------------------------------------------------- parsing


def parse_activity(
    payload: dict[str, Any], *, chain: Chain, watched: set[str]
) -> list[dict[str, Any]]:
    """Turn an ADDRESS_ACTIVITY delivery into token-arrival events.

    Alchemy sends one delivery per block containing an ``activity`` array of
    transfers touching *any* registered address, in either direction — so a
    watched wallet **sending** a token shows up here too and must be skipped.
    """
    event = payload.get("event") or {}
    activities = event.get("activity") or []
    seen_at = _iso(_utcnow())
    events: list[dict[str, Any]] = []

    for item in activities:
        if not isinstance(item, dict):
            continue
        # A reorg'd transfer never happened; do not let it count as a buy.
        if item.get("removed"):
            continue
        category = str(item.get("category") or "").lower()
        if category not in TOKEN_CATEGORIES:
            continue

        to_address = str(item.get("toAddress") or "").strip().lower()
        from_address = str(item.get("fromAddress") or "").strip().lower()
        if to_address not in watched:
            continue  # the watched party is the sender, not the receiver
        if from_address in watched:
            # Shuffling a token between wallets of the same list is one
            # position, not two buyers.
            continue

        contract = item.get("rawContract") or {}
        token_address = str(contract.get("address") or "").strip().lower()
        if not token_address:
            continue

        tx_hash = str(item.get("hash") or "").strip().lower()
        if not tx_hash:
            continue

        symbol = item.get("asset")
        events.append(
            {
                "chain": chain.value,
                "wallet_address": to_address,
                "token_address": token_address,
                "tx_hash": tx_hash,
                "token_symbol": str(symbol).strip() if symbol else None,
                "amount": _to_float(item.get("value")),
                "block_num": _to_int(item.get("blockNum")),
                "seen_at": seen_at,
            }
        )
    return events


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    try:
        return int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return None


# ---------------------------------------------------------------- evaluation


def evaluate_token(
    watchlist: dict[str, Any], token_address: str
) -> tuple[SignalOut, bool, int] | None:
    """Re-check one token for one watchlist against the live event store.

    Returns ``(signal, created, previous_wallet_count)`` when the token is at or
    above the threshold, else None. Counting happens in SQLite over the
    watchlist's own wallets, so it stays correct as wallets are added or removed.
    """
    chain = Chain(watchlist["chain"])
    wallets = db.get_wallets(int(watchlist["id"]))
    if not wallets:
        return None

    window_hours = int(watchlist["buy_window_hours"])
    since = _iso(_utcnow() - timedelta(hours=window_hours))
    rows = db.events_in_window(
        chain=chain.value,
        token_address=token_address,
        since_iso=since,
        wallets=wallets,
    )
    if not rows:
        return None

    required = effective_min_wallets(
        int(watchlist["min_wallets"]),
        float(watchlist["min_wallets_pct"]),
        len(wallets),
    )
    if len(rows) < required:
        return None

    symbol = next((row["token_symbol"] for row in rows if row["token_symbol"]), None)
    buyers = [
        {
            "wallet_address": row["wallet_address"],
            "buy_count": int(row["buy_count"] or 1),
            # Alchemy's transfer feed carries no USD price; enrichment would
            # need a separate price lookup, so this stays honest about that.
            "amount_usd": None,
            "first_buy_at": row["first_buy_at"],
            "last_buy_at": row["last_buy_at"],
            "via": "live",
        }
        for row in rows
    ]

    signal_id, created, previous = db.upsert_signal(
        watchlist_id=int(watchlist["id"]),
        chain=chain.value,
        token_address=token_address,
        token_symbol=symbol,
        wallet_count=len(rows),
        watchlist_size=len(wallets),
        total_usd=None,
        buyers=buyers,
    )
    row = db.get_signal(signal_id)
    if row is None:  # pragma: no cover - just written
        return None
    return signal_to_out(row), created, previous


def ignored_for(watchlist: dict[str, Any], chain: Chain) -> set[str]:
    return set(
        ignored_tokens_for(chain, list(_json_list(watchlist.get("ignore_tokens"))))
    )


def check_token(
    watchlist: dict[str, Any], token: str, chain: Chain
) -> tuple[SignalOut, bool] | None:
    """Evaluate one token and say whether it is worth announcing.

    Returns ``(signal, is_new)`` for a token that has just crossed the
    threshold or gained buyers since the last look, else None. Shared by the
    event path and the periodic sweep so both decide identically.
    """
    if token in ignored_for(watchlist, chain):
        return None
    try:
        result = evaluate_token(watchlist, token)
    except Exception:  # pragma: no cover - defensive
        log.exception("live evaluation failed for watchlist %s", watchlist["id"])
        return None
    if result is None:
        return None
    signal, created, previous = result
    if created:
        return signal, True
    if signal.status == "active" and signal.wallet_count > previous:
        return signal, False
    return None


async def announce(fired: list[tuple[SignalOut, bool]], *, chain: Chain) -> None:
    for signal, is_new in fired:
        await send_signal_notification(
            watchlist_name=signal.watchlist_name or f"#{signal.watchlist_id}",
            chain=chain,
            window_hours=0,
            signals=[(signal, is_new)],
        )


async def ingest(payload: dict[str, Any], *, chain: Chain) -> dict[str, Any]:
    """Store a delivery's events and fire any signal it completes.

    Returns a small summary for the endpoint to log/return. Never raises on
    signal-side problems: the delivery has already been accepted, and Alchemy
    retries anything we answer with an error.
    """
    watched = db.realtime_wallets(chain.value)
    if not watched:
        return {"events": 0, "stored": 0, "signals": 0}

    events = parse_activity(payload, chain=chain, watched=watched)
    if not events:
        return {"events": 0, "stored": 0, "signals": 0}

    stored = db.record_events(events)

    # Only the (wallet, token) pairs this delivery actually touched need
    # re-checking — everything else's count is unchanged.
    pairs = {(event["wallet_address"], event["token_address"]) for event in events}
    fired: list[tuple[SignalOut, bool]] = []
    checked: set[tuple[int, str]] = set()

    for wallet, token in pairs:
        for watchlist in db.realtime_watchlists_for_wallet(chain.value, wallet):
            key = (int(watchlist["id"]), token)
            if key in checked:
                continue
            checked.add(key)
            outcome = check_token(watchlist, token, chain)
            if outcome:
                fired.append(outcome)

    await announce(fired, chain=chain)
    db.prune_events(_iso(_utcnow() - timedelta(days=EVENT_RETENTION_DAYS)))
    return {"events": len(events), "stored": stored, "signals": len(fired)}


# ----------------------------------------------------------------- the sweep


async def sweep() -> dict[str, Any]:
    """Re-check every live watchlist's recent tokens against its threshold.

    The event path only evaluates the tokens a delivery touched, which misses
    a token that is *already* over the line for a reason no new event will
    announce: the threshold was lowered, wallets were added to the list, or
    the wallets that would tip it over bought before the last change. This
    sweep closes that gap — it is cheap, since it reads the local event store
    rather than any API.
    """
    fired_total = 0
    checked_total = 0

    for chain_value in db.realtime_chains():
        chain = Chain(chain_value)
        fired: list[tuple[SignalOut, bool]] = []
        for watchlist in db.live_watchlists(chain_value):
            wallets = db.get_wallets(int(watchlist["id"]))
            if not wallets:
                continue
            since = _iso(
                _utcnow() - timedelta(hours=int(watchlist["buy_window_hours"]))
            )
            required = effective_min_wallets(
                int(watchlist["min_wallets"]),
                float(watchlist["min_wallets_pct"]),
                len(wallets),
            )
            for row in db.token_activity(
                chain=chain_value, wallets=wallets, since_iso=since
            ):
                if row["wallet_count"] < required:
                    # token_activity is sorted by wallet_count, so nothing
                    # further down can qualify either.
                    break
                checked_total += 1
                outcome = check_token(watchlist, row["token_address"], chain)
                if outcome:
                    fired.append(outcome)
        await announce(fired, chain=chain)
        fired_total += len(fired)

    return {"checked": checked_total, "signals": fired_total}


async def sweep_loop() -> None:
    """Run :func:`sweep` on an interval for the lifetime of the process."""
    log.info("live sweep running every %ss", settings.live_sweep_seconds)
    while True:
        try:
            await asyncio.sleep(settings.live_sweep_seconds)
            result = await sweep()
            if result["signals"]:
                log.info(
                    "live sweep fired %s signal(s) from %s token(s)",
                    result["signals"],
                    result["checked"],
                )
        except asyncio.CancelledError:
            log.info("live sweep stopped")
            raise
        except Exception:  # pragma: no cover - belt and braces
            log.exception("live sweep failed")


# ------------------------------------------------------------------- the board


def accumulation_board(
    *, watchlist_id: int | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """What the watched wallets are buying right now, threshold or not.

    A signal only exists once the threshold is crossed; this shows the
    build-up before that — the token four wallets into a ten-wallet threshold
    that is worth keeping an eye on.
    """
    rows: list[dict[str, Any]] = []
    watchlists = [
        wl
        for wl in db.live_watchlists()
        if watchlist_id is None or int(wl["id"]) == watchlist_id
    ]

    for watchlist in watchlists:
        chain = Chain(watchlist["chain"])
        wallets = db.get_wallets(int(watchlist["id"]))
        if not wallets:
            continue
        window_hours = int(watchlist["buy_window_hours"])
        since = _iso(_utcnow() - timedelta(hours=window_hours))
        required = effective_min_wallets(
            int(watchlist["min_wallets"]),
            float(watchlist["min_wallets_pct"]),
            len(wallets),
        )
        ignores = ignored_for(watchlist, chain)
        signals = {
            row["token_address"]: row["status"]
            for row in db.list_signals(
                watchlist_id=int(watchlist["id"]), include_dismissed=True, limit=500
            )
        }

        for row in db.token_activity(
            chain=chain.value, wallets=wallets, since_iso=since
        ):
            token = row["token_address"]
            if token in ignores:
                continue
            rows.append(
                {
                    "watchlist_id": int(watchlist["id"]),
                    "watchlist_name": watchlist["name"],
                    "chain": chain.value,
                    "token_address": token,
                    "token_symbol": row["token_symbol"],
                    "wallet_count": row["wallet_count"],
                    "buy_count": row["buy_count"],
                    "watchlist_size": len(wallets),
                    "required": required,
                    "window_hours": window_hours,
                    "first_buy_at": row["first_buy_at"],
                    "last_buy_at": row["last_buy_at"],
                    "signal_status": signals.get(token),
                }
            )

    rows.sort(
        key=lambda row: (
            -(row["wallet_count"] / max(row["required"], 1)),
            row["last_buy_at"] or "",
        )
    )
    return rows[:limit]


def _json_list(raw: Any) -> Iterable[str]:
    import json

    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


__all__ = [
    "accumulation_board",
    "check_token",
    "evaluate_token",
    "ingest",
    "parse_activity",
    "sweep",
    "sweep_loop",
]
