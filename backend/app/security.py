"""Contract risk screening: can this token actually be sold again?

Why this exists
---------------
Every other check in DICE asks whether a token is *interesting*. This one asks
whether it is *survivable*. The worst outcome the system can produce is not a
missed signal — it is a correct signal on a token whose contract will not let
you sell, or takes 40% of the proceeds when you do. Ten wallets really did buy
it; the contract simply keeps their money.

Where the answers come from
---------------------------
GoPlus Security's public token endpoint, which reads the deployed bytecode and
the pool state and reports honeypot behaviour, buy/sell tax, mint authority,
blacklists, transfer pauses and LP lock state. No key is needed.

This is deliberately **not** an LLM reading contract source. A model that is
mostly right about a honeypot is worse than useless here: the failure is
silent and costs the whole position. A dedicated service that says
``is_honeypot: 1`` is a fact; a model's opinion about Solidity is not.

What blocks and what only warns
-------------------------------
Only unsellability blocks a signal, because it is the one verdict that makes
the trade impossible rather than merely bad. Everything else — high tax, open
mint authority, unlocked liquidity — is attached to the signal as a warning
and left to the person reading it. A gate that silently swallows signals on
heuristics would be indistinguishable from a broken webhook.

An API failure never blocks. If GoPlus is down, tokens pass with
``checked: False``, exactly as an unknown price leaves an outcome pending
rather than recording a zero.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from . import db
from .models import Chain

log = logging.getLogger(__name__)

BASE_URL = "https://api.gopluslabs.io/api/v1"

#: One address per request. The endpoint accepts a comma-separated list and
#: the documentation implies batching, but it answers for the *first* address
#: only — verified by asking for three and receiving one. Sending more would
#: silently leave the rest unscreened, which for a safety check is the worst
#: possible failure: it looks like a pass.
BATCH_SIZE = 1

#: How long a verdict stays usable. A contract's risk profile changes rarely,
#: but not never — an owner can switch on a tax — so this is hours, not days.
VERDICT_TTL_SECONDS = 6 * 3600

#: An unchecked verdict is cached far more briefly. It records "the API was
#: down", which stops being true the moment it comes back up.
UNCHECKED_TTL_SECONDS = 600

#: DICE chain -> GoPlus chain id. Chains absent here are simply not screened;
#: an unscreened token is reported as unchecked rather than as safe.
CHAIN_IDS: dict[Chain, str] = {
    Chain.ethereum: "1",
    Chain.optimism: "10",
    Chain.bnb: "56",
    Chain.gnosis: "100",
    Chain.polygon: "137",
    Chain.sonic: "146",
    Chain.mantle: "5000",
    Chain.base: "8453",
    Chain.arbitrum: "42161",
    Chain.celo: "42220",
    Chain.avalanche_c: "43114",
    Chain.linea: "59144",
}

#: A sell tax at or above this is not a fee, it is a trapdoor.
SEVERE_TAX_PCT = 20.0

#: Below this, "tax" is usually a normal transfer fee rather than a warning.
NOTABLE_TAX_PCT = 10.0


def supported(chain: Chain) -> bool:
    return chain in CHAIN_IDS


def _pct(value: Any) -> float | None:
    """GoPlus reports taxes as a fraction string ("0.05"), sometimes empty."""
    if value in (None, "", "-"):
        return None
    try:
        return round(float(value) * 100, 1)
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool:
    return str(value or "").strip() == "1"


def assess(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn one GoPlus record into a verdict plus the reasons behind it.

    The reasons matter as much as the verdict: "blocked" with no explanation
    is indistinguishable from a bug, and a warning nobody can interpret gets
    ignored, which is worse than not showing it.
    """
    buy_tax = _pct(raw.get("buy_tax"))
    sell_tax = _pct(raw.get("sell_tax"))

    blockers: list[str] = []
    warnings: list[str] = []

    if _flag(raw.get("is_honeypot")):
        blockers.append("Honeypot: the contract does not permit selling.")
    if _flag(raw.get("cannot_sell_all")):
        warnings.append("The contract blocks selling the whole position at once.")
    if _flag(raw.get("transfer_pausable")):
        warnings.append("Transfers can be paused by the owner at any time.")
    if _flag(raw.get("is_blacklisted")):
        warnings.append("The owner can blacklist addresses, including yours.")

    if sell_tax is not None and sell_tax >= SEVERE_TAX_PCT:
        # Not a blocker: it is sellable, just expensive, and that is a
        # judgement about size rather than about possibility.
        warnings.append(f"Sell tax {sell_tax}% — a fifth of the position or more.")
    elif sell_tax is not None and sell_tax >= NOTABLE_TAX_PCT:
        warnings.append(f"Sell tax {sell_tax}%.")
    if buy_tax is not None and buy_tax >= NOTABLE_TAX_PCT:
        warnings.append(f"Buy tax {buy_tax}%.")

    if _flag(raw.get("is_mintable")):
        warnings.append("Mint authority is open — the supply can still grow.")
    if _flag(raw.get("can_take_back_ownership")):
        warnings.append("Ownership can be reclaimed after being renounced.")
    if _flag(raw.get("owner_change_balance")):
        warnings.append("The owner can change balances directly.")
    if _flag(raw.get("hidden_owner")):
        warnings.append("The contract has a hidden owner.")
    if raw.get("is_open_source") is not None and not _flag(raw.get("is_open_source")):
        warnings.append("Source code is not verified.")

    locked = _locked_share(raw.get("lp_holders"))
    if locked is not None and locked < 50.0:
        warnings.append(f"Only {locked}% of the LP is locked or burned.")

    holders = _to_int(raw.get("holder_count"))
    top = _top_holder_share(raw.get("holders"))
    if top is not None and top >= 50.0:
        warnings.append(f"One holder controls {top}% of the supply.")

    return {
        "checked": True,
        "blocked": bool(blockers),
        "blockers": blockers,
        "warnings": warnings,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "holder_count": holders,
        "top_holder_pct": top,
        "lp_locked_pct": locked,
        "open_source": _flag(raw.get("is_open_source")),
        "symbol": raw.get("token_symbol") or None,
    }


