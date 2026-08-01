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

## Quick start

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000>, paste your Dune API key into the **Dune API key**
field, hit **Test key**, then run a query.

## Entering your Dune API key in the website

There is no key in any config file by default. The UI has a key panel with:

| Control | What it does |
| --- | --- |
| **API key** field | Masked input with a Show/Hide toggle. |
| **Remember** | `On this browser` (localStorage), `Until tab closes` (sessionStorage), or `Do not save` (kept in memory for the page only). |
| **Save key** | Stores it per the Remember choice, clearing the other store so no stale copy survives a switch. |
| **Test key** | Calls `POST /api/key/validate`, which asks Dune whether the key authenticates — before you spend any credits. |
| **Clear** | Wipes the key from both browser stores. |

How the key is handled:

- It travels in the `X-Dune-Api-Key` **header**, never in a URL — so it cannot
  leak through server access logs, browser history, or the `Referer` header.
- The server holds it only for the lifetime of the request. It is never written
  to disk, never logged, and never included in any response body
  (`GET /api/config` reports only *whether* a server-side key exists).
- `DUNE_API_KEY` in the environment is an optional fallback for a private
  single-user deployment; leave it unset for a shared one so each user brings
  their own key. A header key always wins over the environment key.

If you deploy DICE publicly, serve it over HTTPS — the key is only as protected
as the transport carrying it.

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

CSV carries the leading table for the chosen mode; XLSX carries **both** plus a
`Request` sheet recording exactly what was asked for; JSON carries everything;
HTML is a standalone, filterable page with no external assets.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/config` | UI bootstrap — key presence, execution mode, row caps. |
| `POST /api/key/validate` | Check a Dune key. Header: `X-Dune-Api-Key`. |
| `POST /api/sql` | Preview the generated DuneSQL. Needs a key, since the table is resolved from the catalogue. |
| `GET /api/source?chain=ethereum` | Which table DICE resolved, its shape and column mapping. Add `refresh=true` to re-resolve. |
| `GET /api/discover` | Raw listing of curated balance tables the key can reach. |
| `POST /api/holders` | Run the query; returns a preview + `job_id`. |
| `GET /api/export/{job_id}?format=csv\|xlsx\|json\|html` | Download the full result. |
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

Results are cached in memory for an hour keyed by `job_id`, so downloading four
formats runs the Dune query once, not four times.

## Execution modes

**Ad hoc (default).** DICE generates DuneSQL, creates a *private* query in your
Dune account and executes it. Needs a Dune plan that includes query CRUD.

**Saved query.** Set `DUNE_QUERY_ID` to a parametrised query you saved in Dune,
declaring `blockchain`, `token_address`, `start_date`, `end_date` and
`minimum_balance`. DICE then only passes parameter values — this works on any
paid plan. Use `POST /api/sql` to get SQL to paste into that saved query.

## Chains and source tables

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
- `include_contracts=false` filters via `contracts.contract_mapping`, so it only
  removes contracts Dune has decoded.
- A single job is capped at 366 days and `DICE_MAX_ROWS` rows.

## Tests

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The suite covers request validation, SQL generation for EVM and Solana, row
parsing against Dune's date/number formats, holder-mode selection, every export
format, and the full API with a stubbed Dune client — no network, no credits.
