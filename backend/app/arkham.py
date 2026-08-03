"""Arkham Intelligence: who is behind an address.

What this adds
--------------
Everything else in DICE treats a wallet as an opaque address. Arkham knows
that a given address is "Binance hot wallet 7" or "Jump Trading" or a labelled
fund. That matters for signals: an exchange hot wallet receiving a token is not
a conviction buy, it is a customer deposit, and a handful of those in a
watchlist quietly poison the co-buy count. Labels let those be excluded and let
genuinely notable buyers stand out.

What it does *not* add
----------------------
Historical holders. The token-holders endpoint reports a current snapshot, so
it cannot answer "who held this on 7 January 2023" — that stays with the Dune
paths (see :func:`app.sql.build_transfer_snapshot_sql`).

Access is by application and metered, so every call site treats a missing or
rejected key as "no labels available" rather than an error worth failing over.

Response shapes are not publicly documented. :func:`raw_lookup` returns
Arkham's answer untouched so the exact shape can be confirmed against a real
key, and :func:`describe_address` is deliberately tolerant of several spellings
rather than assuming one.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import db
from .models import Chain

log = logging.getLogger(__name__)

BASE_URL = "https://api.arkm.com"

#: Arkham's chain identifiers. Mostly the same words Dune uses; the ones that
#: differ are listed explicitly.
CHAIN_SLUGS: dict[Chain, str] = {
    Chain.ethereum: "ethereum",
    Chain.arbitrum: "arbitrum_one",
    Chain.avalanche_c: "avalanche",
    Chain.base: "base",
    Chain.bnb: "bsc",
    Chain.linea: "linea",
    Chain.optimism: "optimism",
    Chain.polygon: "polygon",
    Chain.solana: "solana",
}


class ArkhamError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def chain_slug(chain: Chain) -> str:
    return CHAIN_SLUGS.get(chain, chain.value)


def api_key() -> str | None:
    return (db.get_setting("arkham_api_key") or "").strip() or None


def configured() -> bool:
    return api_key() is not None


async def _request(path: str, **params: Any) -> Any:
    key = api_key()
    if not key:
        raise ArkhamError("No Arkham API key saved.", status_code=401)
    try:
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"API-Key": key},
        ) as client:
            response = await client.get(path, params=params or None)
    except httpx.HTTPError as exc:
        raise ArkhamError(f"Could not reach Arkham: {exc}", status_code=503) from exc

    if response.status_code in (401, 403):
        raise ArkhamError(
            "Arkham rejected the API key. Generate one at intel.arkm.com → "
            "Settings → API Keys (access is granted by application).",
            status_code=401,
        )
    if response.status_code == 429:
        raise ArkhamError("Arkham rate limit hit; retry shortly.", status_code=429)
    if response.status_code >= 400:
        raise ArkhamError(
            f"Arkham returned {response.status_code}: {response.text[:300]}"
        )
    try:
        return response.json()
    except ValueError:
        raise ArkhamError("Arkham returned a non-JSON response")


async def raw_lookup(chain: Chain, address: str) -> Any:
    """Arkham's answer for one address, untransformed.

    Exists so the response shape can be inspected against a real key rather
    than guessed at — :func:`describe_address` parses whatever this returns.
    """
    return await _request(f"/intelligence/address/{address}", chain=chain_slug(chain))


def _first(payload: Any, *keys: str) -> Any:
    """Pull the first present key, tolerating Arkham's nesting variations."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if payload.get(key) not in (None, "", []):
            return payload[key]
    return None


def describe_address(payload: Any, chain: Chain) -> dict[str, Any]:
    """Reduce a lookup to the label fields DICE cares about.

    Written defensively: Arkham nests the answer per chain in some responses
    and returns it flat in others, and entity/label objects carry either a
    ``name`` or an ``id``. Anything unrecognised yields empty labels rather
    than an exception, because a missing label must never break a signal.
    """
    if isinstance(payload, dict):
        # Some responses key the body by chain slug.
        nested = payload.get(chain_slug(chain))
        if isinstance(nested, dict):
            payload = nested

    entity = _first(payload, "arkhamEntity", "entity") or {}
    label = _first(payload, "arkhamLabel", "label") or {}

    entity_name = _first(entity, "name", "id") if isinstance(entity, dict) else None
    label_name = _first(label, "name", "id") if isinstance(label, dict) else None
    if isinstance(entity, str):
        entity_name = entity
    if isinstance(label, str):
        label_name = label

    entity_type = (
        _first(entity, "type", "category") if isinstance(entity, dict) else None
    )

    return {
        "entity": entity_name,
        "entity_type": entity_type,
        "label": label_name,
        # Exchange and bridge wallets move tokens on behalf of other people, so
        # counting them as conviction buyers is a category error.
        "is_service": str(entity_type or "").lower()
        in {"cex", "exchange", "bridge", "custodian", "otc"},
    }


async def label_address(chain: Chain, address: str) -> dict[str, Any] | None:
    """Labels for one address, or None when Arkham cannot answer."""
    try:
        payload = await raw_lookup(chain, address)
    except ArkhamError as exc:
        log.info("arkham lookup failed for %s: %s", address[:10], exc)
        return None
    return describe_address(payload, chain)


__all__ = [
    "ArkhamError",
    "api_key",
    "chain_slug",
    "configured",
    "describe_address",
    "label_address",
    "raw_lookup",
]
