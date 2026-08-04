# DICE — historical token holders

DICE takes a chain, a token address and a date range, and returns **every
address that had a positive balance of that token on each day of the range**,
exported as CSV / Excel / JSON / HTML.

The key design decision: DICE reads **daily balance snapshots**, not transfers.
A wallet that bought before the range and simply held through it emits no
transfers inside the window — a transfer-based query would silently drop
exactly the holders that matter most.

> **Holder** = an address whose end-of-day (UTC) balance is greater than zero
> (or greater than the requested minimum).

An end date later than today is clamped to today, and the response says so. A
still-current balance row carries `valid_to IS NULL`, which matches *every*
future calendar day — unclamped, a future end date would project today's
balances forward and emit snapshots for days that have not happened.

## Quick start

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000>, paste your Dune API key into the **Dune API key**
field, hit **Save key**, then run a query.

## The Dune API key lives on the server

DICE is a **single-user deployment**: the first time you press *Save key* the
key is validated against Dune and stored in the server's SQLite database.
From then on every request — the queries you start in the UI *and* the
scheduled watchlist monitoring — uses that stored key. Saving a new key
replaces the old one; *Remove* deletes it.

Details worth knowing:

- The key travels in the `X-Dune-Api-Key` **header**, never in a URL, and is
  never echoed back: `GET /api/config` reports only that a key exists plus its
  last four characters.
- A key typed into the field but not saved still works for one-off requests —
  it rides along as a header and overrides the stored key for that request.
- `DUNE_API_KEY` in the environment acts as a fallback when nothing has been
  saved through the UI.
- Because the key is stored server-side, protect the server: it sits in
  `DICE_DB_PATH` (SQLite) readable by the service user only, and the systemd
  unit runs hardened. Serve over HTTPS — the key crosses the wire when you
  save it.

## Deploying to a server

```bash
# on the server, as root
bash deploy/install.sh your-domain.example      # with a domain
bash deploy/install.sh 203.0.113.10 --self-signed   # bare IP, no domain
```

Sets up a `dice` service user, a hardened systemd unit running uvicorn on
`127.0.0.1:8000`, and an nginx reverse proxy. Re-run it to deploy updates.

TLS matters here more than for a typical app: the Dune API key travels in a
request header, so plain HTTP puts it on the wire in the clear. Without a
domain you can still get a **publicly-trusted** certificate via sslip.io — see
[deploy/README.md](deploy/README.md).

## Where the history comes from

Dune's balance tables are the fast path, but they only reach as far back as
Dune backfilled them — and Dune does not publish that date. **Check coverage**
asks directly: it reports the table's first and last day, the first and last
day your token appears in it, and how many rows exist for it. Zero rows means
the holder query cannot answer for any date.

When the balance table cannot reach a range, balances can be **rebuilt from
transfers**. A balance is just the running total of transfers in minus
transfers out, and `tokens.transfers` covers a chain's whole history. The
reconstruction emits exactly the same shape as the balance query, so buyer/
holder classification, holder modes and exports all work unchanged.

| History source | Reads | Cost |
| --- | --- | --- |
| **Auto** *(default)* | The balance table; falls back to transfers only if it returns nothing | One query, occasionally two |
| **Balance table only** | Never falls back — useful to confirm what the fast path alone knows | Cheapest |
| **Rebuild from transfers** | Every transfer the token ever had, expanded across the range | Much heavier; use for old ranges |

The response reports which source actually answered, and an empty result names
the stage that emptied it rather than guessing.

## Arkham labels (optional)

