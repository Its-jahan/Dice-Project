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

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import db
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

            ignores = ignored_tokens_for(
                chain, list(_json_list(watchlist.get("ignore_tokens")))
            )
            if token in ignores:
                continue

            try:
                result = evaluate_token(watchlist, token)
            except Exception:  # pragma: no cover - defensive
                log.exception("live evaluation failed for watchlist %s", watchlist["id"])
                continue
            if result is None:
                continue
            signal, created, previous = result
            if created:
                fired.append((signal, True))
            elif signal.status == "active" and signal.wallet_count > previous:
                fired.append((signal, False))

    for signal, is_new in fired:
        await send_signal_notification(
            watchlist_name=signal.watchlist_name or f"#{signal.watchlist_id}",
            chain=chain,
            window_hours=0,
            signals=[(signal, is_new)],
        )

    db.prune_events(_iso(_utcnow() - timedelta(days=EVENT_RETENTION_DAYS)))
    return {"events": len(events), "stored": stored, "signals": len(fired)}


def _json_list(raw: Any) -> Iterable[str]:
    import json

    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


__all__ = ["evaluate_token", "ingest", "parse_activity"]
