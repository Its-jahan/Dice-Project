"""Alchemy Notify (webhook) client — the real-time half of DICE.

Why this exists
---------------
The Dune monitor answers "what did these wallets buy?" only when asked, so a
signal is at best one polling interval old — with a 24 h interval that is 12 h
of average delay, far more than Dune's own lag. Alchemy's **Address Activity**
webhooks invert that: register the watched wallets once and Alchemy POSTs to
this server the moment any of them receives a token, seconds after the block.

Shape of the integration
------------------------
One webhook per **network**, holding the union of the wallets of every
realtime-enabled watchlist on that chain — Alchemy webhooks are per-network,
and one wallet may sit in several watchlists.

    POST   /api/create-webhook            -> {id, signing_key, ...}
    PATCH  /api/update-webhook-addresses  -> add/remove addresses
    DELETE /api/delete-webhook

Both the management API (``X-Alchemy-Token``, from the webhooks dashboard) and
the delivery signature (``X-Alchemy-Signature``, HMAC-SHA256 of the raw body
with the webhook's signing key) are handled here.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from .models import Chain

log = logging.getLogger(__name__)

NOTIFY_BASE = "https://dashboard.alchemy.com/api"

#: DICE chain -> Alchemy network identifier. Alchemy names networks itself and
#: adds new ones regularly; a chain missing here simply cannot be watched live
#: (the UI says so) and still works through the Dune monitor. If Alchemy
#: rejects a value the error is surfaced verbatim rather than swallowed, so a
#: renamed network is obvious.
ALCHEMY_NETWORKS: dict[Chain, str] = {
    Chain.ethereum: "ETH_MAINNET",
    Chain.polygon: "MATIC_MAINNET",
    Chain.arbitrum: "ARB_MAINNET",
    Chain.optimism: "OPT_MAINNET",
    Chain.base: "BASE_MAINNET",
    Chain.bnb: "BNB_MAINNET",
    Chain.avalanche_c: "AVAX_MAINNET",
    Chain.gnosis: "GNOSIS_MAINNET",
    Chain.linea: "LINEA_MAINNET",
    Chain.mantle: "MANTLE_MAINNET",
    Chain.unichain: "UNICHAIN_MAINNET",
    Chain.sonic: "SONIC_MAINNET",
    Chain.ink: "INK_MAINNET",
    Chain.celo: "CELO_MAINNET",
}

#: Addresses sent per management call. Alchemy accepts large batches, but a
#: modest chunk keeps one oversized watchlist from tripping a request limit and
#: makes a partial failure easy to retry.
ADDRESS_BATCH = 500


class AlchemyError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def network_for(chain: Chain) -> str | None:
    return ALCHEMY_NETWORKS.get(chain)


def supported_chains() -> list[str]:
    return sorted(chain.value for chain in ALCHEMY_NETWORKS)


def verify_signature(raw_body: bytes, signature: str | None, signing_key: str) -> bool:
    """Check ``X-Alchemy-Signature`` against the raw request body.

    The digest is over the bytes exactly as received — re-serialising the JSON
    changes whitespace and key order and the hash no longer matches.
    """
    if not signature or not signing_key:
        return False
    digest = hmac.new(
        signing_key.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, signature.strip())


class AlchemyNotifyClient:
    """Thin async wrapper around the webhook management endpoints."""

    def __init__(self, auth_token: str, *, base_url: str = NOTIFY_BASE) -> None:
        if not auth_token or not auth_token.strip():
            raise AlchemyError("no Alchemy auth token supplied", status_code=401)
        self._token = auth_token.strip()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "X-Alchemy-Token": self._token,
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> "AlchemyNotifyClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise AlchemyError(f"could not reach Alchemy: {exc}", status_code=503)

        if response.status_code in (401, 403):
            raise AlchemyError(
                "Alchemy rejected the auth token — copy it from the top of the "
                "webhooks dashboard (it is not the app API key).",
                status_code=401,
            )
        if response.status_code >= 400:
            # Carry the real status, not just print it in the message. A caller
            # that needs to tell "this webhook is gone" (404) from "Alchemy is
            # unhappy" cannot parse prose, and the default of 502 silently made
            # every such check unreachable.
            raise AlchemyError(
                f"Alchemy returned {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    async def create_address_webhook(
        self, *, network: str, webhook_url: str, addresses: list[str]
    ) -> dict[str, Any]:
        """Create the webhook and return ``{id, signing_key, ...}``.

        Alchemy requires at least one address at creation time, so the caller
        seeds it with the first batch and adds the rest afterwards.
        """
        payload = {
            "network": network,
            "webhook_type": "ADDRESS_ACTIVITY",
            "webhook_url": webhook_url,
            "addresses": addresses[:ADDRESS_BATCH],
        }
        data = await self._request("POST", "/create-webhook", json=payload)
        webhook = data.get("data") or {}
        if not webhook.get("id"):
            raise AlchemyError("Alchemy did not return a webhook id")
        return webhook

    async def update_addresses(
        self, webhook_id: str, *, add: list[str], remove: list[str]
    ) -> None:
        """Add and/or remove addresses, in batches. Idempotent on Alchemy's side."""
        chunks = [
            add[index : index + ADDRESS_BATCH]
            for index in range(0, len(add), ADDRESS_BATCH)
        ] or [[]]
        for position, chunk in enumerate(chunks):
            # Removals ride along with the first call; later calls only add.
            removals = remove if position == 0 else []
            if not chunk and not removals:
                continue
            await self._request(
                "PATCH",
                "/update-webhook-addresses",
                json={
                    "webhook_id": webhook_id,
                    "addresses_to_add": chunk,
                    "addresses_to_remove": removals,
                },
            )

    async def list_addresses(self, webhook_id: str, *, limit: int = 100) -> list[str]:
        """Every address currently registered, following Alchemy's cursor."""
        addresses: list[str] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {"webhook_id": webhook_id, "limit": limit}
            if after:
                params["after"] = after
            data = await self._request("GET", "/webhook-addresses", params=params)
            page = data.get("data") or []
            addresses.extend(str(item) for item in page)
            pagination = data.get("pagination") or {}
            after = (pagination.get("cursors") or {}).get("after")
            if not after or not page:
                return addresses
            if after in seen_cursors:
                # A cursor that does not advance would loop forever against a
                # remote API; take what we have rather than hang the sync.
                log.warning("Alchemy address cursor stalled for %s", webhook_id)
                return addresses
            seen_cursors.add(after)

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._request(
            "DELETE", "/delete-webhook", params={"webhook_id": webhook_id}
        )
