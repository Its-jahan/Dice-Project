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
bash deploy/install.sh your-domain.example
```

Sets up a `dice` service user, a hardened systemd unit running uvicorn on
`127.0.0.1:8000`, and an nginx reverse proxy. Re-run it to deploy updates.
See [deploy/README.md](deploy/README.md) — in particular the TLS step, which
matters here because the Dune API key travels in a request header.

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
| `POST /api/sql` | Preview the generated DuneSQL. No key, no credits. |
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

## Chains

EVM: Ethereum, Base, Arbitrum, Optimism, Polygon — via `tokens.balances_daily`.

Solana: via `tokens_solana.balances_daily`, aggregated **per owner**. A Solana
wallet can control several token accounts for one mint, so DICE sums them;
otherwise one holder appears as several fragmented rows.

Dune's curated balance tables are in Open Beta. If a table or column changes
upstream, `backend/app/sql.py` is the only file that needs editing.

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
