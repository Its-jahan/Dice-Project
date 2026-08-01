"""Watchlist monitoring: recent DEX buys per wallet, co-buy signal detection.

The idea, end to end
--------------------
A watchlist holds wallets that bought some token very early (extracted with the
holder query). Periodically — or on demand — DICE asks Dune what those wallets
have *bought on DEXes* inside a rolling window. If enough distinct wallets
bought the same token, that token becomes a **signal**: the wallets that were
early once are piling into something again.

Threshold rule: a token fires when its distinct-buyer count reaches
``max(min_wallets, ceil(min_wallets_pct% of watchlist size))``. Quote
currencies and stables are excluded up front (see ``sql.DEFAULT_IGNORED_TOKENS``)
so USDC legs of ordinary swaps never count as "buys".

Scheduling: every worker process runs one ``scheduler_loop``; SQLite hands each
due watchlist to exactly one of them (``db.claim_next_due``). Scheduled runs
need ``DUNE_API_KEY`` on the server — there is no browser attached to supply
one. Manual runs from the UI use the per-request header key like everything
else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import date, datetime, timezone
from typing import Any, Iterable

import httpx

from . import db
from .config import settings
from .dune import DuneClient, ensure_query
from .holders import _to_float
from .models import Chain, MonitorResult, MonitorRunOut, SignalOut, TokenBuyer
from .sql import build_new_positions_sql, build_trades_sql, ignored_tokens_for

log = logging.getLogger(__name__)

#: Rows fetched per monitor run. (wallets x tokens) stays far below this in
#: practice; the ORDER BY amount_usd in the SQL keeps the biggest buys if not.
MONITOR_MAX_ROWS = 200_000


# ------------------------------------------------------------------ parsing


def _to_iso_datetime(value: Any) -> str | None:
    """Dune timestamps ("2026-08-01 10:22:33.000 UTC", ISO, datetime) → ISO."""
    if isinstance(value, datetime):
        stamped = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamped.astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, date):
        return datetime(
            value.year, value.month, value.day, tzinfo=timezone.utc
        ).isoformat(timespec="seconds")
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(" UTC", "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_trade_rows(
    rows: Iterable[dict[str, Any]],
    *,
    chain: Chain,
    wallets: Iterable[str],
    ignored_tokens: Iterable[str],
) -> list[dict[str, Any]]:
    """Normalise Dune trade rows into per-(wallet, token) buy records.

    The SQL already filters wallets and ignored tokens; doing it again here
    costs nothing and keeps a schema drift upstream from silently polluting
    signals. Unusable rows are dropped, mirroring ``holders.parse_rows``.
    """
    wallet_set = set(wallets)
    ignored = set(ignored_tokens)
    buys: list[dict[str, Any]] = []
    for row in rows:
        wallet = str(row.get("wallet_address") or "").strip()
        token = str(row.get("token_address") or "").strip()
        if chain.is_evm:
            wallet = wallet.lower()
            token = token.lower()
        if not wallet or not token:
            continue
        if wallet not in wallet_set or token in ignored:
            continue

        buy_count = _to_float(row.get("buy_count"))
        amount_usd = _to_float(row.get("amount_usd"))
        symbol = row.get("token_symbol")
        buys.append(
            {
                "wallet_address": wallet,
                "token_address": token,
                "token_symbol": str(symbol).strip() if symbol else None,
                "buy_count": int(buy_count) if buy_count and buy_count > 0 else 1,
                "amount_usd": amount_usd,
                "first_buy_at": _to_iso_datetime(row.get("first_buy_at")),
                "last_buy_at": _to_iso_datetime(row.get("last_buy_at")),
                "via": "dex",
            }
        )
    return buys


def parse_position_rows(
    rows: Iterable[dict[str, Any]],
    *,
    chain: Chain,
    wallets: Iterable[str],
    ignored_tokens: Iterable[str],
) -> list[dict[str, Any]]:
    """Normalise new-position rows into the same buy-record shape, ``via=balance``.

    A position has no USD amount or symbol — the balance table doesn't carry
    them — so those stay None and the aggregation treats unknown values
    leniently (they pass ``min_buy_usd``).
    """
    wallet_set = set(wallets)
    ignored = set(ignored_tokens)
    buys: list[dict[str, Any]] = []
    for row in rows:
        wallet = str(row.get("wallet_address") or "").strip()
        token = str(row.get("token_address") or "").strip()
        if chain.is_evm:
            wallet = wallet.lower()
            token = token.lower()
        if not wallet or not token:
            continue
        if wallet not in wallet_set or token in ignored:
            continue
        first_seen = _to_iso_datetime(row.get("first_seen_at"))
        buys.append(
            {
                "wallet_address": wallet,
                "token_address": token,
                "token_symbol": None,
                "buy_count": 1,
                "amount_usd": None,
                "first_buy_at": first_seen,
                "last_buy_at": first_seen,
                "via": "balance",
            }
        )
    return buys


def merge_buys(
    dex_buys: list[dict[str, Any]], position_buys: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One record per (wallet, token); DEX evidence wins over a bare position.

    A new position that also shows up in dex.trades is simply that swap — the
    labelled record keeps the trade's USD amount and symbol. A position with
    no matching trade stays labelled ``balance``: acquired some other way
    (OTC, CEX withdrawal, transfer).
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {
        (b["wallet_address"], b["token_address"]): b for b in position_buys
    }
    for buy in dex_buys:
        merged[(buy["wallet_address"], buy["token_address"])] = buy
    return list(merged.values())


# ------------------------------------------------------------------ signals


def effective_min_wallets(
    min_wallets: int, min_wallets_pct: float, watchlist_size: int
) -> int:
    """The distinct-buyer count a token needs before it becomes a signal."""
    required = min_wallets
    if min_wallets_pct > 0:
        required = max(required, math.ceil(min_wallets_pct / 100 * watchlist_size))
    return max(required, 2)


def aggregate_candidates(
    buys: list[dict[str, Any]],
    *,
    watchlist_size: int,
    min_wallets: int,
    min_wallets_pct: float,
    min_buy_usd: float,
) -> list[dict[str, Any]]:
    """Group buys per token and keep the tokens that cross the threshold.

    ``min_buy_usd`` drops a wallet's buys of a token when their USD total is
    *known* and below the floor. Unknown values pass — very new tokens often
    have no USD price on Dune yet, and those are exactly the interesting ones.
    """
    per_token: dict[str, list[dict[str, Any]]] = {}
    for buy in buys:
        usd = buy["amount_usd"]
        if usd is not None and usd < min_buy_usd:
            continue
        per_token.setdefault(buy["token_address"], []).append(buy)

    required = effective_min_wallets(min_wallets, min_wallets_pct, watchlist_size)
    candidates: list[dict[str, Any]] = []
    for token, token_buys in per_token.items():
        buyers = sorted(
            token_buys,
            key=lambda b: (-(b["amount_usd"] or 0.0), b["wallet_address"]),
        )
        distinct = len({b["wallet_address"] for b in buyers})
        if distinct < required:
            continue
        known_usd = [b["amount_usd"] for b in buyers if b["amount_usd"] is not None]
        symbol = next((b["token_symbol"] for b in buyers if b["token_symbol"]), None)
        candidates.append(
            {
                "token_address": token,
                "token_symbol": symbol,
                "wallet_count": distinct,
                "dex_wallet_count": len(
                    {b["wallet_address"] for b in buyers if b.get("via") == "dex"}
                ),
                "total_usd": round(sum(known_usd), 2) if known_usd else None,
                "buyers": [
                    {
                        "wallet_address": b["wallet_address"],
                        "buy_count": b["buy_count"],
                        "amount_usd": b["amount_usd"],
                        "first_buy_at": b["first_buy_at"],
                        "last_buy_at": b["last_buy_at"],
                        "via": b.get("via", "dex"),
                    }
                    for b in buyers
                ],
            }
        )

    candidates.sort(key=lambda c: (-c["wallet_count"], -(c["total_usd"] or 0.0)))
    return candidates


def signal_to_out(row: dict[str, Any]) -> SignalOut:
    buyers = [TokenBuyer(**b) for b in json.loads(row.get("buyers") or "[]")]
    return SignalOut(
        id=row["id"],
        watchlist_id=row["watchlist_id"],
        watchlist_name=row.get("watchlist_name"),
        chain=Chain(row["chain"]),
        token_address=row["token_address"],
        token_symbol=row.get("token_symbol"),
        wallet_count=row["wallet_count"],
        watchlist_size=row["watchlist_size"],
        total_usd=row.get("total_usd"),
        buyers=buyers,
        first_seen_at=row["first_seen_at"],
        last_updated_at=row["last_updated_at"],
        status=row["status"],
    )


def run_to_out(row: dict[str, Any]) -> MonitorRunOut:
    return MonitorRunOut(
        id=row["id"],
        watchlist_id=row["watchlist_id"],
        trigger=row["trigger"],
        started_at=row["started_at"],
        finished_at=row.get("finished_at"),
        status=row["status"],
        error=row.get("error"),
        window_hours=row["window_hours"],
        wallets_checked=row.get("wallets_checked") or 0,
        wallets_truncated=bool(row.get("wallets_truncated")),
        buy_rows=row.get("buy_rows") or 0,
        tokens_seen=row.get("tokens_seen") or 0,
        signals_fired=row.get("signals_fired") or 0,
        execution_id=row.get("execution_id"),
    )


# ------------------------------------------------------------------ the run


async def _execute(
    client: DuneClient, *, purpose: str, name: str, sql: str
) -> tuple[list[dict[str, Any]], str]:
    """Run one monitor query through its reusable slot; return (rows, exec id)."""
    query_id = await ensure_query(client, purpose=purpose, name=name, query_sql=sql)
    execution_id = await client.execute_query(query_id)
    await client.wait_for_execution(execution_id)
    rows, _ = await client.fetch_results(execution_id, max_rows=MONITOR_MAX_ROWS)
    return rows, execution_id


async def resolve_balance_source(client: DuneClient, chain: Chain):
    """The chain's balance table, resolved and cached exactly like the holder
    query does — importing here avoids a circular import at module load."""
    from .main import resolve_for

    source, _ = await resolve_for(client, chain)
    return source


async def run_monitor(
    watchlist_id: int, *, api_key: str, trigger: str = "manual"
) -> MonitorResult:
    """Execute one monitor pass for a watchlist and persist what it found."""
    watchlist = db.get_watchlist(watchlist_id)
    if watchlist is None:
        raise LookupError(f"watchlist {watchlist_id} does not exist")

    all_wallets = db.get_wallets(watchlist_id)
    if not all_wallets:
        raise ValueError("watchlist has no wallets to monitor")

    chain = Chain(watchlist["chain"])
    cap = settings.monitor_max_wallets
    wallets = all_wallets[:cap]
    truncated = len(all_wallets) > len(wallets)
    extra_ignores = list(json.loads(watchlist["ignore_tokens"] or "[]"))
    window_hours = int(watchlist["buy_window_hours"])

    run_id = db.create_run(
        watchlist_id=watchlist_id, trigger=trigger, window_hours=window_hours
    )
    detection = str(watchlist["buy_detection"] or "both")
    ignored = ignored_tokens_for(chain, extra_ignores)
    label = f"{watchlist['name'][:40]} ({chain.value}, {len(wallets)}w, {window_hours}h)"

    try:
        dex_buys: list[dict[str, Any]] = []
        position_buys: list[dict[str, Any]] = []
        execution_id: str | None = None

        async with DuneClient(api_key) as client:
            if detection in ("dex", "both"):
                # One reusable query slot per watchlist and mode: concurrent
                # monitors must not rewrite each other's SQL.
                rows, execution_id = await _execute(
                    client,
                    purpose=f"monitor:{watchlist_id}",
                    name=f"DICE monitor: {label}",
                    sql=build_trades_sql(
                        chain,
                        wallets,
                        window_hours=window_hours,
                        extra_ignore_tokens=extra_ignores,
                    ),
                )
                dex_buys = parse_trade_rows(
                    rows, chain=chain, wallets=wallets, ignored_tokens=ignored
                )

            if detection in ("balance", "both"):
                source = await resolve_balance_source(client, chain)
                rows, position_execution = await _execute(
                    client,
                    purpose=f"positions:{watchlist_id}",
                    name=f"DICE positions: {label}",
                    sql=build_new_positions_sql(
                        chain,
                        source,
                        wallets,
                        window_hours=window_hours,
                        extra_ignore_tokens=extra_ignores,
                    ),
                )
                position_buys = parse_position_rows(
                    rows, chain=chain, wallets=wallets, ignored_tokens=ignored
                )
                execution_id = execution_id or position_execution

        buys = merge_buys(dex_buys, position_buys)
        candidates = aggregate_candidates(
            buys,
            watchlist_size=len(wallets),
            min_wallets=int(watchlist["min_wallets"]),
            min_wallets_pct=float(watchlist["min_wallets_pct"]),
            min_buy_usd=float(watchlist["min_buy_usd"]),
        )

        new_signals: list[SignalOut] = []
        updated_signals: list[SignalOut] = []
        notify: list[tuple[SignalOut, bool]] = []
        for candidate in candidates:
            signal_id, created, previous_count = db.upsert_signal(
                watchlist_id=watchlist_id,
                chain=chain.value,
                token_address=candidate["token_address"],
                token_symbol=candidate["token_symbol"],
                wallet_count=candidate["wallet_count"],
                watchlist_size=len(wallets),
                total_usd=candidate["total_usd"],
                buyers=candidate["buyers"],
            )
            signal_row = db.get_signal(signal_id)
            if signal_row is None:  # pragma: no cover - row just written
                continue
            out = signal_to_out(signal_row)
            if created:
                new_signals.append(out)
                notify.append((out, True))
            else:
                updated_signals.append(out)
                if out.status == "active" and out.wallet_count > previous_count:
                    notify.append((out, False))

        db.finish_run(
            run_id,
            status="ok",
            wallets_checked=len(wallets),
            wallets_truncated=int(truncated),
            buy_rows=len(buys),
            tokens_seen=len({b["token_address"] for b in buys}),
            signals_fired=len(candidates),
            execution_id=execution_id,
        )
        db.prune_runs(watchlist_id)
        db.finish_schedule(
            watchlist_id, interval_hours=float(watchlist["monitor_interval_hours"])
        )

        if notify:
            await send_signal_notification(
                watchlist_name=watchlist["name"],
                chain=chain,
                window_hours=window_hours,
                signals=notify,
            )

        run_row = db.get_run(run_id)
        assert run_row is not None
        return MonitorResult(
            run=run_to_out(run_row),
            new_signals=new_signals,
            updated_signals=updated_signals,
        )
    except Exception as exc:
        db.finish_run(run_id, status="error", error=str(exc)[:500])
        raise


# ------------------------------------------------------------------ telegram


def _short_address(address: str) -> str:
    return address if len(address) <= 14 else f"{address[:6]}…{address[-4:]}"


#: Channels and supergroups carry a ``-100``-prefixed id. People routinely
#: paste the bare internal number (from a t.me/c/ link or a helper bot), which
#: Telegram then rejects with an unhelpful "chat not found".
_TME_INVITE_RE = re.compile(r"^(?:https?://)?t\.me/c/(\d+)")
_TME_PUBLIC_RE = re.compile(r"^(?:https?://)?t\.me/(?:s/)?([A-Za-z]\w{3,})/?$")


def normalize_chat_id(raw: str) -> str:
    """Turn the forms people actually paste into what the Bot API expects.

    ``https://t.me/mychannel`` → ``@mychannel``;
    ``https://t.me/c/1234567890/12`` → ``-1001234567890``.
    Anything already in a valid shape (``@name``, ``-100…``, a bare user id)
    is returned untouched — guessing at a bare number is left to
    :func:`send_telegram_message`, which can test it against Telegram.
    """
    value = (raw or "").strip()
    if not value:
        return value
    invite = _TME_INVITE_RE.match(value)
    if invite:
        return f"-100{invite.group(1)}"
    public = _TME_PUBLIC_RE.match(value)
    if public:
        return f"@{public.group(1)}"
    return value


def _describe_telegram_error(status: int, payload: dict[str, Any], chat_id: str) -> str:
    """Translate a Bot API rejection into the fix for it."""
    description = str(payload.get("description") or f"HTTP {status}")
    lowered = description.lower()

    if "chat not found" in lowered:
        return (
            f"{description} — Telegram cannot see chat “{chat_id}”. For a "
            "channel the id must be the -100… form (e.g. -1001234567890), and "
            "the bot has to be added to the channel first. A public channel "
            "can also be addressed as @channelusername."
        )
    if "not enough rights" in lowered or "need administrator" in lowered:
        return (
            f"{description} — the bot is in the channel but cannot post. Make "
            "it an administrator with the “Post Messages” permission."
        )
    if "bot is not a member" in lowered or "bot was kicked" in lowered:
        return (
            f"{description} — add the bot to the channel and make it an "
            "administrator."
        )
    if status == 401 or "unauthorized" in lowered:
        return (
            f"{description} — the bot token is wrong or was revoked. Get a "
            "fresh one from @BotFather."
        )
    if "can't parse" in lowered or "chat_id is empty" in lowered:
        return f"{description} — the chat id is malformed."
    return f"Telegram answered {status}: {description}"


async def describe_telegram_bot() -> str | None:
    """The configured bot's @username via getMe, or None if it cannot be read.

    Knowing which bot to add as a channel administrator is half the setup, and
    a token holds no hint of its own name.
    """
    token, _ = db.telegram_credentials()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not payload.get("ok"):
        return None
    return (payload.get("result") or {}).get("username")


async def _post_message(
    client: httpx.AsyncClient, token: str, chat_id: str, text: str
) -> tuple[bool, int, dict[str, Any]]:
    response = await client.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"description": response.text[:200]}
    return response.status_code < 400, response.status_code, payload


async def send_telegram_message(text: str) -> str | None:
    """Send one Telegram message; return an error description or None on success.

    Credentials come from the UI-saved settings, falling back to the
    environment. Callers decide whether a failure matters: signal pushes log
    and continue, the settings "send test" endpoint reports it to the user.

    A bare numeric chat id that Telegram does not recognise is retried once
    with the ``-100`` channel prefix; if that works the corrected id is saved,
    so the common "pasted the channel's internal number" mistake fixes itself.
    """
    token, chat_id = db.telegram_credentials()
    if not token or not chat_id:
        return "Telegram is not configured (bot token / chat id missing)."

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            ok, status, payload = await _post_message(client, token, chat_id, text)
            if ok:
                return None

            description = str(payload.get("description") or "").lower()
            retryable = chat_id.lstrip("-").isdigit() and not chat_id.startswith("-100")
            if "chat not found" in description and retryable:
                candidate = f"-100{chat_id.lstrip('-')}"
                ok, status, payload = await _post_message(
                    client, token, candidate, text
                )
                if ok:
                    db.set_setting("telegram_chat_id", candidate)
                    log.info("corrected Telegram chat id to the channel form")
                    return None
                chat_id = candidate
    except httpx.HTTPError as exc:
        return f"could not reach Telegram: {exc}"

    return _describe_telegram_error(status, payload, chat_id)


async def send_signal_notification(
    *,
    watchlist_name: str,
    chain: Chain,
    window_hours: int,
    signals: list[tuple[SignalOut, bool]],
) -> None:
    """Push new/strengthened signals to Telegram, if configured. Best-effort."""
    token, chat_id = db.telegram_credentials()
    if not token or not chat_id:
        return

    lines = [f"DICE signals — {watchlist_name} ({chain.value}, last {window_hours}h)"]
    for signal, is_new in signals:
        label = signal.token_symbol or _short_address(signal.token_address)
        usd = (
            f" ≈ ${signal.total_usd:,.0f}" if signal.total_usd is not None else ""
        )
        dex = sum(1 for buyer in signal.buyers if buyer.via == "dex")
        positions = len(signal.buyers) - dex
        breakdown = f" [{dex} DEX"
        breakdown += f", {positions} new position]" if positions else "]"
        lines.append(
            f"{'NEW' if is_new else 'UP'} {label}: "
            f"{signal.wallet_count}/{signal.watchlist_size} wallets bought{usd}"
            f"{breakdown}"
        )
        lines.append(signal.token_address)
        lines.append(f"https://dexscreener.com/{chain.value}/{signal.token_address}")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n…"

    error = await send_telegram_message(text)
    if error:
        log.warning("telegram notification failed: %s", error)


# ------------------------------------------------------------------ scheduler


async def scheduler_loop() -> None:
    """Run due watchlists forever; one loop per worker, claims dedupe them."""
    log.info(
        "watchlist scheduler running (tick %ss, server key %s)",
        settings.monitor_tick_seconds,
        "configured" if db.server_api_key() else "not saved yet — runs idle",
    )
    while True:
        try:
            await asyncio.sleep(settings.monitor_tick_seconds)
            if not settings.monitor_enabled:
                continue
            # Re-read every tick: a key saved through the UI starts working
            # without a restart.
            api_key = db.server_api_key() or ""
            if not api_key:
                continue

            while True:
                watchlist_id = db.claim_next_due(now_iso=db.utcnow_iso())
                if watchlist_id is None:
                    break
                watchlist = db.get_watchlist(watchlist_id)
                interval = float(
                    (watchlist or {}).get("monitor_interval_hours") or 24.0
                )
                try:
                    result = await run_monitor(
                        watchlist_id, api_key=api_key, trigger="auto"
                    )
                    log.info(
                        "scheduled run for watchlist %s: %s new / %s updated signals",
                        watchlist_id,
                        len(result.new_signals),
                        len(result.updated_signals),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "scheduled monitor run failed for watchlist %s", watchlist_id
                    )
                finally:
                    # Always reschedule + release the claim, even after errors,
                    # so a broken watchlist retries next interval instead of
                    # blocking the queue.
                    db.finish_schedule(watchlist_id, interval_hours=interval)
        except asyncio.CancelledError:
            log.info("watchlist scheduler stopped")
            raise
        except Exception:  # pragma: no cover - belt and braces
            log.exception("watchlist scheduler tick failed")


__all__ = [
    "aggregate_candidates",
    "effective_min_wallets",
    "merge_buys",
    "parse_position_rows",
    "parse_trade_rows",
    "run_monitor",
    "run_to_out",
    "describe_telegram_bot",
    "normalize_chat_id",
    "scheduler_loop",
    "send_signal_notification",
    "send_telegram_message",
    "signal_to_out",
]
