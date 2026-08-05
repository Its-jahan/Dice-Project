# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn app.main:app --reload --port 8000   # dev server
.venv/bin/python -m pytest                                      # whole suite (~30s)
.venv/bin/python -m pytest tests/test_realtime.py               # one file
.venv/bin/python -m pytest -k "pool_age"                        # by name
.venv/bin/python -m pytest tests/test_ai.py::test_the_theme_is_reduced_to_one_grouping_key
```

No linter or formatter is configured, and the frontend has no build step —
`frontend/` is plain HTML/JS with a vendored Bootstrap, served by FastAPI.

Deploy (idempotent, run as root on the server, from a checkout of `main`):

```bash
bash deploy/install.sh <domain-or-ip> [--self-signed] [--with-graph] [--password <user>]
```

`--self-signed` **fails deliberately** when a real Let's Encrypt certificate
already exists. Downgrading TLS breaks Alchemy webhook delivery silently, so
the installer refuses rather than obeys.

## The one-sentence architecture

Find wallets that were early to a token, watch what they buy next, and fire a
signal when enough of them independently buy the same new token — then record
whether that signal was right.

## The pipeline, and why it is split the way it is

Three stages, deliberately decoupled. Getting this wrong is the most expensive
mistake available in this codebase.

1. **Record** — `POST /api/webhooks/alchemy` verifies the HMAC signature,
   decodes transfers, writes `wallet_events`, returns. It makes **no external
   calls at all**.
2. **Evaluate** — `realtime.sweep()`, every `sweep_seconds` (default 60).
   Counts wallets per token *in SQLite first* (free), then looks up only the
   handful already over the threshold.
3. **Notify** — Telegram immediately; the AI research brief follows as a
   second message, because a brief takes ~55s of web search.

The ordering is the point. Asking the expensive question (DexScreener price,
GoPlus contract screen) before the free one (does any token have enough
buyers?) burned the entire API budget on tokens two wallets had touched:
measured at 1,184 distinct tokens per five minutes, almost none of which could
ever signal. **Never move external lookups into the delivery path.**

Consequences worth knowing before changing behaviour: a signal is up to
`sweep_seconds` late, and its recorded entry price is the price at evaluation
rather than at the instant of crossing.

## Two detection paths, one signals table

- `monitor.py` — the **Dune** path. Scheduled, costs credits, up to one
  interval stale.
- `realtime.py` — the **live** path. Alchemy webhooks (EVM) and Helius
  (Solana); the decode differs, everything after it is shared.

Both converge on the same `signals` table and the same notification, so a
watchlist can run either or both.

## Signals are pooled, not per-watchlist

Every live watchlist's wallets union into one pool (`db.realtime_wallets`).
A signal fires when `pool_pct` of the *whole pool* buys one token inside the
window, with `pool_min_wallets` as a floor. The signal still records which
watchlists the buyers came from (`breakdown`), so attribution survives pooling.

This means adding wallets **raises** the bar for every signal. A large,
undifferentiated cohort makes signals rarer, not better.

## The gate stack

A token must clear all of these, in this order, cheapest first:

1. **Paid for in the same transaction** (`is_buy`) — separates a purchase from
   an airdrop blasted at thousands of addresses.
2. **Threshold** — counted locally in SQLite.
3. **Liquidity** — DexScreener; no pool means nobody bought it.
4. **Pool age** (`max_pool_age_hours`) — the count alone cannot express
   "early". On a large pool, five-year-old blue chips attract more simultaneous
   buyers than a real launch does, so no threshold separates them; only age
   does.
5. **Contract risk** (`security.py`, GoPlus) — only unsellability *blocks*.
   Everything else rides along as a warning, because a gate that silently
   swallows signals is indistinguishable from a broken webhook.

Failures never block: an unreachable API leaves a token unchecked rather than
condemned, and an unknown pool age is never treated as old.

## The measurement layer

This is what makes every threshold falsifiable rather than a guess.

- `performance.py` — stamps entry price, liquidity and pool age when a signal
  **first** fires (`INSERT OR IGNORE`; a strengthened signal is the same
  opportunity), then fills 1h/24h/7d. Reports **median**, not mean — one 40x
  would otherwise make four losses look like a strategy.
- `wallets.py` — scores each wallet on hit rate, median return and *lead time*,
  with the return shrunk toward zero by how thin the evidence is, so one lucky
  hit cannot top the table.
- `cohorts.py` — overlap, containment and lift between watchlists; builds the
  derived "repeat wallets" cohort.
- `infra.py` — routers, settlement contracts and exchange hot wallets that must
  never be counted. Applied both at cohort entry *and* at
  `db.realtime_wallets`, the second of which repairs cohorts built before the
  filter existed.

## The exit half (`exits.py`)

Everything else decides when to *enter*. This is the only thing that says a
position has stopped being one: when enough of the wallets **named on a
signal** have sold since it fired, Telegram says so.

- It counts the signal's own buyers, not the pool. "Is the thesis I acted on
  intact" is a different question from "is anyone selling anything".
- `since_iso` is the signal's own `first_seen_at`. A sale from *before* it
  fired is a wallet that rebought, not one leaving.
- Two bars: a percentage (the cohort is going, not one member rotating) and a
  floor (a third of three buyers is one wallet).
- One alert per signal, enforced by the existence of a `signal_exits` row.
  Claim before sending — the reverse order re-sends forever if a send fails.

**`signals.buyers` is a list of TokenBuyer objects, not addresses.** Reading it
as addresses fails silently: every wallet becomes the string form of a dict,
matches nothing, and the count is zero forever while the code looks fine.
Measured on live data: the naive parse found 0 sellers where the correct one
found 5.

## The AI layer's boundaries (`ai.py`)

The model **never** decides to buy, computes a threshold, or replaces the
liquidity/contract gates — detection is set arithmetic and must stay testable.
It does two things: research what a signalled token is (with web search), and
review measured outcomes to propose parameter changes.

Briefs and the review take **separate models**; the review defaults to the
strong one even when briefs run on a cheap one. Measured on identical data, a
cheap model answered "confidence: high" on two unmatured signals where the
strong one answered "confidence: none". Either an OpenRouter (`sk-or-…`) or an
Anthropic (`sk-ant-…`) key works; the prefix picks the backend.

## The front door (`auth.py`)

One password, no username — a single-user deployment, and a username field that
accepts anything teaches the wrong thing about what is checked. scrypt from the
standard library; session tokens stored **hashed**; failed attempts counted in
SQLite so restarting the process is not a way to pick the lock.

Two properties are load-bearing and both fail silently if broken:

- **`OPEN_PATHS` must keep the webhooks open.** Alchemy cannot present a cookie,
  and it already proves itself with an HMAC over the raw body. Gating
  `/api/webhooks/*` stops every signal while the UI looks perfectly healthy.
  `test_the_webhooks_stay_open_without_a_cookie` is the guard.
- **No password set means no gate**, so an existing install cannot lock itself
  out on upgrade. It opts in by setting one.

The API answers **401**; pages **303** to `/login`. A fetch cannot do anything
useful with a redirect to HTML, and a browser cannot do anything useful with a
401 body. `login.html` is a real form POST with server-rendered errors — it is
the one page that must work with JavaScript switched off.

## Repo-specific traps

- **Dune's balance table is a rolling ~3-week window**, not an archive. Any
  historical range needs `history_source=transfers`, which reads every transfer
  the token ever had — slow but correct. A query returning nothing for a 2023
  date is this, not a bug.
- **Runtime settings live in SQLite, not just env.** The idiom throughout is
  `db.get_setting("x")` falling back to `settings.x`. Adding a knob means both,
  plus the `/api/settings/...` endpoint, or it silently cannot be changed
  without a redeploy.
- **The live database is `/var/lib/dice/dice.db`**, set by the systemd unit —
  not the repo's `backend/data/dice.db`. A script run as root on the server
  reads the wrong one unless `DICE_DB_PATH` is set.
- **Deliveries are recorded in `webhook_deliveries` but pruned**, so the row
  count is a window, not a total; the highest `id` is the real total.
- `graphify-out/` is a generated code map, rebuilt on deploy. It can be stale
  relative to the working tree — verify anything surprising against the source.

## Testing conventions

Tests are behavioural and named as sentences describing the guarantee
(`test_an_old_pool_cannot_signal_however_many_wallets_buy_it`). The valuable
ones are usually negative: no key, a timeout, a refusal, an outage — the system
must behave exactly as if the feature were absent.

Two things that have caused wrong-but-passing tests here:

- **Test the path, not the handler.** A test that hand-builds an exception with
  `status_code=404` passed against a client that never set the status, so the
  recovery it covered could never fire in production. Drive the real code path.
- **Build fixtures through the real model.** The exit tests passed while the
  feature could not work: the fixture wrote `buyers` as bare addresses and the
  code read bare addresses, so both agreed and both were wrong about the
  schema. `tests/test_exits.py::_signal` now serialises a real `TokenBuyer`.
- **A delivery no longer fires a signal.** Tests must `POST /api/live/sweep`
  between buying and asserting — the shared `_settle(client)` helper does this.

No network in the suite: Dune is stubbed, DexScreener/GoPlus/OpenRouter are
monkeypatched or driven through `httpx.MockTransport`.
