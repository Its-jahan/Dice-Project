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

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/config` | UI bootstrap — key presence, execution mode, row caps. |
| `POST /api/key` · `DELETE /api/key` | Validate and save the key on the server / remove it. |
| `POST /api/key/validate` | Check a key without saving. Header: `X-Dune-Api-Key`. |
| `GET` / `PUT /api/settings/notifications` · `POST …/test` | Telegram bot token + chat id, and a test send. |
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