[Arkham](https://intel.arkm.com) turns an address into who is behind it —
"Binance hot wallet", a named fund, a bridge. That matters for signals: an
exchange wallet receiving a token is a customer deposit, not a conviction buy,
and a few of those quietly inflate a co-buy count.

Arkham reports holders **as they are now**, so it does not answer historical
questions — that stays with the two Dune sources above. Access is by
application and calls are metered. Paste the key in the *Arkham labels* card;
the lookup shows both the parsed labels and the raw response, because Arkham's
response shapes are not published and the parser is deliberately tolerant of
several spellings.

## Buyers versus holders

On a token that has existed for years, "who held it between 1 and 5 July" is
mostly people who bought in 2019. The **Buyers or holders** filter separates
the two:

- **Buyer** — the balance went *up* on some day of the range. That covers both
  cases: a wallet that held none and bought in, and one that already held some
  and added more.
- **Holder** — the balance only stayed flat or fell.

A wallet that sold and then bought back counts as a buyer, because an increase
did happen.

Making this work needs the balance on the day *before* the range, so the query
reads one extra day: a balance of 5,000 on 1 July says nothing by itself, but
5,000 the day before makes it a holder while 0 the day before makes it a buyer.
That day is used for classification only and never appears in the output.

Every export carries three extra columns — `wallet_type`, `opening_balance`
(what the wallet held going in, so a first-time buyer is `0`) and
`bought_amount` (how much the position grew) — which is usually more useful
than the filter itself: sort by `bought_amount` to find who accumulated most.

## Holder modes

| Mode | Meaning | Export shape |
| --- | --- | --- |
| `daily` *(default)* | Every wallet with a positive end-of-day balance, one row per (wallet, day). | Snapshot rows lead |
| `any_time` | Wallets that held at any point in the range. | Per-wallet summary leads |
| `continuous` | Wallets that held on **every** day of the range. | Per-wallet summary leads |

All three come from one Dune execution — DICE fetches daily snapshots once and
derives the modes locally, so switching modes never costs a second query.

### Output columns

Snapshots:

| wallet_address | token_address | snapshot_date | balance |
| --- | --- | --- | --- |
| 0xabc… | 0x123… | 2026-07-20 | 1540.25 |
| 0xabc… | 0x123… | 2026-07-21 | 1300.00 |

Per-wallet summary:

| wallet_address | first_date | last_date | days_held | min_balance | max_balance | avg_balance |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 0xabc… | 2026-07-20 | 2026-07-31 | 12 | 450 | 1540 | 980.5 |

`days_held` counts distinct days **inside the requested window** on which the
wallet's end-of-day balance cleared the minimum — not how long it has held the
token overall. A wallet holding since January shows `days_held = 3` for a
three-day window. Widen the window to see more.

"Holder on day D" means holding at the **end** of D. The query tests each
interval against `D + 1 day` rather than against midnight, so a wallet that
bought at 14:00 on the 21st counts as a holder on the 21st. Comparing against
the start of the day would push every intra-day acquisition to the next day.

CSV carries the leading table for the chosen mode; XLSX carries **both** plus a
`Request` sheet recording exactly what was asked for; JSON carries everything;
HTML is a standalone, filterable page with no external assets.

## Watchlists & co-buy signals

The holder query answers "who bought token X early". Watchlists answer the
follow-up question: **what are those wallets buying now?**

The loop:

1. Run a holder query over a token's first day(s) — the wallets that aped in
   early.
2. In the results card, **save those wallets as a watchlist** (if the set is
   bigger than the monitoring cap, keep the top-N holders by balance).
3. DICE re-checks the watchlist on a schedule (default every 24 h, can be as
   tight as hourly): one Dune query against the curated DEX trade tables
   (`dex.trades` / `dex_solana.trades`) asking what each wallet **bought**
   inside the buy window (default 48 h).
4. When enough distinct wallets bought the *same* token, it becomes a
   **signal**: token, buyer count, USD volume, and exactly which wallets bought
   — each tagged with how it was detected — plus a DexScreener link to eyeball
   it.

**Threshold.** A token fires when its distinct-buyer count reaches
`max(min_wallets, ceil(min_wallets_pct% × watchlist size))` — with 100 wallets
and the default 10 %, that is 10 co-buyers. Both knobs are per-watchlist and
editable in the UI.

**What counts as a buy** — picked per watchlist, changeable any time:

| Mode | What it counts | Cost per check |
| --- | --- | --- |
| **Both, labelled** *(default)* | Runs the two below together and tags every buyer `DEX buy` or `new position`. When a wallet shows up both ways, the trade wins so the USD amount is kept. | 2 Dune executions |
| **DEX swaps only** | Swaps in `dex.trades` / `dex_solana.trades` where the wallet received the token. Cleanest evidence — real money changed hands — but blind to OTC deals, CEX withdrawals and transfers between a person's own wallets. | 1 execution |
| **Any new position** | Any token whose balance went from nothing to positive inside the window, read from the same balance table as the holder query. Catches every route, including airdrops and self-transfers, and has no USD amount attached. | 1 execution |

Whichever mode you pick, wrapped-native tokens, major stables and
liquid-staking tokens are ignored (every swap "buys" USDC as its other leg),
and the watchlist's own source token is auto-ignored — remove it from the
ignore list if you want re-accumulation alerts. Buys with a *known* USD value
below `min_buy_usd` are dropped; buys with no USD price (very new tokens, and
all bare positions) always pass.

### Live monitoring (Alchemy webhooks)

The Dune monitor only looks when it is scheduled, so a signal is at most one
interval old — with a 24 h interval that is **12 h of average delay**, far more
than Dune's own data lag. Live monitoring inverts that: register the watched
wallets with Alchemy once, and it POSTs to this server the moment any of them
receives a token. The rolling-window count runs locally in SQLite, so the
signal fires **seconds after the block** and costs no Dune credits.

Setup, once:

1. In the Alchemy dashboard open **Webhooks** and copy the **auth token** from
   the top of the page — this is not your app's API key.
2. Paste it into the *Live monitoring* card along with the public HTTPS address
   of this server (Alchemy has to reach `/api/webhooks/alchemy`).
3. Switch **Live** on for a watchlist. DICE creates one webhook per network
   holding the union of the wallets of every live watchlist on that chain, and
   reconciles it whenever wallets or toggles change.

Deliveries are authenticated by `X-Alchemy-Signature` — HMAC-SHA256 of the raw
body with the webhook's signing key — so the endpoint is safe to expose. An
unknown webhook id or a bad signature is rejected before anything is stored,
and redeliveries are idempotent.

**Seeing accumulation before it fires.** A signal only exists once the
threshold is crossed, which makes the run-up invisible — a token four wallets
into a ten-wallet threshold looks identical to one nobody has touched. The
**Live accumulation** panel lists every token the watched wallets are buying,
threshold or not, with a progress bar (`4/6`) and how many buyers are still
needed. It refreshes every 20 seconds.

The window is each watchlist's own **buy window**, so if you want "10 wallets
across 3 days" rather than 48 hours, set that list's buy window to 72.

### Deliveries record; the sweep decides

A webhook delivery stores what it saw and returns. It prices nothing and
screens nothing. Every judgement — is this tradeable, is the contract safe,
has it crossed the threshold — happens on the sweep, once a minute.

That ordering is the whole point. The old path looked every token in every
delivery up on DexScreener and screened it on GoPlus *before* asking the free
question of whether any wallet count was near the threshold. Measured on a
live install: **15 deliveries a minute touching 1,184 distinct tokens every
five minutes**, almost all of them touched by two wallets and incapable of
signalling. GoPlus screens one address per request and is rate limited, so the
budget went on tokens that could never matter, and ran out.

The sweep asks the questions in the right order — it counts wallets per token
in SQLite, which is free, and looks up only the handful already over the line.

**The cost is up to one minute of latency**, and two smaller things worth
knowing: a signal's recorded entry price is the price at evaluation rather
than at the instant the threshold was crossed, and `/api/live/sweep` is what
turns stored events into signals. Against pools whose median age at signal is
34 hours, a minute is not the constraint — spending the entire API budget
before lunchtime is. `sweep_seconds` is configurable, with a floor of 15,
because every pass that finds a candidate spends the budget that runs out.

**Nothing gets missed.** Deliveries only re-check the tokens they touch, which
leaves a gap: a token can become qualified for a reason no new buy will
announce — the threshold was lowered, or wallets were added to the list after
they bought. A sweep re-checks every live watchlist's stored events against its
current threshold every few minutes (`DICE_LIVE_SWEEP_SECONDS`, default 300),
and **Re-check now** runs it on demand. It reads only local SQLite, so it costs
nothing.

**Checking it works.** The card has three tools, because "no signals yet" has
two very different causes — a broken webhook, or simply nobody buying:

| Tool | Proves |
| --- | --- |
| **Check URL is reachable** | The server calls its own public URL, so DNS, TLS and the reverse proxy are all exercised. This is the half a simulation cannot test. |
| **Send test signal** | Injects a fake `DICETEST` buy by the threshold number of wallets through the *real* ingest path — event store, threshold, signal row, Telegram push. If it works, everything downstream of Alchemy works. Dismiss the signal afterwards. |
| **Recent deliveries** | Every real delivery and its outcome: `delivered`, `bad signature`, `unknown webhook`. A webhook that arrives and fails looks nothing like one that never arrives. |

What live mode counts as a buy: a token arriving at a watched wallet **that
the wallet paid for in the same transaction**. That last part is not a detail —
it is what separates a purchase from an airdrop. Spam tokens are blasted at
thousands of addresses at a time, so without the check a single spammer paying
gas produces "20 of your 20 wallets bought this", which is both the strongest
possible signal and completely meaningless. A swap costs the wallet ETH or a
stablecoin in the same tx; an airdrop is one-way and the recipient is passive.

Two more arrivals are dropped: transfers *from* another wallet in the same
watchlist (a token passed around the group is one position, not N buyers), and
the usual stablecoin/wrapped-native stoplist.

**And it must be tradeable.** A transfer proves a token moved, not that it can
be bought. Spam tokens are minted and blasted at thousands of addresses without
ever having a liquidity pool — the DexScreener link on such a token answers
"Token or Pair Not Found", because there is nothing there. Every candidate is
checked against DexScreener before it can signal: no pool, or less than
`DICE_MIN_LIQUIDITY_USD` (default $1,000) of it, and it is out. This is ground
truth rather than a heuristic, and it is what keeps the board honest — in
testing, a poolless spam token had *more* buyers than the real one next to it.

The same lookup fills the gap the transfer feed leaves: price, liquidity and
24h volume now appear on the live board, and token links point at the actual
pair URL so they always resolve. Answers are cached for 30 minutes.

**Airdrops count, but stay labelled.** Being handed a token by a real project
is worth knowing about, so one-way arrivals do count towards a signal
(`DICE_SIGNAL_AIRDROPS`, on by default, toggleable in the live card). That is
only safe because the liquidity gate is what removes spam — a token with no
pool is dropped whether it was bought or handed out. Every signal carries a
kind, `bought` / `airdrop` / `mixed`, and every buyer carries its own badge, so
"five wallets bought this" is never blurred into "five wallets were given
this". Turn the toggle off to require payment.

The remaining trade-off is that a genuine CEX withdrawal or OTC purchase looks
like an airdrop, since both are one-way. **Include airdrops** on the live board shows
everything that was filtered, with a `Paid for` column — `0 of 20` and a single
sender is an airdrop wearing a signal's clothes. Those rows never fire a
signal.

Because the transfer feed carries no price, live buyers are labelled `live` and
live signals have no USD total.

Live and scheduled monitoring write to the same signals table and the same
Telegram alerts, so a watchlist can run either or both. Chains covered are
listed in the card; Solana is Dune-only here.

**Scheduling uses the saved key.** Once a key is saved in the UI, scheduled
runs are active — no environment configuration needed. The check interval is
chosen when the watchlist is created (2 h – 48 h) and can be changed any time
from its Edit dialog. Every monitor run is one Dune execution on your plan's
credits — a 2-hour interval spends 12× more than a daily one. With several
uvicorn workers a SQLite claim guarantees each due watchlist runs exactly
once.

**Telegram (optional).** Configure a bot token and chat id in the UI's
*Telegram alerts* card (or via `DICE_TELEGRAM_BOT_TOKEN` /
`DICE_TELEGRAM_CHAT_ID` env vars) and every *new* or *strengthened* signal
(buyer count grew) is pushed as a message; *Send test* verifies the wiring.
Dismissed signals stay quiet even if they re-trigger.

Watchlists, run history and signals persist in SQLite (`DICE_DB_PATH`,
default `backend/data/dice.db`; the systemd unit uses `/var/lib/dice/dice.db`).
The monitor query is generated ad hoc, so plans without query CRUD can use the
holder side via `DUNE_QUERY_ID` but not scheduled monitoring.

## Was the signal right?

Everything above decides *when* to fire. None of it says whether firing was
correct, which makes every threshold in DICE a guess — 10% of the pool, a
48-hour window, this set of cohorts rather than that one. The **Signal
scoreboard** closes that loop.

When a signal fires for the first time, DICE stamps the token's price,
liquidity and **pool age** at that instant. Then, as each horizon comes due, it
looks the price up again at **1 hour, 24 hours and 7 days**. The card reports
two numbers per horizon:

* **win rate** — the share of signals that were up at all;
* **median return** — deliberately the median, not the mean. One token that
  went 40x drags a mean into looking like a strategy while the median says most
  signals went nowhere.

Only the *first* fire is recorded. A signal that keeps gaining buyers is the
same opportunity, and re-stamping it would quietly re-baseline the entry to a
price the signal itself may have helped move.

An empty horizon says **why** it is empty. "in 22h · 2 signals too young" is
a different statement from "overdue · press Check prices now", and both are
different from a bare dash — which cannot distinguish "this has not elapsed
yet" from "this is broken". A reader who cannot tell assumes broken, and the
honest state of a new scoreboard is mostly empty for a day.

For the same reason a return that rounds to zero is shown as flat and grey
rather than as `-0.0%` in red: an unchanged price is not a losing trade.

Two failure modes are recorded honestly rather than hidden. A token whose pool
has vanished reads as **-100%**, because that is the truth: the position could
not be exited. A price lookup that *fails* leaves the horizon **pending**, so
an API outage is never recorded as a token going to zero.

### The setting that makes "early" mean something

`max_pool_age_hours` refuses to fire a signal on a token whose liquidity pool
is older than the limit. It is not a nicety — without it there is no threshold
that works at all.

Measured on a live pool of 6,231 watched wallets: the tokens attracting the
most simultaneous buyers were LINK, AAVE, SHIB and APE, with pools **45,000+
hours old**. Five years. They lead not because anything is happening but
because more wallets hold them. A genuine 35-hour-old launch sat at a similar
count. So raising the threshold silences everything, and lowering it fills
Telegram with blue chips — the count alone cannot express "early", and only
the age can.

Tokens whose age DexScreener does not report are never blocked: missing
metadata is not evidence of an old pool, and silently dropping signals on
absent data is the failure mode hardest to notice. Empty means no limit, so
nothing changes for an install that does not set it.

**Pool age** answers the question the whole system exists for — how early was
this, really? Ten wallets buying a two-hour-old pool and ten buying a
two-year-old one are different events, and only one of them is being early. The
age shows on both the accumulation board and the scoreboard, and
`Max pool age (h)` on the board hides anything older. Tokens whose age
DexScreener does not report survive the filter: missing metadata is not
evidence of an old pool.

Give it a couple of weeks of signals before drawing conclusions. Three
outcomes is an anecdote; thirty is a basis for moving the threshold.

## Solana

Alchemy Notify's Address Activity is an EVM product, so the live path used to
stop at the chain boundary. Solana goes through **Helius** instead: save a
Helius API key next to the Alchemy token, switch **Live** on for a Solana
watchlist, and press *Sync wallets*.

Only the decoding differs. The events Helius produces are deliberately the
same shape as the Alchemy ones, so the event store, the pooled threshold, the
liquidity gate, the contract screen and the outcome tracking all work on
Solana without knowing Solana exists — and a Solana signal means exactly what
an Ethereum one does.

The buy and sell rules translate directly: a mint arriving is a **buy** when
the wallet also sent SOL or another token out in the same transaction, and an
airdrop when it did not; a mint leaving is a **sale** when something other
than that mint came back. Helius decodes transfers for you, so unlike the EVM
path there is no inferring a swap from raw logs.

**Two caveats worth knowing.** Helius does not sign delivery bodies — it sends
back a fixed `Authorization` header agreed at creation time, which is weaker
than Alchemy's HMAC, so DICE generates a full-length secret and compares it in
constant time. And the client has **not been exercised against a live Helius
account**: the parser is covered by tests built from the documented shape, but
the first real delivery is what proves the HTTP side. `Sync wallets` surfaces
any mismatch as an explicit error rather than as silence.

Base, meanwhile, needs nothing new — it has been an Alchemy network all along.

## Can it be sold again?

Every other check asks whether a token is *interesting*. This one asks whether
it is *survivable*. The worst thing DICE can produce is not a missed signal —
it is a correct signal on a token whose contract refuses the sell. Ten wallets
really did buy it; the contract simply keeps the money.

Before a signal goes out, the contract is screened through GoPlus Security
(no key needed): honeypot behaviour, buy and sell tax, mint authority,
blacklists, transfer pauses, LP lock and holder concentration.

**Only unsellability blocks.** It is the one verdict that makes the trade
impossible rather than merely unwise. High tax, open mint authority and
unlocked liquidity ride along as warnings — on the board, in the signal, and
in the Telegram message — and the decision stays yours. A gate that silently
swallowed signals on heuristics would be indistinguishable from a broken
webhook. A blocked token still *appears* on the board, marked `honeypot`, for
the same reason: you have a real interest in knowing your wallets walked into
one.

**An outage never blocks.** If the risk API does not answer, tokens pass
marked unchecked, exactly as an unknown price leaves an outcome pending
instead of recording a zero. Failing closed would take the whole system down
with the API.

This is deliberately not an LLM reading contract source. A model that is
mostly right about a honeypot is worse than useless: the failure is silent and
costs the whole position. `is_honeypot: 1` from a dedicated service is a fact;
a model's opinion about Solidity is not.

> The endpoint accepts a comma-separated address list and appears to batch, but
> answers for the **first address only** — verified by asking for three and
> receiving one. DICE therefore sends one request per token and caches the
> verdicts for six hours (failures for ten minutes). Sending a batch would have
> left the rest silently unscreened, which for a safety check is the worst
> possible failure: it looks like a pass.

## Watching the money leave

Everything above watches money going *in*. **Live distribution** is the same
evidence read the other way, and for a token you are already holding on a
signal it is the more urgent half.

A **sale** is a token leaving a watched wallet *while something else comes back
in the same transaction* — the mirror of the buy test. A one-way departure is
somebody funding another wallet or paying an invoice, and counting it as an
exit would misread the position entirely. A transfer between two wallets in
the watched set is neither: one position moving is not a buyer and not a
seller.

The accumulation board gained a **Sold** column from the same data: a token six
wallets bought and three have since sold is being distributed, not
accumulated, and the buy count alone cannot tell those apart.

## Where a model earns its keep — and where it does not

### Which key

Either an **OpenRouter** key (`sk-or-…`) or an **Anthropic** one (`sk-ant-…`)
— paste whichever you have and the prefix decides; there is no provider to
pick. OpenRouter is a gateway that reaches the same Claude models at the same
list price (`anthropic/claude-opus-5`, $5/$25 per million), and it works from
places the Anthropic API does not, which is the reason it is the default.

Both go through one internal call, so the prompts, the parsing, and every
guarantee below are identical. The two differ only in how a request is shaped
on the wire — which is exactly as much as should differ. **Test** proves the
saved key works now rather than when a signal fires.

### Which model

The picker is filled from the provider's live catalogue — type to search 338
models on OpenRouter, or paste any slug. The price per million tokens and the
context window are shown for whatever is selected, so the choice is made with
the cost visible rather than looked up afterwards. Empty means the default,
Claude Opus 5.

**Briefs and the review take separate models**, because the jobs are not
alike and the economics agree with the quality. A brief is search-and-
summarise, runs on every signal, and a small fast model does it well for a
fraction of the cost. The review runs when a human asks and has to decide what
a handful of numbers does *not* yet support.

That split is not a guess. Run on identical data — two signals, neither
matured — the two answers were:

| | cheap model | strong model |
|---|---|---|
| confidence | `high` | `none` |
| verdict | "no changes warranted" | explained that every win rate in the scoreboard was null |
| recommendations | 0 | 1 — noticed a router contract had contaminated *both* signals, and labelled the claim as not outcome-based |

So the review model defaults to the strong one even when briefs are set to a
cheap one. Both are configurable.

Models that cannot honour a fixed response schema are marked. That is not
fatal — briefs are unaffected, and the review falls back to reading JSON out
of the reply.

One cost worth knowing: the briefs use web search, which OpenRouter bills per
result — five per signal.

**Not detection.** Which wallets bought what, how many, inside which window,
and whether that crosses a threshold is set arithmetic and SQL. Handing it to
a model would make it slower, more expensive, occasionally wrong, and — worst
— **untestable**: the tests that currently pin signal behaviour would all
become approximately true. Contract safety is likewise a dedicated service's
job, not a model's opinion about bytecode.

Two things arithmetic genuinely cannot do:

**Triage.** A signal says "GEM, 19 of 180 wallets". Finding out what GEM
actually *is* costs five minutes, which on a six-hour-old pool is most of the
edge. The research answers that in four lines — theme, what it is, what the
combination suggests, and the biggest risk.

It arrives as a **second Telegram message**, deliberately. Measured against a
real model on a real token, one brief takes **55 seconds** — it runs a web
search and reads pages. The alert fires inside the Alchemy webhook, which is
retried if it answers slowly, so holding the alert for the research would mean
arriving a minute late to a pool that is hours old *and* risking a duplicate
delivery. The alert is the time-critical half; the research is not. A
background pass on the sweep picks up any signal without a brief, researches
it, and sends the finding after. If search finds nothing about a
token days after launch, it says so: that absence is itself the finding, and
inventing a plausible project is the one failure that would make this worse
than useless.

**Review.** Once the scoreboard has outcomes and the wallets have scores,
someone has to read them and say "under six hours won, over 48 lost, move the
filter". *Review my results* does exactly that, grounded in the numbers and
told to answer "not enough data yet" rather than manufacture confidence from
four data points. On demand rather than scheduled: it costs money per run and
has nothing new to say until more signals are scored.

The **theme** from each brief accumulates into a breakdown — which kinds of
token your cohorts are early to, and which they arrive late to. That is the
question a single brief cannot answer.

**The model never decides to buy, never sets a threshold, and never replaces
the liquidity or contract gates.** Every call is best-effort and time-boxed:
enrichment sits in the webhook request path, so if the API is slow, absent, or
declines, the signal fires on time *without* a brief rather than late with
one. Only the first fire of a signal is briefed — a signal that gains buyers
is the same opportunity, already answered.

### Addresses that are never a trader

Routers, settlement contracts and exchange hot wallets are excluded twice:
when wallets enter a cohort, and again when wallets are counted towards a
signal. The second is what repairs cohorts built before the filter existed,
without rewriting stored data.

Neither of the existing defences catches them. The holder query's
contract filter only applies to cohorts built with it switched on; the
`sprayer` flag needs weeks of history before it notices. And an exchange hot
wallet is not a contract and buys nothing — it *receives* — so it lands in an
early-buyer cohort simply because tokens flow through it.

The cost of missing one is not cosmetic, because everything downstream is a
count. On this install a single router had bought 744 distinct tokens, and a
review of the first two signals found a router had contributed to **both**.
One such address inflates the pool, raises the threshold every real wallet
must clear, and creates overlap between cohorts that merely appear to share a
member.

The list is deliberately conservative: a wrong entry silently removes a real
trader, which is much harder to notice than a router that slipped through.

## Which wallets are worth counting

The pooled signal counts heads: ten wallets bought, therefore fire. That
treats a fund that was four hours early to three tokens that tripled exactly
like a bot that buys four hundred things a month and is right by accident.
The **Wallet quality** card is the correction. Three measurements, all from
data DICE already holds:

* **Hit rate** — of the tokens the wallet paid for, the share that became
  signals. This is the noise filter: a sprayer's hit rate collapses towards
  zero as its buying volume rises.
* **Median return** — what the signals it bought into did 24 hours later. The
  direct question of whether this wallet's buying predicts price.
* **Lead time** — how many hours before the signal fired it bought. A wallet
  consistently hours ahead is a leading indicator; one that buys twelve
  minutes before the threshold is part of the crowd.

The **score** is the median return discounted by how thin the evidence is:
with one signal most of it is discounted, by five it counts almost in full.
Without that, one lucky 4x would top the table forever — the difference
between ranking skill and ranking luck. A wallet with nothing measured scores
*nothing*, not zero, because zero would rank a brand-new wallet above one with
a real loss.

Two flags, deliberately advisory rather than automatic removal (dropping
wallets silently would make the pool size lie):

| Flag | Meaning |
| --- | --- |
| **sprayer** | 25+ tokens bought, under 5% of them signalled. Counting it towards a threshold is close to counting noise. |
| **follower** | Three or more signals, median lead under half an hour. The return may look fine — it bought the same token — but it never gives you time to act. |

Lead time is deliberately *not* folded into the score. A threshold signal
guarantees somebody crosses it last, so one low lead means nothing; only a
habit across several signals is a verdict, which is exactly what the flag
requires.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/config` | UI bootstrap — key presence, execution mode, row caps. |
| `POST /api/key` · `DELETE /api/key` | Validate and save the key on the server / remove it. |
| `POST /api/key/validate` | Check a key without saving. Header: `X-Dune-Api-Key`. |
| `GET` / `PUT /api/settings/notifications` · `POST …/test` | Telegram bot token + chat id, and a test send. |
| `GET` / `PUT /api/settings/realtime` · `POST …/sync` | Alchemy auth token + public URL; reconcile watched wallets. |
| `POST …/check-url` · `POST …/simulate` · `GET …/deliveries` | Reachability probe, synthetic signal, delivery log. |
| `GET /api/live/tokens` · `POST /api/live/sweep` | Accumulation board (below-threshold included) and an on-demand re-check. `max_pool_age_hours` hides pools older than N hours. |
| `GET /api/coverage` | How far back Dune's balance table reaches, for the table and for one token. |
| `GET` / `PUT /api/settings/arkham` · `GET /api/arkham/address` | Arkham key, and an address lookup (`raw=true` for the untouched response). |
| `POST /api/webhooks/alchemy` | Alchemy delivery endpoint (signature-verified). |
| `POST /api/dune/archive-queries` | Archive every `DICE …` query in the Dune account (frees private-query slots). |
| `POST /api/sql` | Preview the generated DuneSQL. Needs a key, since the table is resolved from the catalogue. |
| `GET /api/source?chain=ethereum` | Which table DICE resolved, its shape and column mapping. Add `refresh=true` to re-resolve. |
| `GET /api/discover` | Raw listing of curated balance tables the key can reach. |
| `POST /api/holders` | Run the query; returns a preview + `job_id`. |
| `GET /api/export/{job_id}?format=csv\|xlsx\|json\|html` | Download the full result. |
| `GET /api/watchlists` · `POST /api/watchlists` | List / create watchlists (create takes explicit wallet lists). |
| `POST /api/watchlists/from-job/{job_id}` | Turn a finished holders job into a watchlist (`top_n` optional). |
| `GET` / `PATCH` / `DELETE /api/watchlists/{id}` | Inspect, tune (thresholds, wallets, ignores) or drop a watchlist. |
| `GET /api/watchlists/{id}/wallets` | The full wallet list. |
| `POST /api/watchlists/{id}/monitor` | Run the monitor now. Header: `X-Dune-Api-Key`. |
| `GET /api/watchlists/{id}/runs` | Monitor run history. |
| `GET /api/signals` | Active signals (`?include_dismissed=true` for all). |
| `POST /api/webhooks/helius` | Helius delivery endpoint for Solana (Authorization-header verified). |
| `GET /api/live/exits` | What the watched wallets are selling. |
| `GET /api/tokens/risk?chain=&address=` | Screen one contract (honeypot, taxes, owner powers, LP lock). |
| `GET /api/ai/models` | The active provider's catalogue: ids, prices, context, schema support. |
| `GET` / `PUT /api/settings/ai` · `POST …/test` | Model key (server-side), enrichment toggle, theme breakdown, and a live key check. |
| `POST` / `GET /api/ai/review` | Run a review of the measured results / read the last one. |
| `GET /api/signals/{id}/brief` | The researched brief for one signal. |
| `GET /api/wallets/leaderboard?chain=` | Wallet quality: score, hit rate, median return, lead time, cohort count. |
| `GET /api/signals/performance` · `POST …/refresh` | Scoreboard: win rate and median return per horizon; the POST looks up any prices now due. |
| `POST /api/signals/{id}/dismiss` · `/restore` | Mute / unmute a signal. |
| `GET /api/health` | Liveness. |

Request body:

```json
{
  "chain": "ethereum",
  "token_address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
  "start_date": "2026-07-20",
  "end_date": "2026-07-31",
  "min_balance": 0,
  "holder_mode": "daily",
  "wallet_filter": "all",
  "include_contracts": true,
  "exclude_burn_addresses": true
}
```

Results are cached for an hour keyed by `job_id`, so downloading four formats
runs the Dune query once, not four times. The cache is on disk (`DICE_JOB_DIR`,
`/var/lib/dice` under systemd) rather than in memory, because uvicorn runs
several workers and the worker serving the export is rarely the one that ran
the query.

## Execution modes

**Ad hoc (default).** DICE generates DuneSQL and executes it through a small,
fixed set of *private* queries it keeps in your Dune account — one per task
(holders, diagnostics, one per monitored watchlist), updated in place before
every run. Dune caps how many private queries an account may own, so DICE
never creates one per execution; if you hit "Max number of private queries
reached", archive old per-run `DICE …` queries at dune.com once and re-run.
Needs a Dune plan that includes query CRUD.

**Saved query.** Set `DUNE_QUERY_ID` to a parametrised query you saved in Dune,
declaring `blockchain`, `token_address`, `start_date`, `end_date` and
`minimum_balance`. DICE then only passes parameter values — this works on any
paid plan. Use `POST /api/sql` to get SQL to paste into that saved query.

## Chains and source tables

Twenty EVM chains plus Solana are selectable. Chain values are Dune's own
schema names (`balances_<value>`), so `avalanche_c` rather than `avalanche`. A
chain your plan cannot reach reports a clear "no readable balance table"
instead of failing obscurely.

DICE does **not** hard-code a Dune table name, because there isn't a stable one.
Reading a live Dune account's catalogue shows all of this at once:

- the plain `balances_ethereum` schema holds only internal tables —
  `stg_daily_updates`, `raw_updates`, `triggers` — and no `daily_updates`;
- the usable table sits in a rotating build schema,
  `balances_ethereum__spellbook_sqlmesh_490.daily_updates`, whose number
  changes when Dune rebuilds;
- other chains expose an older dense shape instead, `balances_polygon.erc20_day`;
- and what any given API key can see depends on its plan.

So DICE asks `information_schema` what exists, picks the best candidate, and
adapts its SQL to that table's columns. Preference order is by table
semantics first (`daily_updates` over `erc20_day` over `stg_daily_updates`),
then plain schemas over build schemas, then the highest build number.

Two shapes are handled:

| Shape | Table looks like | What DICE does |
| --- | --- | --- |
| `interval` | sparse rows with `[valid_from, valid_to)` | cross joins a generated calendar to expand each interval into one row per day |
| `daily` | already one row per address, token and day | sums per address and day |

The interval expansion is the whole point. A wallet that bought before the
window and held through it is a *single* row spanning the range — collapse that
and you lose precisely the steady holders this tool exists to find.

Because build schemas rotate, a resolved source can go stale mid-process. On a
"table does not exist" failure DICE drops the cached resolution, resolves
again and retries once, rather than surfacing the error.

Click **Check data source** in the UI (or `GET /api/source?chain=ethereum`) to
see which table was chosen and how its columns were mapped. If nothing is
readable at all, that is reported as a plan problem rather than a code one —
the curated balance tables are not on every Dune tier.

## Known limits

- Dates are **UTC** end-of-day throughout.
- Balances are *direct wallet balances*. Tokens sitting in staking contracts,
  LP positions, bridges or other DeFi contracts are held by those contracts, not
  by the underlying user — DICE reports the contract. Untangling that needs
  protocol-specific queries.
- Rebasing, wrapped and proxy tokens with unusual accounting may need a bespoke
  query; the generic snapshot query assumes standard balance semantics.
- `include_contracts=false` resolves a contract-identifying table from the
  catalogue, preferring `<chain>.creation_traces` — every deployed contract
  appears there, whereas a decoded-contract mapping only covers projects
  someone has decoded. If no such table is readable the request fails rather
  than quietly returning the contracts you asked to exclude.
- A single job is capped at 366 days and `DICE_MAX_ROWS` rows.
- `min_balance` is strict: `100` means *more than* 100, and the default `0`
  means any positive balance. The SQL filter and the row filter agree on this.
- A start date in the future is rejected; an end date in the future is clamped
  to today.
- Resolved source tables are cached on disk for 6 hours (`DICE_CACHE_DIR`,
  falling back under `DICE_JOB_DIR`) because resolving one costs a Dune
  execution. **Check data source** re-resolves immediately.

## Tests

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The suite covers request validation, SQL generation for EVM and Solana, row
parsing against Dune's date/number formats, holder-mode selection, every export
format, and the full API with a stubbed Dune client — no network, no credits.
