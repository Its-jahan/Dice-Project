"""Solana live monitoring through Helius webhooks.

Why a second provider
---------------------
Alchemy Notify's Address Activity is an EVM product; it has no Solana
equivalent, so the live path simply stops at the chain boundary. Helius is the
Solana-native equivalent — register wallet addresses, receive a POST per
transaction touching them — and it is the only piece missing for Solana to
behave like every EVM chain in DICE.

The events this produces are deliberately the *same shape* as the Alchemy
ones, so everything downstream — the event store, the pooled threshold, the
board, the outcome tracking — works on Solana without knowing Solana exists.

How deliveries are authenticated
--------------------------------
Helius does not sign bodies. It sends back a fixed ``Authorization`` header
that you chose when creating the webhook, and that is the whole check. That is
weaker than Alchemy's HMAC — anyone replaying a captured request gets in — so
the secret is generated at full length here rather than being anything
guessable, and it is compared in constant time. The endpoint stays idempotent
regardless: a replayed delivery re-inserts events that are already keyed by
signature, and changes nothing.

Verification status
-------------------
The request and payload shapes here follow Helius's documented enhanced
webhook format. They have **not** been exercised against a live Helius
account, because that needs an API key. The parser is covered by tests built
from the documented shape; the client is not, and the first real delivery is
the thing that proves it. ``/api/settings/realtime`` reports the webhook back
so a mismatch shows up as an explicit failure rather than as silence.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import Any, Iterable

import httpx

from .models import Chain

log = logging.getLogger(__name__)

BASE_URL = "https://api.helius.xyz/v0"

#: Helius accepts up to this many addresses on one webhook.
MAX_ADDRESSES = 100_000

#: Solana's native mint, the equivalent of "external" native transfers on EVM.
#: A wallet paying in SOL is paying, exactly like one paying in ETH.
WRAPPED_SOL = "So11111111111111111111111111111111111111112"


class HeliusError(RuntimeError):
    """A Helius API call failed in a way worth showing the operator."""


def new_auth_secret() -> str:
    """A delivery secret. Long because it is the *only* thing guarding the URL."""
    return secrets.token_urlsafe(32)


def verify_auth(header: str | None, expected: str) -> bool:
    """Constant-time comparison of the delivery secret."""
    if not header or not expected:
        return False
    # Helius sends the value verbatim, but operators paste "Bearer x" often
    # enough that accepting both avoids a failure mode that looks like silence.
    candidate = header[7:] if header.lower().startswith("bearer ") else header
    return hmac.compare_digest(candidate.strip(), expected.strip())


class HeliusClient:
    """Minimal Helius webhook client: create, replace addresses, delete."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "HeliusClient":
        self._client = httpx.AsyncClient(
            base_url=BASE_URL, timeout=httpx.Timeout(30.0, connect=10.0)
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._client is None:  # pragma: no cover - misuse
            raise RuntimeError("HeliusClient must be used as a context manager")
        params = {"api-key": self._api_key}
        try:
            response = await self._client.request(
                method, path, params=params, **kwargs
            )
        except httpx.HTTPError as error:
            raise HeliusError(f"Helius did not answer: {error}") from error
        if response.status_code >= 400:
            # Helius puts the reason in the body; surfacing the status alone
            # turns "your key has no webhook quota" into an unhelpful 400.
            detail = response.text.strip()[:300] or response.reason_phrase
            raise HeliusError(f"Helius returned {response.status_code}: {detail}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as error:  # pragma: no cover - defensive
            raise HeliusError("Helius returned a non-JSON body") from error

    async def create_webhook(
        self, *, webhook_url: str, addresses: Iterable[str], auth_secret: str
    ) -> dict[str, Any]:
        payload = {
            "webhookURL": webhook_url,
            "transactionTypes": ["Any"],
            "accountAddresses": sorted(addresses)[:MAX_ADDRESSES],
            "webhookType": "enhanced",
            "authHeader": auth_secret,
        }
        return await self._request("POST", "/webhooks", json=payload)

    async def set_addresses(
        self, webhook_id: str, *, webhook_url: str, addresses: Iterable[str],
        auth_secret: str,
    ) -> dict[str, Any]:
        """Replace the watched set.

        Helius has no add/remove endpoint — the edit call takes the whole list
        — so this sends the full set every time. That is a feature here: it
        cannot drift out of step with the watchlists the way an incremental
        reconciliation can.
        """
        payload = {
            "webhookURL": webhook_url,
            "transactionTypes": ["Any"],
            "accountAddresses": sorted(addresses)[:MAX_ADDRESSES],
            "webhookType": "enhanced",
            "authHeader": auth_secret,
        }
        return await self._request("PUT", f"/webhooks/{webhook_id}", json=payload)

    async def get_webhook(self, webhook_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/webhooks/{webhook_id}")

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._request("DELETE", f"/webhooks/{webhook_id}")


def parse_activity(
    payload: Any, *, watched: set[str], seen_at: str
) -> list[dict[str, Any]]:
    """Turn an enhanced-webhook delivery into the same events the EVM path makes.

    Helius sends a JSON *array* of transactions, each with ``tokenTransfers``
    and ``nativeTransfers`` already decoded — so unlike the EVM path there is
    no need to infer a swap from raw logs. The buy/sell rules are identical:

    * a token arriving is a **buy** when the wallet also sent something out in
      the same transaction, and an airdrop when it did not;
    * a token leaving is a **sale** when something other than that token came
      back in the same transaction, and a plain transfer otherwise;
    * a move between two watched wallets is neither.
    """
    transactions = payload if isinstance(payload, list) else [payload]
    events: list[dict[str, Any]] = []

    for transaction in transactions:
        if not isinstance(transaction, dict):
            continue
        signature = str(transaction.get("signature") or "").strip()
        if not signature:
            continue
        slot = transaction.get("slot")

        token_transfers = [
            item
            for item in (transaction.get("tokenTransfers") or [])
            if isinstance(item, dict)
        ]
        native_transfers = [
            item
            for item in (transaction.get("nativeTransfers") or [])
            if isinstance(item, dict)
        ]

        # What each watched wallet paid out and received in this transaction.
        paid: set[str] = set()
        received: dict[str, set[str]] = {}
        for item in token_transfers:
            sender = str(item.get("fromUserAccount") or "").strip()
            recipient = str(item.get("toUserAccount") or "").strip()
            mint = str(item.get("mint") or "").strip()
            if sender in watched:
                paid.add(sender)
            if recipient in watched and mint:
                received.setdefault(recipient, set()).add(mint)
        for item in native_transfers:
            sender = str(item.get("fromUserAccount") or "").strip()
            recipient = str(item.get("toUserAccount") or "").strip()
            if sender in watched:
                paid.add(sender)
            if recipient in watched:
                # SOL coming back is the proceeds of a sale, exactly as ETH is.
                received.setdefault(recipient, set()).add(WRAPPED_SOL)

        for item in token_transfers:
            mint = str(item.get("mint") or "").strip()
            if not mint:
                continue
            sender = str(item.get("fromUserAccount") or "").strip()
            recipient = str(item.get("toUserAccount") or "").strip()
            inbound = recipient in watched
            outbound = sender in watched
            if not inbound and not outbound:
                continue
            if inbound and outbound:
                continue  # one position moving inside the watched set

            wallet = recipient if inbound else sender
            got_back = received.get(wallet, set()) - {mint}
            events.append(
                {
                    "chain": Chain.solana.value,
                    "wallet_address": wallet,
                    "token_address": mint,
                    # Solana's transaction signature is its hash.
                    "tx_hash": signature,
                    "token_symbol": None,
                    "amount": _to_float(item.get("tokenAmount")),
                    "block_num": int(slot) if isinstance(slot, int) else None,
                    "seen_at": seen_at,
                    "from_address": sender or None,
                    "is_buy": inbound and wallet in paid,
                    "is_sell": outbound and bool(got_back),
                }
            )
    return events


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "HeliusClient",
    "HeliusError",
    "MAX_ADDRESSES",
    "new_auth_secret",
    "parse_activity",
    "verify_auth",
]