def unchecked(reason: str) -> dict[str, Any]:
    """The verdict for a token nothing could be learned about.

    Never ``blocked``: an outage is not evidence of a scam, and a screening
    service that fails closed would take the whole system down with it.
    """
    return {
        "checked": False,
        "blocked": False,
        "blockers": [],
        "warnings": [],
        "reason": reason,
        "buy_tax": None,
        "sell_tax": None,
        "holder_count": None,
        "top_holder_pct": None,
        "lp_locked_pct": None,
        "open_source": None,
        "symbol": None,
    }


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _locked_share(holders: Any) -> float | None:
    """Share of the LP held by a locker contract or burned."""
    if not isinstance(holders, list) or not holders:
        return None
    total = 0.0
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        if _flag(holder.get("is_locked")):
            try:
                total += float(holder.get("percent") or 0)
            except (TypeError, ValueError):
                continue
    return round(total * 100, 1)


def _top_holder_share(holders: Any) -> float | None:
    """Largest non-LP, non-contract holder as a percentage of supply."""
    if not isinstance(holders, list) or not holders:
        return None
    best = 0.0
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        # A locked or contract-held balance is not one person able to dump.
        if _flag(holder.get("is_locked")) or _flag(holder.get("is_contract")):
            continue
        try:
            best = max(best, float(holder.get("percent") or 0))
        except (TypeError, ValueError):
            continue
    return round(best * 100, 1) if best else None


def _fresh_after(seconds: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds")


async def screen(
    chain: Chain, addresses: Iterable[str], *, refresh: bool = False
) -> dict[str, dict[str, Any]]:
    """Risk verdicts per token address. Failures come back unchecked, not unsafe."""
    wanted = sorted({address.lower() for address in addresses if address})
    if not wanted:
        return {}

    chain_id = CHAIN_IDS.get(chain)
    if chain_id is None:
        return {
            address: unchecked(f"{chain.value} is not screened by GoPlus")
            for address in wanted
        }

    results: dict[str, dict[str, Any]] = {}
    if not refresh:
        cached = db.get_token_risks(
            chain=chain.value,
            addresses=wanted,
            fresh_after=_fresh_after(VERDICT_TTL_SECONDS),
        )
        recent = set(
            db.get_token_risks(
                chain=chain.value,
                addresses=wanted,
                fresh_after=_fresh_after(UNCHECKED_TTL_SECONDS),
            )
        )
        results = {
            address: verdict
            for address, verdict in cached.items()
            # A real verdict keeps for hours. A failure keeps for minutes: it
            # records that the API was down, not that the token is unknowable.
            if verdict.get("checked") or address in recent
        }

    missing = [address for address in wanted if address not in results]
    if not missing:
        return results

    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=httpx.Timeout(20.0, connect=10.0)
    ) as client:
        for address in missing:
            try:
                response = await client.get(
                    f"/token_security/{chain_id}",
                    params={"contract_addresses": address},
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                log.warning("GoPlus lookup failed for %s: %s", address, error)
                verdict = unchecked("the risk API did not answer")
            else:
                found = payload.get("result") or {}
                # GoPlus echoes addresses lowercased, but not on every chain.
                record = next(
                    (
                        value
                        for key, value in found.items()
                        if isinstance(value, dict) and str(key).lower() == address
                    ),
                    None,
                )
                verdict = (
                    assess(record) if record else unchecked("no record for this token")
                )
            db.save_token_risk(
                chain=chain.value, token_address=address, verdict=verdict
            )
            results[address] = verdict

    return results


__all__ = [
    "CHAIN_IDS",
    "UNCHECKED_TTL_SECONDS",
    "VERDICT_TTL_SECONDS",
    "assess",
    "screen",
    "supported",
    "unchecked",
]
