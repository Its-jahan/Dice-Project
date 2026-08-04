"""Addresses that are never a trader, and must never sit in a cohort.

Why a list and not a heuristic
------------------------------
The holder query can already exclude smart contracts, and the wallet score
already flags a "sprayer" — an address that buys hundreds of tokens and hits
nothing. Both help, and neither is sufficient:

* the contract filter only applies to cohorts built *with it switched on*, and
  the ones built before it existed keep their contracts forever;
* the sprayer flag is advisory and needs history, so a fresh cohort carries a
  router for weeks before enough evidence accumulates to notice.

Meanwhile an exchange hot wallet is not a contract at all and buys nothing —
it *receives* — so neither check sees it, and it lands in early-buyer cohorts
simply because tokens flow through it.

What this costs when it is wrong
--------------------------------
Everything downstream is a count. One router inside the watched set inflates
the pool (raising the threshold every real wallet must clear), contributes to
signals it had no opinion about, and distorts the overlap between cohorts that
appear to share a member. Measured on the live install: a single address had
bought 744 distinct tokens, and a review of the first two signals found that a
router had contributed to *both* of them.

So the rule is applied twice — when wallets enter a cohort, and again when
wallets are counted towards a signal. The second is what repairs cohorts that
already exist, without rewriting anyone's stored data.
"""

from __future__ import annotations

#: Address -> what it is. Lowercased, EVM only; Solana equivalents would need
#: their own entries and are not guessed at here.
#:
#: Deliberately conservative. A wrong entry silently removes a real trader,
#: which is far harder to notice than a router that slipped through — the
#: sprayer flag and the contract filter still catch what this misses.
NEVER_WATCH: dict[str, str] = {
    # Routers and settlement contracts: they hold tokens mid-trade on behalf
    # of whoever called them, so every swap looks like a purchase.
    "0x000000000004444c5dc75cb358380d2e3de08a90": "Uniswap v4 PoolManager",
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "CoW Protocol Settlement",
    "0x00000000009726632680fb29d3f7a9734e3010e2": "Rainbow Router",
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2 Router",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3 Router",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap V3 Router 2",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap Universal Router",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch v5 Router",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x Exchange Proxy",
    "0x881d40237659c251811cec9c364ef91dc08d300c": "MetaMask Swap Router",
    # Exchange hot wallets: they receive constantly and decide nothing. These
    # are not contracts, so a contract filter never removes them.
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance hot wallet",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance hot wallet",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance hot wallet",
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be": "Binance",
    "0xd551234ae421e3bcba99a0da6d736074f22192ff": "Binance",
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f": "Binance",
    "0x9696f59e4d72e237be84ffd425dcad154bf96976": "Binance",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase",
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase",
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740": "Coinbase",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase",
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2": "Kraken",
    "0x0d0707963952f2fba59dd06f2b425ace40b492fe": "Gate.io",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit hot wallet",
}


def is_never_watched(address: str) -> bool:
    return address.strip().lower() in NEVER_WATCH


def label(address: str) -> str | None:
    return NEVER_WATCH.get(address.strip().lower())


def drop(addresses: list[str]) -> list[str]:
    """Remove the addresses that are never a trader, preserving order."""
    return [a for a in addresses if not is_never_watched(a)]


def found_in(addresses: list[str]) -> dict[str, str]:
    """Which of these are on the list, and what each one is.

    Used to tell someone *why* their cohort shrank, rather than silently
    handing back fewer wallets than they supplied.
    """
    return {
        a.strip().lower(): NEVER_WATCH[a.strip().lower()]
        for a in addresses
        if is_never_watched(a)
    }


__all__ = ["NEVER_WATCH", "drop", "found_in", "is_never_watched", "label"]
