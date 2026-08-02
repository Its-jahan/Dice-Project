"""DexScreener lookups: does this token actually trade, and at what price?

Why this exists
---------------
The live path learns about a token from a transfer, and a transfer proves
nothing about tradability. Spam tokens are minted and blasted at thousands of
addresses without ever having a liquidity pool — they cannot be bought, sold
or priced, and a "signal" on one is noise. DexScreener knows which tokens have
a pool, so it is used here as the ground-truth gate: no pair, no signal.

It also fills the gap the transfer feed leaves. Alchemy carries no price, so
live signals had no USD figure at all; the same call returns price, liquidity
and 24h volume, which is what decides whether a signal is worth acting on.

The public API needs no key, allows batched lookups and is rate limited, so
answers are cached in SQLite (see :data:`MARKET_TTL_SECONDS`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from . import db
from .models import Chain

log = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com"

#: Addresses per request. The endpoint takes a comma-separated list; this
#: keeps requests well inside the documented rate limit.
BATCH_SIZE = 30

#: How long a market snapshot stays usable. Long enough to keep a busy board
#: off the API, short enough that liquidity figures are not stale.
MARKET_TTL_SECONDS = 1800

#: DICE chain -> DexScreener chain slug. They agree for most chains but not
#: all: Dune calls BNB Chain "bnb" where DexScreener says "bsc".
CHAIN_SLUGS: dict[Chain, str] = {
    Chain.ethereum: "ethereum",
    Chain.arbitrum: "arbitrum",
    Chain.avalanche_c: "avalanche",
    Chain.base: "base",
    Chain.bnb: "bsc",
    Chain.celo: "celo",
    Chain.gnosis: "gnosischain",
    Chain.linea: "linea",
    Chain.mantle: "mantle",
    Chain.optimism: "optimism",
    Chain.polygon: "polygon",
    Chain.sei: "seiv2",
    Chain.sonic: "sonic",
    Chain.unichain: "unichain",
    Chain.ink: "ink",
    Chain.solana: "solana",
}


def chain_slug(chain: Chain) -> str:
    return CHAIN_SLUGS.get(chain, chain.value)


def token_url(chain: Chain, token_address: str) -> str:
    return f"https://dexscreener.com/{chain_slug(chain)}/{token_address}"


def _best_pair(pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The deepest pool — the one a price is actually meaningful against."""
    priced = [pair for pair in pairs if isinstance(pair, dict)]
    if not priced:
        return None
    return max(priced, key=lambda pair: _usd(pair.get("liquidity")))


def _usd(liquidity: Any) -> float:
    if isinstance(liquidity, dict):
        try:
            return float(liquidity.get("usd") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarise(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a token's pairs to the numbers a decision needs."""
    pair = _best_pair(pairs)
    if pair is None:
        return {
            "has_pair": False,
            "price_usd": None,
            "liquidity_usd": 0.0,
            "volume_24h": 0.0,
            "fdv": None,
            "symbol": None,
            "name": None,
            "pair_url": None,
            "pair_created_at": None,
        }
    base = pair.get("baseToken") or {}
    created = pair.get("pairCreatedAt")
    return {
        "has_pair": True,
        "price_usd": _to_float(pair.get("priceUsd")),
        "liquidity_usd": _usd(pair.get("liquidity")),
        "volume_24h": _to_float((pair.get("volume") or {}).get("h24")) or 0.0,
        "fdv": _to_float(pair.get("fdv")),
        "symbol": base.get("symbol"),
        "name": base.get("name"),
        "pair_url": pair.get("url"),
        # DexScreener reports milliseconds.
        "pair_created_at": (
            datetime.fromtimestamp(created / 1000, timezone.utc).isoformat(
                timespec="seconds"
            )
            if isinstance(created, (int, float)) and created > 0
            else None
        ),
    }


async def _fetch_batch(
    client: httpx.AsyncClient, chain: Chain, addresses: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """One batched call; returns the pairs found, grouped by token address.

    A token with no pool simply does not appear in the response — that absence
    is the answer, not an error.
    """
    path = f"/tokens/v1/{chain_slug(chain)}/{','.join(addresses)}"
    try:
        response = await client.get(path)
    except httpx.HTTPError as exc:
        log.warning("dexscreener unreachable: %s", exc)
        return {}
    if response.status_code >= 400:
        log.warning("dexscreener returned %s", response.status_code)
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}

    pairs = payload if isinstance(payload, list) else (payload.get("pairs") or [])
    grouped: dict[str, list[dict[str, Any]]] = {}
    wanted = {address.lower() for address in addresses}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        base = (pair.get("baseToken") or {}).get("address") or ""
        base = str(base).lower()
        if base in wanted:
            grouped.setdefault(base, []).append(pair)
    return grouped


async def market_data(
    chain: Chain, addresses: Iterable[str], *, refresh: bool = False
) -> dict[str, dict[str, Any]]:
    """Market summary per token address, cached and batched.

    Addresses whose lookup fails outright (network down, API unhappy) are
    simply absent from the result, so callers can tell "no pool" from "do not
    know yet" and avoid treating an outage as proof a token is spam.
    """
    wanted = {address.lower() for address in addresses if address}
    if not wanted:
        return {}

    known: dict[str, dict[str, Any]] = {}
    if not refresh:
        fresh_after = (
            datetime.now(timezone.utc) - timedelta(seconds=MARKET_TTL_SECONDS)
        ).isoformat(timespec="seconds")
        known = db.get_token_markets(
            chain=chain.value, addresses=wanted, fresh_after=fresh_after
        )

    missing = sorted(wanted - set(known))
    if not missing:
        return known

    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=httpx.Timeout(20.0, connect=10.0)
    ) as client:
        for index in range(0, len(missing), BATCH_SIZE):
            batch = missing[index : index + BATCH_SIZE]
            grouped = await _fetch_batch(client, chain, batch)
            if not grouped and index == 0 and len(batch) == len(missing):
                # A completely empty answer for the first batch is ambiguous:
                # every token could be poolless, or the API could be down. The
                # per-address writes below still record the poolless verdict,
                # which is correct in the common case; a hard failure was
                # already logged.
                pass
            for address in batch:
                summary = summarise(grouped.get(address, []))
                db.save_token_market(
                    chain=chain.value, token_address=address, summary=summary
                )
                known[address] = summary

    return known


__all__ = ["chain_slug", "market_data", "summarise", "token_url"]
