"""DICE backend: historical token holders from Dune, exported as files.

API key policy
--------------
Single-user deployment: the key saved through the UI (``POST /api/key``) is
validated against Dune and stored in the server's SQLite settings, and it
powers both interactive queries and the scheduled watchlist monitoring. An
``X-Dune-Api-Key`` header still overrides it per request, and ``DUNE_API_KEY``
in the environment is the fallback when nothing has been saved. The key is
never logged and never returned in full by any endpoint — only a last-four
hint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from html import escape as html_escape
from urllib.parse import quote
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, AsyncIterator

import httpx
from fastapi import Body, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from . import (
    ai,
    alchemy,
    arkham,
    auth,
    cohorts,
    db,
    helius,
    monitor,
    performance,
    realtime,
    security,
    wallets,
)
from .cache import cache
from .config import settings
from .dune import DuneClient, DuneError, ensure_query
from .exporters import DATASETS, MEDIA_TYPES, export, filename_for
from .holders import (
    apply_holder_mode,
    apply_wallet_filter,
    build_summary,
    classify_wallets,
    parse_rows,
)
from .jobs import store
from .models import (
    Chain,
    ExportFormat,
    HistorySource,
    HoldersRequest,
    HoldersResponse,
    MonitorResult,
    MonitorRunOut,
    SignalOut,
    WatchlistCreate,
    WatchlistFromJob,
    WalletType,
    WatchlistOut,
    WatchlistUpdate,
    normalize_addresses,
)
from .source import (
    ContractSource,
    Source,
    SourceNotFound,
    resolve_contract_source,
    resolve_source,
)
from .sql import (
    DISCOVERY_LIMIT,
    build_catalog_sql,
    build_coverage_sql,
    build_transfer_snapshot_sql,
    build_contracts_catalog_sql,
    build_discovery_sql,
    build_query_parameters,
    build_snapshot_sql,
)

log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
#: How many snapshot rows the JSON preview returns. Full data goes in exports.
PREVIEW_ROWS = 500

#: Deliberately unmistakable token for simulated signals, so a test result can
#: never be confused with a real one.
SIMULATED_TOKEN = "0xd1ce" + "0" * 36
SIMULATED_SYMBOL = "DICETEST"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run the background loops for the lifetime of the process."""
    tasks: list[asyncio.Task[None]] = []
    if settings.monitor_enabled:
        tasks.append(asyncio.create_task(monitor.scheduler_loop()))
    # The live sweep re-checks stored events so a signal is not missed just
    # because no new delivery happened to touch that token.
    tasks.append(asyncio.create_task(realtime.sweep_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="DICE",
    description="Historical token holder extraction via Dune Analytics.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def _no_store(request: Request, call_next):
    """Never let a browser cache this app.

    index.html and app.js are served from stable URLs with no version hash, so
    a cached copy of one and a fresh copy of the other produces a page whose
    buttons exist but do nothing. The assets are a few KB; correctness beats
    the saved round trip.
    """
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.exception_handler(DuneError)
async def _dune_error_handler(_request: Request, exc: DuneError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


def resolve_api_key(header_key: str | None) -> str:
    """Header key wins; otherwise the server-saved key (UI or environment)."""
    key = (header_key or "").strip() or db.server_api_key() or ""
    if not key:
        raise HTTPException(
            status_code=401,
            detail="No Dune API key saved yet. Enter your key in the UI and "
            "press Save — it is stored on this server so scheduled "
            "monitoring can use it too.",
        )
    return key


ApiKeyHeader = Annotated[str | None, Header(alias="X-Dune-Api-Key")]


def _key_hint(key: str | None) -> str | None:
    return f"…{key[-4:]}" if key and len(key) >= 8 else None


@app.get("/api/config")
async def get_config() -> dict[str, object]:
    """What the UI needs to render itself — never includes the key itself."""
    server_key = db.server_api_key()
    telegram_token, telegram_chat = db.telegram_credentials()
    return {
        "server_key_configured": bool(server_key),
        "server_key_hint": _key_hint(server_key),
        "saved_query_id": settings.dune_query_id,
        "execution_mode": "saved_query" if settings.dune_query_id else "ad_hoc",
        "max_rows": settings.max_rows,
        "preview_rows": PREVIEW_ROWS,
        "monitor": {
            "enabled": settings.monitor_enabled,
            "max_wallets": settings.monitor_max_wallets,
            # Scheduled (unattended) runs execute server-side, so they only
            # happen once a key has been saved on the server.
            "auto_possible": settings.monitor_enabled and bool(server_key),
            "telegram_configured": bool(telegram_token and telegram_chat),
        },
        "realtime": {
            "configured": bool(db.get_setting("alchemy_auth_token")),
            "public_url_set": bool(_public_base_url()),
            "supported_chains": live_chains(),
            "helius_configured": bool(db.get_setting("helius_api_key")),
        },
    }


@app.post("/api/key/validate")
async def validate_key(x_dune_api_key: ApiKeyHeader = None) -> dict[str, bool]:
    """Check a key before the user spends credits on a real run."""
    key = resolve_api_key(x_dune_api_key)
    async with DuneClient(key) as client:
        return {"valid": await client.validate_key()}


@app.post("/api/key")
async def save_key(body: Annotated[dict, Body()]) -> dict[str, object]:
    """Validate a Dune key and store it on the server, replacing any old one.

    Single-user deployment model: the saved key is what runs both the queries
    started from the UI and the scheduled watchlist monitoring, so it lives on
    the server (SQLite) rather than in a browser.
    """
    key = str(body.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="Provide a key to save.")
    async with DuneClient(key) as client:
        if not await client.validate_key():
            raise HTTPException(
                status_code=401,
                detail="Dune rejected this key — nothing was saved.",
            )
    db.set_setting("dune_api_key", key)
    return {"saved": True, "hint": _key_hint(key)}


@app.delete("/api/key")
async def clear_key() -> dict[str, bool]:
    db.delete_setting("dune_api_key")
    return {"saved": False}


# ------------------------------------------------------------- notifications


@app.get("/api/settings/notifications")
async def get_notification_settings() -> dict[str, object]:
    token, chat_id = db.telegram_credentials()
    return {
        "telegram_configured": bool(token and chat_id),
        "bot_token_hint": _key_hint(token),
        "chat_id": chat_id,
    }


@app.put("/api/settings/notifications")
async def save_notification_settings(
    body: Annotated[dict, Body()],
) -> dict[str, object]:
    """Save Telegram credentials. Empty strings clear a value."""
    if "bot_token" in body:
        token = str(body.get("bot_token") or "").strip()
        if token:
            db.set_setting("telegram_bot_token", token)
        else:
            db.delete_setting("telegram_bot_token")
    if "chat_id" in body:
        # Accept t.me links and other pasted forms, not just the raw id.
        chat_id = monitor.normalize_chat_id(str(body.get("chat_id") or ""))
        if chat_id:
            db.set_setting("telegram_chat_id", chat_id)
        else:
            db.delete_setting("telegram_chat_id")
    return await get_notification_settings()


@app.post("/api/settings/notifications/test")
async def test_notification() -> dict[str, object]:
    """Send a test message, naming the bot so channel setup is unambiguous."""
    bot = await monitor.describe_telegram_bot()
    error = await monitor.send_telegram_message(
        "DICE test message — signal alerts will arrive in this chat."
    )
    if error:
        hint = (
            f" The bot is @{bot}; a channel must have it added as an "
            "administrator with permission to post."
            if bot
            else ""
        )
        raise HTTPException(status_code=502, detail=error + hint)
    # The chat id may have been corrected during the send; report what stuck.
    _, chat_id = db.telegram_credentials()
    return {"sent": True, "bot_username": bot, "chat_id": chat_id}


# ---------------------------------------------------------- dune maintenance


@app.post("/api/dune/archive-queries")
async def archive_dice_queries(x_dune_api_key: ApiKeyHeader = None) -> dict[str, object]:
    """Archive every query named ``DICE …`` in the Dune account.

    Cleans up the per-run queries older DICE versions accumulated (the cause
    of "Max number of private queries reached"). Only queries whose name
    starts with "DICE" are touched; the reusable slots are forgotten so the
    next run recreates them fresh.
    """
    key = resolve_api_key(x_dune_api_key)
    archived = 0
    errors: list[str] = []
    async with DuneClient(key) as client:
        page_size = 100
        offset = 0
        targets: list[int] = []
        while True:
            queries, total = await client.list_queries(limit=page_size, offset=offset)
            for query in queries:
                query_id = query.get("id") or query.get("query_id")
                name = str(query.get("name") or "")
                if isinstance(query_id, int) and name.startswith("DICE"):
                    targets.append(query_id)
            offset += page_size
            if not queries or offset >= total:
                break

        for query_id in targets:
            try:
                await client.archive_query(query_id)
                archived += 1
            except DuneError as exc:
                # Keep going — one query the account cannot touch should not
                # abandon the other 25 — but never swallow *why* it failed.
                errors.append(f"{query_id}: {exc}")

        # Only forget the reuse slots we actually archived; otherwise the next
        # run would create new queries while the old ones still hold the cap.
        slots_cleared = (
            db.drop_account_slots(client.key_fingerprint) if archived else 0
        )

    return {
        "found": len(targets),
        "archived": archived,
        "failed": len(errors),
        "slots_cleared": slots_cleared,
        # First few reasons; the UI shows them so a total failure is diagnosable.
        "errors": errors[:3],
    }


@app.post("/api/sql")
async def preview_sql(
    req: Annotated[HoldersRequest, Body()],
    x_dune_api_key: ApiKeyHeader = None,
) -> dict[str, object]:
    """Show the DuneSQL DICE would run.

    Needs a key now: the table and its column names are discovered from the
    catalogue rather than assumed, so the preview would otherwise be a guess.
    Resolution is cached, so repeat previews cost nothing.
    """
    key = resolve_api_key(x_dune_api_key)
    async with DuneClient(key) as client:
        try:
            source, _ = await resolve_for(client, req.chain)
        except SourceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        contracts = await contracts_if_needed(client, req)
    return {
        "sql": build_snapshot_sql(req, source, contracts),
        "source": source.qualified,
        "shape": source.shape,
        "parameters": build_query_parameters(req),
        "execution_mode": "saved_query" if settings.dune_query_id else "ad_hoc",
    }


async def _run_sql(
    client: DuneClient,
    *,
    name: str,
    sql: str,
    max_rows: int,
    purpose: str = "diagnostic",
):
    """Execute and drain a one-off query, reusing the account's slot for it."""
    query_id = await ensure_query(client, purpose=purpose, name=name, query_sql=sql)
    execution_id = await client.execute_query(query_id)
    await client.wait_for_execution(execution_id)
    rows, _ = await client.fetch_results(execution_id, max_rows=max_rows)
    return rows


@app.get("/api/discover")
async def discover_tables(
    pattern: str | None = None,
    x_dune_api_key: ApiKeyHeader = None,
) -> dict[str, object]:
    """List the balance tables this Dune key can actually reach.

    ``information_schema`` only reports what the caller is entitled to, so this
    distinguishes the two causes of "does not exist or it is private": a table
    that was renamed upstream, versus one gated behind a Dune plan tier.
    """
    key = resolve_api_key(x_dune_api_key)
    try:
        sql = build_discovery_sql(pattern)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async with DuneClient(key) as client:
        rows = await _run_sql(
            client,
            name=f"DICE discover {pattern or 'curated'}",
            sql=sql,
            max_rows=DISCOVERY_LIMIT,
        )

    tables = sorted(
        f"{row.get('table_schema')}.{row.get('table_name')}" for row in rows
    )
    return {
        "pattern": pattern,
        "count": len(tables),
        # The SQL caps at 500 rows. Say so rather than let an alphabetically
        # truncated list read as "these are all the tables that exist".
        "truncated": len(tables) >= DISCOVERY_LIMIT,
        "tables": tables,
    }


#: Column types of each resolved table, carried in the cache entry so the
#: diagnostics view can show whether valid_from is a date or a timestamp.
def _source_key(chain: Chain) -> str:
    return f"source.{chain.value}"


def _contract_key(chain: Chain) -> str:
    return f"contracts.{chain.value}"


async def _read_catalog(
    client: DuneClient, *, name: str, sql: str
) -> tuple[dict[str, set[str]], dict[str, dict[str, str]]]:
    """Run a catalogue query, returning ``({table: columns}, {table: types})``."""
    rows = await _run_sql(client, name=name, sql=sql, max_rows=2000)
    catalog: dict[str, set[str]] = {}
    types: dict[str, dict[str, str]] = {}
    for row in rows:
        key = f"{row.get('table_schema')}.{row.get('table_name')}"
        column = str(row.get("column_name"))
        catalog.setdefault(key, set()).add(column)
        if row.get("data_type") is not None:
            types.setdefault(key, {})[column] = str(row["data_type"])
    return catalog, types


async def resolve_for(client: DuneClient, chain: Chain) -> tuple[Source, dict]:
    """Find the balance table for a chain, caching across workers and restarts."""
    cached = cache.get(_source_key(chain))
    if cached:
        try:
            return Source(**cached["source"]), cached.get("column_types", {})
        except TypeError:
            cache.drop(_source_key(chain))  # cache written by an older version

    catalog, types = await _read_catalog(
        client, name=f"DICE catalogue {chain.value}", sql=build_catalog_sql(chain)
    )
    source = resolve_source(catalog)
    column_types = types.get(source.qualified, {})
    cache.put(
        _source_key(chain),
        {"source": asdict(source), "column_types": column_types},
    )
    return source, column_types


async def resolve_contracts_for(client: DuneClient, chain: Chain) -> ContractSource:
    """Find a table identifying contract addresses, caching the answer."""
    cached = cache.get(_contract_key(chain))
    if cached:
        try:
            return ContractSource(**cached)
        except TypeError:
            cache.drop(_contract_key(chain))

    catalog, _ = await _read_catalog(
        client,
        name=f"DICE contract catalogue {chain.value}",
        sql=build_contracts_catalog_sql(chain),
    )
    contracts = resolve_contract_source(chain, catalog)
    cache.put(_contract_key(chain), asdict(contracts))
    return contracts


async def contracts_if_needed(
    client: DuneClient, req: HoldersRequest
) -> ContractSource | None:
    """Resolve the contract table only when the request actually filters on it."""
    if req.include_contracts:
        return None
    try:
        return await resolve_contracts_for(client, req.chain)
    except SourceNotFound as exc:
        # Failing loudly beats returning the contracts they asked to exclude.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/settings/arkham")
async def get_arkham_settings() -> dict[str, object]:
    key = arkham.api_key()
    return {"configured": bool(key), "key_hint": _key_hint(key)}


@app.put("/api/settings/arkham")
async def save_arkham_settings(body: Annotated[dict, Body()]) -> dict[str, object]:
    key = str(body.get("api_key") or "").strip()
    if key:
        db.set_setting("arkham_api_key", key)
    else:
        db.delete_setting("arkham_api_key")
    return await get_arkham_settings()


@app.get("/api/arkham/address")
async def arkham_address(
    address: str,
    chain: Chain = Chain.ethereum,
    raw: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    """Who Arkham says an address is.

    ``raw=true`` returns the untouched response, which is how the parsing was
    confirmed against a real key — Arkham's shapes are not published.
    """
    try:
        payload = await arkham.raw_lookup(chain, address.strip())
    except arkham.ArkhamError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if raw:
        return {"address": address, "chain": chain.value, "raw": payload}
    return {
        "address": address,
        "chain": chain.value,
        **arkham.describe_address(payload, chain),
    }


@app.get("/api/coverage")
async def token_coverage(
    chain: Chain = Chain.ethereum,
    token_address: str = "",
    x_dune_api_key: ApiKeyHeader = None,
) -> dict[str, object]:
    """What history does Dune actually hold for this table and this token?

    Answers the question an empty result raises but cannot settle: did the
    token have no holders in that range, or does the balance table simply not
    reach that far back?
    """
    key = resolve_api_key(x_dune_api_key)
    token = token_address.strip()
    if not token:
        raise HTTPException(status_code=422, detail="Provide a token_address.")
    try:
        token = normalize_addresses(chain, [token])[0]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async with DuneClient(key) as client:
        try:
            source, _ = await resolve_for(client, chain)
        except SourceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        rows = await _run_sql(
            client,
            name=f"DICE coverage {chain.value} {token[:10]}",
            sql=build_coverage_sql(chain, token, source),
            max_rows=10,
            purpose="coverage",
        )

    row = rows[0] if rows else {}
    return {
        "chain": chain.value,
        "token_address": token,
        "table": source.qualified,
        "shape": source.shape,
        "table_first_day": row.get("table_first_day"),
        "table_last_day": row.get("table_last_day"),
        "token_first_day": row.get("token_first_day"),
        "token_last_day": row.get("token_last_day"),
        "token_rows": row.get("token_rows"),
    }


@app.get("/api/source")
async def get_source(
    chain: Chain = Chain.ethereum,
    refresh: bool = False,
    x_dune_api_key: ApiKeyHeader = None,
) -> dict[str, object]:
    """Report which table DICE resolved for a chain, and its column mapping."""
    key = resolve_api_key(x_dune_api_key)
    if refresh:
        cache.drop(_source_key(chain))
        cache.drop(_contract_key(chain))
    async with DuneClient(key) as client:
        try:
            source, column_types = await resolve_for(client, chain)
        except SourceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "chain": chain.value,
        "table": source.qualified,
        "shape": source.shape,
        "columns": {
            "address": source.address,
            "token": source.token,
            "balance": source.balance,
            "day": source.day,
            "valid_from": source.valid_from,
            "valid_to": source.valid_to,
        },
        # Whether valid_from is a date or a timestamp decides how an
        # intra-day acquisition is attributed, so show it.
        "column_types": column_types,
    }


async def _run_holders_query(client: DuneClient, req: HoldersRequest) -> str:
    """Build and execute the snapshot query, re-resolving once if it goes stale.

    Dune publishes the balance models into rotating ``__spellbook_sqlmesh_NNN``
    build schemas, so a source cached earlier in the process can stop existing
    when Dune rebuilds. Rather than surface "table does not exist" to the user,
    drop the cached answer, resolve again and retry once.
    """
    for attempt in (1, 2):
        try:
            source, _ = await resolve_for(client, req.chain)
        except SourceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        contracts = await contracts_if_needed(client, req)
        query_id = await ensure_query(
            client,
            purpose="holders",
            name=f"DICE holders {req.chain.value} {req.token_address[:10]} "
            f"{req.start_date}..{req.end_date}",
            query_sql=build_snapshot_sql(req, source, contracts),
        )
        execution_id = await client.execute_query(query_id)
        try:
            await client.wait_for_execution(execution_id)
        except DuneError as exc:
            stale = "does not exist" in str(exc)
            if attempt == 1 and stale:
                # Both resolutions name tables that can rotate away.
                cache.drop(_source_key(req.chain))
                cache.drop(_contract_key(req.chain))
                log.warning("source for %s went stale, re-resolving", req.chain.value)
                continue
            raise
        return execution_id

    raise AssertionError("unreachable")  # pragma: no cover


async def _run_transfer_query(client: DuneClient, req: HoldersRequest) -> str:
    """Execute the transfer-reconstruction query and return its execution id."""
    query_id = await ensure_query(
        client,
        purpose="holders_transfers",
        name=f"DICE holders via transfers {req.chain.value} "
        f"{req.token_address[:10]} {req.start_date}..{req.end_date}",
        query_sql=build_transfer_snapshot_sql(req),
    )
    execution_id = await client.execute_query(query_id)
    await client.wait_for_execution(execution_id)
    return execution_id


@app.post("/api/holders")
async def get_holders(
    req: Annotated[HoldersRequest, Body()],
    x_dune_api_key: ApiKeyHeader = None,
) -> dict[str, object]:
    """Run the snapshot query and return a preview plus a job id for export."""
    key = resolve_api_key(x_dune_api_key)

    source_used = "balances"
    async with DuneClient(key) as client:
        if settings.dune_query_id:
            query_id = settings.dune_query_id
            parameters = build_query_parameters(req)
            execution_id = await client.execute_query(query_id, parameters=parameters)
            await client.wait_for_execution(execution_id)
            rows, truncated = await client.fetch_results(execution_id)
        elif req.history_source is HistorySource.transfers:
            execution_id = await _run_transfer_query(client, req)
            rows, truncated = await client.fetch_results(execution_id)
            source_used = "transfers"
        else:
            execution_id = await _run_holders_query(client, req)
            rows, truncated = await client.fetch_results(execution_id)
            if not rows and req.history_source is HistorySource.auto:
                # The balance table only reaches as far back as Dune
                # backfilled it. Rather than report "no holders" for a range
                # it simply does not cover, rebuild the balances from
                # transfers, which go back to genesis.
                log.info(
                    "balance table returned nothing for %s; retrying from transfers",
                    req.token_address,
                )
                execution_id = await _run_transfer_query(client, req)
                rows, truncated = await client.fetch_results(execution_id)
                source_used = "transfers"

    # classify_wallets also strips the baseline day it needed, so nothing
    # downstream sees a snapshot from before the requested range.
    parsed = parse_rows(rows, req)
    in_range, facts = classify_wallets(parsed, req)
    filtered = apply_wallet_filter(in_range, facts, req)
    snapshots = apply_holder_mode(filtered, req)
    summary = build_summary(snapshots, facts)

    # An empty result has several very different causes, and guessing at them
    # wastes the user's credits on re-runs. Count what survived each stage so
    # the UI can say which one it was.
    stages = {
        "source": source_used,
        "dune_rows": len(rows),
        "after_min_balance": len(parsed),
        "wallets_in_range": len({s.wallet_address for s in in_range}),
        "buyers": sum(
            1 for f in facts.values() if f["wallet_type"] is WalletType.buyer
        ),
        "holders": sum(
            1 for f in facts.values() if f["wallet_type"] is WalletType.holder
        ),
        "after_wallet_filter": len({s.wallet_address for s in filtered}),
        "after_holder_mode": len({s.wallet_address for s in snapshots}),
    }
    result = HoldersResponse(
        request=req,
        execution_id=execution_id,
        row_count=len(snapshots),
        wallet_count=len(summary),
        snapshots=snapshots,
        summary=summary,
        truncated=truncated,
    )

    job_id = store.put(result)
    return {
        "job_id": job_id,
        "execution_id": execution_id,
        "row_count": result.row_count,
        "wallet_count": result.wallet_count,
        "truncated": result.truncated,
        "effective_end_date": req.effective_end_date.isoformat(),
        "end_date_clamped": req.end_date_clamped,
        "stages": stages,
        "preview": [s.model_dump(mode="json") for s in snapshots[:PREVIEW_ROWS]],
        "summary_preview": [s.model_dump(mode="json") for s in summary[:PREVIEW_ROWS]],
    }


@app.get("/api/export/{job_id}")
async def download_export(
    job_id: str,
    fmt: Annotated[ExportFormat, Query(alias="format")] = ExportFormat.csv,
    dataset: str = "auto",
) -> Response:
    """Download a finished result.

    ``dataset`` picks which table leads the file — the UI passes the tab the
    user is looking at, so downloading from the summary tab yields the summary
    rather than the snapshots the holder mode would otherwise choose.
    """
    if dataset not in DATASETS:
        raise HTTPException(
            status_code=422, detail=f"dataset must be one of {', '.join(DATASETS)}"
        )
    result = store.get(job_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Result expired or unknown job id — run the query again.",
        )
    payload = export(result, fmt, dataset)
    return Response(
        content=payload,
        media_type=MEDIA_TYPES[fmt],
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename_for(result, fmt, dataset)}"'
            )
        },
    )


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ------------------------------------------------------ realtime (Alchemy)


def _public_base_url() -> str | None:
    return (db.get_setting("public_base_url") or settings.public_base_url or "").strip() or None


def live_chains() -> list[str]:
    """Chains DICE can monitor live, across both providers.

    Alchemy Notify covers the EVM chains; Solana is Helius. Kept in one place
    because two lists of "what works live" drift, and the symptom is a chain
    the UI offers and the sync then refuses.
    """
    return sorted(alchemy.supported_chains() + [Chain.solana.value])


def _webhook_url() -> str:
    base = _public_base_url()
    if not base:
        raise HTTPException(
            status_code=422,
            detail="Set the public HTTPS URL of this server first — Alchemy has "
            "to be able to reach it to deliver events.",
        )
    return base.rstrip("/") + "/api/webhooks/alchemy"


def _helius_webhook_url() -> str:
    base = _public_base_url()
    if not base:
        raise HTTPException(
            status_code=422,
            detail="Set the public HTTPS URL of this server first — Helius has "
            "to be able to reach it to deliver events.",
        )
    return base.rstrip("/") + "/api/webhooks/helius"


async def sync_solana() -> dict[str, object]:
    """Make Helius watch exactly the wallets Solana's live watchlists hold.

    Helius has no incremental address API, so the full set goes on every sync.
    That is the safer shape anyway: it cannot drift out of step with the
    watchlists the way an add/remove reconciliation can.
    """
    api_key = db.get_setting("helius_api_key")
    if not api_key:
        raise HTTPException(
            status_code=422,
            detail="Save your Helius API key first — Solana live monitoring "
            "does not go through Alchemy.",
        )

    chain = Chain.solana
    wanted = db.realtime_wallets(chain.value)
    existing = db.get_webhook(chain.value)

    try:
        async with helius.HeliusClient(api_key) as client:
            if not wanted:
                if existing:
                    await client.delete_webhook(existing["webhook_id"])
                    db.delete_webhook(chain.value)
                return {"chain": chain.value, "addresses": 0, "webhook_id": None}

            # The delivery secret is the only thing guarding the endpoint, so
            # it is generated once and then reused — rotating it on every sync
            # would leave in-flight deliveries failing for no reason.
            secret = (existing or {}).get("signing_key") or helius.new_auth_secret()
            url = _helius_webhook_url()
            if existing:
                await client.set_addresses(
                    existing["webhook_id"],
                    webhook_url=url,
                    addresses=wanted,
                    auth_secret=secret,
                )
                webhook_id = existing["webhook_id"]
            else:
                created = await client.create_webhook(
                    webhook_url=url, addresses=wanted, auth_secret=secret
                )
                webhook_id = str(created.get("webhookID") or created.get("id") or "")
                if not webhook_id:
                    raise HTTPException(
                        status_code=502,
                        detail="Helius created a webhook but returned no id.",
                    )
    except helius.HeliusError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    db.save_webhook(
        chain=chain.value,
        network="solana",
        webhook_id=webhook_id,
        signing_key=secret,
        webhook_url=url,
        address_count=len(wanted),
    )
    return {
        "chain": chain.value,
        "addresses": len(wanted),
        "webhook_id": webhook_id,
    }


async def sync_realtime_chain(chain: Chain) -> dict[str, object]:
    """Make Alchemy watch exactly the wallets this chain's live watchlists hold.

    Creates the network's webhook on first use, then reconciles the address
    list — additions and removals — against what Alchemy currently has.
    """
    token = db.get_setting("alchemy_auth_token")
    if not token:
        raise HTTPException(
            status_code=422, detail="Save your Alchemy auth token first."
        )
    network = alchemy.network_for(chain)
    if not network:
        raise HTTPException(
            status_code=422,
            detail=f"Alchemy webhooks are not wired up for {chain.value}. "
            f"Supported: {', '.join(alchemy.supported_chains())}.",
        )

    wanted = db.realtime_wallets(chain.value)
    existing = db.get_webhook(chain.value)

    try:
        async with alchemy.AlchemyNotifyClient(token) as client:
            if not wanted:
                # Nothing left to watch: drop the webhook rather than leave an
                # empty one consuming a free-tier slot.
                if existing:
                    await client.delete_webhook(existing["webhook_id"])
                    db.delete_webhook(chain.value)
                return {"chain": chain.value, "addresses": 0, "webhook_id": None}

            if not existing:
                created = await client.create_address_webhook(
                    network=network,
                    webhook_url=_webhook_url(),
                    addresses=sorted(wanted),
                )
                db.save_webhook(
                    chain=chain.value,
                    network=network,
                    webhook_id=str(created["id"]),
                    signing_key=str(created.get("signing_key") or ""),
                    webhook_url=_webhook_url(),
                    address_count=0,
                )
                existing = db.get_webhook(chain.value)

            webhook_id = existing["webhook_id"]
            try:
                registered = set(await client.list_addresses(webhook_id))
            except alchemy.AlchemyError as gone:
                # The webhook was deleted or recreated on Alchemy's side, so
                # our stored id points at nothing. Without this the sync fails
                # with "Webhook not found" on every retry and live monitoring
                # stays dead — silently, because the app still shows a webhook
                # and a wallet count. Re-create instead, and note that the
                # signing key changes with it.
                if gone.status_code != 404:
                    raise
                log.warning(
                    "webhook %s no longer exists at Alchemy; recreating",
                    webhook_id,
                )
                db.delete_webhook(chain.value)
                created = await client.create_address_webhook(
                    network=network,
                    webhook_url=_webhook_url(),
                    addresses=sorted(wanted),
                )
                db.save_webhook(
                    chain=chain.value,
                    network=network,
                    webhook_id=str(created["id"]),
                    signing_key=str(created.get("signing_key") or ""),
                    webhook_url=_webhook_url(),
                    address_count=len(wanted),
                )
                existing = db.get_webhook(chain.value)
                webhook_id = existing["webhook_id"]
                registered = set(await client.list_addresses(webhook_id))
            to_add = sorted(wanted - registered)
            to_remove = sorted(registered - wanted)
            if to_add or to_remove:
                await client.update_addresses(
                    webhook_id, add=to_add, remove=to_remove
                )
            db.mark_webhook_synced(chain.value, len(wanted))
            return {
                "chain": chain.value,
                "webhook_id": webhook_id,
                "addresses": len(wanted),
                "added": len(to_add),
                "removed": len(to_remove),
            }
    except alchemy.AlchemyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/api/settings/realtime")
async def get_realtime_settings() -> dict[str, object]:
    token = db.get_setting("alchemy_auth_token")
    return {
        "configured": bool(token),
        "token_hint": _key_hint(token),
        "public_base_url": _public_base_url(),
        "supported_chains": live_chains(),
        "helius_configured": bool(db.get_setting("helius_api_key")),
        "webhooks": [
            {
                "chain": row["chain"],
                "network": row["network"],
                "webhook_id": row["webhook_id"],
                "address_count": row["address_count"],
                "synced_at": row["synced_at"],
            }
            for row in db.list_webhooks()
        ],
    }


@app.put("/api/settings/realtime")
async def save_realtime_settings(body: Annotated[dict, Body()]) -> dict[str, object]:
    if "auth_token" in body:
        token = str(body.get("auth_token") or "").strip()
        if token:
            db.set_setting("alchemy_auth_token", token)
        else:
            db.delete_setting("alchemy_auth_token")
    if "helius_api_key" in body:
        key = str(body.get("helius_api_key") or "").strip()
        if key:
            db.set_setting("helius_api_key", key)
        else:
            db.delete_setting("helius_api_key")
    if "public_base_url" in body:
        url = str(body.get("public_base_url") or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=422, detail="The public URL must start with https://"
            )
        if url:
            db.set_setting("public_base_url", url)
        else:
            db.delete_setting("public_base_url")
    return await get_realtime_settings()


@app.post("/api/settings/realtime/sync")
async def sync_realtime() -> dict[str, object]:
    """Reconcile every chain that has live watchlists, and drop the rest."""
    chains = set(db.realtime_chains()) | {row["chain"] for row in db.list_webhooks()}
    results = []
    for value in sorted(chains):
        try:
            # Solana is not an Alchemy Notify product; it has its own provider
            # and its own endpoint, but the same event store behind it.
            if value == Chain.solana.value:
                results.append(await sync_solana())
            else:
                results.append(await sync_realtime_chain(Chain(value)))
        except HTTPException as exc:
            results.append({"chain": value, "error": exc.detail})
    return {"synced": results}


#: Marker used by the reachability probe so its own delivery is not mistaken
#: for a misconfigured webhook in the log.
PROBE_WEBHOOK_ID = "dice-reachability-probe"


@app.post("/api/webhooks/helius")
async def receive_helius_webhook(request: Request) -> dict[str, object]:
    """Helius enhanced-webhook delivery — the Solana equivalent of the above.

    Authenticated by the ``Authorization`` header agreed when the webhook was
    created. That is weaker than Alchemy's HMAC — Helius does not sign bodies
    — so the secret is full length and compared in constant time, and the path
    stays idempotent: replaying a delivery re-inserts events already keyed by
    signature and changes nothing.
    """
    registration = db.get_webhook(Chain.solana.value)
    if registration is None:
        # Nothing registered means nothing to verify against, so nothing is
        # processed. Answered 200 for the same reason as the Alchemy path:
        # a provider that sees non-2xx retries, then disables the webhook.
        db.record_delivery(
            chain=Chain.solana.value,
            status="unknown_webhook",
            detail="no Solana webhook registered on this server",
        )
        return {"ignored": "no solana webhook registered", "events": 0}

    if not helius.verify_auth(
        request.headers.get("Authorization"), registration["signing_key"]
    ):
        db.record_delivery(
            chain=Chain.solana.value,
            status="bad_signature",
            detail="Authorization header did not match the stored secret",
        )
        raise HTTPException(status_code=401, detail="Bad authorization.")

    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError:
        db.record_delivery(chain=Chain.solana.value, status="bad_json")
        raise HTTPException(status_code=400, detail="Body is not JSON.")

    result = await realtime.ingest_solana(payload)
    db.record_delivery(
        chain=Chain.solana.value,
        status="delivered",
        detail=f"{result['events']} event(s), {result['signals']} signal(s)",
    )
    return result


@app.post("/api/webhooks/alchemy")
async def receive_alchemy_webhook(request: Request) -> dict[str, object]:
    """Alchemy Address Activity delivery — the live path into signals.

    Authenticated by ``X-Alchemy-Signature`` (HMAC-SHA256 of the raw body with
    the webhook's signing key), so this endpoint is safe to expose publicly.
    Every outcome is written to the delivery log: a webhook that is arriving
    but failing looks nothing like one that never arrives, and the difference
    is invisible without it.
    """
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError:
        db.record_delivery(chain=None, status="bad_json")
        raise HTTPException(status_code=400, detail="Body is not JSON.")

    webhook_id = str(payload.get("webhookId") or "")
    if webhook_id == PROBE_WEBHOOK_ID:
        db.record_delivery(chain=None, status="probe", detail="reachability check")
        return {"probe": "ok"}

    registration = db.webhook_by_id(webhook_id) if webhook_id else None
    if registration is None:
        # Not a webhook this server created, so there is no signing key to
        # verify it with and nothing is processed. Answered 200 on purpose:
        # Alchemy probes the URL around creation time — before the id is
        # stored — and treats non-2xx replies as a failing endpoint worth
        # retrying and eventually disabling.
        log.warning("ignored delivery for unregistered webhook %r", webhook_id)
        db.record_delivery(
            chain=None, status="unknown_webhook", detail=webhook_id[:80]
        )
        return {"ignored": "unknown webhook id", "events": 0, "signals": 0}

    signature = request.headers.get("X-Alchemy-Signature")
    if not alchemy.verify_signature(raw, signature, registration["signing_key"]):
        db.record_delivery(
            chain=registration["chain"],
            status="bad_signature",
            detail="signature did not match the stored signing key",
        )
        raise HTTPException(status_code=401, detail="Bad signature.")

    if str(payload.get("type") or "") != "ADDRESS_ACTIVITY":
        db.record_delivery(
            chain=registration["chain"],
            status="ignored_type",
            detail=str(payload.get("type"))[:80],
        )
        return {"ignored": payload.get("type")}

    activity_count = len((payload.get("event") or {}).get("activity") or [])
    summary = await realtime.ingest(payload, chain=Chain(registration["chain"]))
    db.record_delivery(
        chain=registration["chain"],
        status="ok",
        activity_count=activity_count,
        stored=int(summary["stored"]),
        signals=int(summary["signals"]),
    )
    if summary["signals"]:
        log.info(
            "live: %s events on %s produced %s signal(s)",
            summary["events"],
            registration["chain"],
            summary["signals"],
        )
    return summary


@app.get("/api/settings/pool")
async def get_pool_settings() -> dict[str, object]:
    """The pooled signal threshold, and what it currently works out to."""
    chains = {
        chain: db.pool_size(chain) for chain in db.realtime_chains()
    }
    return {
        "pool_pct": realtime.pool_pct(),
        "pool_min_wallets": realtime.pool_min_wallets(),
        "signal_airdrops": realtime.signal_airdrops(),
        "risk_screening": realtime.risk_screening(),
        "max_pool_age_hours": realtime.max_pool_age_hours(),
        "sweep_seconds": realtime.sweep_seconds(),
        "window_hours": realtime.pool_window_hours(),
        "pools": [
            {
                "chain": chain,
                "wallets": size,
                "required": realtime.pool_threshold(size),
            }
            for chain, size in sorted(chains.items())
        ],
    }


@app.put("/api/settings/pool")
async def save_pool_settings(body: Annotated[dict, Body()]) -> dict[str, object]:
    if "pool_pct" in body:
        try:
            pct = float(body["pool_pct"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="pool_pct must be a number.")
        if not 0 <= pct <= 100:
            raise HTTPException(
                status_code=422, detail="pool_pct must be between 0 and 100."
            )
        db.set_setting("pool_pct", str(pct))
    if "pool_min_wallets" in body:
        try:
            floor = int(body["pool_min_wallets"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="pool_min_wallets must be a whole number."
            )
        if floor < 2:
            raise HTTPException(
                status_code=422, detail="pool_min_wallets must be at least 2."
            )
        db.set_setting("pool_min_wallets", str(floor))
    if "max_pool_age_hours" in body:
        raw = body["max_pool_age_hours"]
        if raw in (None, "", 0):
            db.delete_setting("max_pool_age_hours")  # no limit
        else:
            try:
                age = float(raw)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail="max_pool_age_hours must be a number of hours.",
                )
            if age <= 0:
                raise HTTPException(
                    status_code=422, detail="max_pool_age_hours must be positive."
                )
            db.set_setting("max_pool_age_hours", str(age))
    if "sweep_seconds" in body:
        try:
            seconds = int(body["sweep_seconds"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="sweep_seconds must be a whole number."
            )
        if seconds < 15:
            raise HTTPException(
                status_code=422,
                detail="sweep_seconds must be at least 15 — every pass spends "
                "market and risk lookups, which is the budget that runs out.",
            )
        db.set_setting("live_sweep_seconds", str(seconds))
    if "risk_screening" in body:
        db.set_setting(
            "risk_screening", "true" if body["risk_screening"] else "false"
        )
    if "signal_airdrops" in body:
        db.set_setting(
            "signal_airdrops", "true" if body["signal_airdrops"] else "false"
        )
    return await get_pool_settings()


@app.get("/api/cohorts/overlap")
async def cohort_overlap(
    universe: Annotated[int, Query(ge=1000)] = cohorts.DEFAULT_UNIVERSE,
    min_overlap: Annotated[int, Query(ge=1)] = cohorts.MIN_OVERLAP,
) -> dict[str, object]:
    """Which watchlists share wallets, ranked by how far above chance.

    Answers "the wallets that farmed X are now in Y" from data already
    stored, so it costs nothing to ask.
    """
    return cohorts.overlap_matrix(universe=universe, min_overlap=min_overlap)


@app.post("/api/cohorts/derive")
async def derive_cohorts(body: Annotated[dict, Body()] = None) -> dict[str, object]:
    """Rebuild the repeat-wallet cohorts from every real cohort.

    Runs automatically whenever cohorts change; this is the manual trigger and
    the place to change the threshold.
    """
    body = body or {}
    threshold = body.get("min_cohorts")
    if threshold is not None:
        try:
            threshold = int(threshold)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="min_cohorts must be a whole number."
            )
        if threshold < 2:
            raise HTTPException(
                status_code=422,
                detail="min_cohorts must be at least 2 — appearing in one "
                "cohort is not a repeat.",
            )
        db.set_setting("derived_min_cohorts", str(threshold))
    return {
        "min_cohorts": cohorts.min_cohorts(),
        "derived": cohorts.refresh_all_derived(threshold),
    }


def _refresh_derived_quietly() -> None:
    """Keep the repeat-wallet cohorts current after any change to the inputs.

    Deliberately swallows failures: adding a watchlist must not fail because a
    derived one could not be rebuilt.
    """
    try:
        cohorts.refresh_all_derived()
    except Exception:  # pragma: no cover - defensive
        log.exception("automatic derived cohort refresh failed")


@app.get("/api/cohorts/overlap/{a_id}/{b_id}")
async def cohort_shared_wallets(
    a_id: int, b_id: int, limit: Annotated[int, Query(ge=1, le=2000)] = 500
) -> dict[str, object]:
    """The actual wallets two cohorts have in common."""
    for watchlist_id in (a_id, b_id):
        if db.get_watchlist(watchlist_id) is None:
            raise HTTPException(
                status_code=404, detail=f"Watchlist {watchlist_id} not found."
            )
    wallets = db.shared_wallets(a_id, b_id, limit=limit)
    return {"a_id": a_id, "b_id": b_id, "count": len(wallets), "wallets": wallets}


@app.get("/api/live/tokens")
async def live_tokens(
    watchlist_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    include_airdrops: Annotated[bool, Query()] = False,
    include_untradeable: Annotated[bool, Query()] = False,
    max_pool_age_hours: Annotated[float | None, Query(gt=0)] = None,
) -> dict[str, object]:
    """What the watched wallets are buying right now, below threshold included.

    A signal only appears once the threshold is crossed. This is the view of
    the build-up before that, so a token halfway there is visible instead of
    invisible. Two classes are excluded by default: one-way arrivals
    (``include_airdrops``) and tokens with no liquidity pool at all
    (``include_untradeable``), neither of which anybody actually bought.
    """
    return {
        "tokens": await realtime.accumulation_board(
            watchlist_id=watchlist_id,
            limit=limit,
            only_buys=not include_airdrops,
            only_tradeable=not include_untradeable,
            max_pool_age_hours=max_pool_age_hours,
        ),
        "include_airdrops": include_airdrops,
        "include_untradeable": include_untradeable,
        "max_pool_age_hours": max_pool_age_hours,
    }


@app.get("/api/tokens/risk")
async def token_risk(
    chain: Annotated[Chain, Query()],
    address: Annotated[str, Query(min_length=3)],
    refresh: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    """Screen one token's contract on demand.

    Answers the question a signal raises but cannot answer by itself: ten
    wallets bought it, but can it be sold again?
    """
    verdicts = await security.screen(chain, [address], refresh=refresh)
    verdict = verdicts.get(address.lower()) or security.unchecked("no answer")
    return {"chain": chain.value, "token_address": address.lower(), **verdict}


@app.get("/api/settings/ai")
def get_ai_settings() -> dict[str, object]:
    active = ai.provider()
    return {
        "provider": active,
        "configured": bool(active),
        "openrouter_hint": _key_hint(db.get_setting("openrouter_api_key")),
        "anthropic_hint": _key_hint(db.get_setting("anthropic_api_key")),
        "enrichment": ai.enabled(),
        "model": ai.model(),
        "review_model": ai.review_model(),
        "default_models": ai.DEFAULT_MODELS,
        "themes": db.theme_counts(),
    }


@app.put("/api/settings/ai")
def save_ai_settings(body: Annotated[dict, Body()]) -> dict[str, object]:
    for field, setting in (
        ("openrouter_api_key", "openrouter_api_key"),
        ("anthropic_api_key", "anthropic_api_key"),
        ("model", "ai_model"),
        ("review_model", "ai_review_model"),
    ):
        if field not in body:
            continue
        value = str(body.get(field) or "").strip()
        if value:
            db.set_setting(setting, value)
        else:
            db.delete_setting(setting)
    if "enrichment" in body:
        db.set_setting("ai_enrichment", "true" if body["enrichment"] else "false")
    return get_ai_settings()


@app.get("/api/ai/models")
async def ai_models(refresh: Annotated[bool, Query()] = False) -> dict[str, object]:
    """Every model the active provider accepts, with what it costs.

    Public on OpenRouter, so the picker is populated before a key is saved —
    choosing a model and pasting a key are the same sitting.
    """
    try:
        models = await ai.list_models(refresh=refresh)
    except ai.AIUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {
        "provider": ai.provider() or "openrouter",
        "selected": ai.model(),
        "models": models,
    }


@app.post("/api/settings/ai/test")
async def test_ai_key() -> dict[str, object]:
    """Prove the key works now, rather than finding out when a signal fires."""
    try:
        return await ai.check_key()
    except ai.AIUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/ai/review")
async def run_ai_review() -> dict[str, object]:
    """Read the measured outcomes and propose parameter changes.

    Deliberately on demand rather than scheduled: it costs money per run, and
    it has nothing new to say until more signals have been scored.
    """
    scoreboard = performance.summarise()
    leaderboard = wallets.leaderboard(Chain.ethereum.value, limit=25)
    payload = {
        "scoreboard": {
            key: value for key, value in scoreboard.items() if key != "recent"
        },
        "signals": scoreboard["recent"],
        "current_settings": {
            "pool_pct": realtime.pool_pct(),
            "pool_min_wallets": realtime.pool_min_wallets(),
            "window_hours": realtime.pool_window_hours(),
            "signal_airdrops": realtime.signal_airdrops(),
            "risk_screening": realtime.risk_screening(),
            "min_liquidity_usd": settings.min_liquidity_usd,
        },
        "wallets": {
            key: value for key, value in leaderboard.items() if key != "rows"
        },
        "top_wallets": leaderboard["rows"][:15],
        "themes": db.theme_counts(),
    }
    try:
        result = await ai.review(payload)
    except ai.AIUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    # Kept so the UI can show the last review without paying for a new one.
    db.set_setting("ai_last_review", json.dumps({**result, "at": db.utcnow_iso()}))
    return result


@app.get("/api/ai/review")
def last_ai_review() -> dict[str, object]:
    stored = db.get_setting("ai_last_review")
    if not stored:
        return {"review": None}
    try:
        return {"review": json.loads(stored)}
    except ValueError:  # pragma: no cover - corrupt setting
        return {"review": None}


@app.get("/api/wallets/leaderboard")
def wallet_leaderboard(
    chain: Annotated[Chain, Query()] = Chain.ethereum,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, object]:
    """Rank the watched wallets by what they have actually done.

    The pooled signal counts heads. This is where you find out which of those
    heads are worth counting — hit rate, median return of the signals a wallet
    bought into, and how many hours ahead of the signal it moved.
    """
    return wallets.leaderboard(chain.value, limit=limit)


@app.get("/api/signals/performance")
def signal_performance() -> dict[str, object]:
    """Whether the signals were right, measured against the price afterwards.

    Every other number in DICE describes the *inputs* to a signal. This is the
    only one that describes the result, which makes it the only basis for
    changing a threshold on evidence instead of on feel.
    """
    return performance.summarise()


@app.post("/api/signals/performance/refresh")
async def refresh_signal_performance() -> dict[str, object]:
    """Fill in any horizons that have come due, now rather than on the sweep."""
    filled = await performance.fill_horizons(limit=200)
    return {"filled": filled, **performance.summarise()}


@app.get("/api/live/exits")
async def live_exits(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    """What the watched wallets are selling.

    DICE has only ever watched money going in. This is the same evidence read
    the other way, and for a token already held on a signal it is the more
    urgent half.
    """
    return {"tokens": await realtime.distribution_board(limit=limit)}


@app.post("/api/live/sweep")
async def run_live_sweep() -> dict[str, object]:
    """Re-check stored events against every live threshold, now."""
    return await realtime.sweep()


@app.get("/api/settings/realtime/deliveries")
async def list_deliveries(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    rows = db.list_deliveries(limit)
    real = [row for row in rows if row["status"] == "ok"]
    return {
        "deliveries": rows,
        "last_delivery_at": real[0]["received_at"] if real else None,
        "total_shown": len(rows),
    }


@app.post("/api/settings/realtime/check-url")
async def check_webhook_url() -> dict[str, object]:
    """Call our own public webhook URL to prove Alchemy can reach it.

    This is the half a simulated signal cannot test: DNS, TLS, the reverse
    proxy and any firewall between the internet and this process.
    """
    url = _webhook_url()
    probe = {"webhookId": PROBE_WEBHOOK_ID, "type": "ADDRESS_ACTIVITY", "event": {}}
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.post(url, json=probe)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach {url} — {exc}. Alchemy will not be able to "
            "deliver either. Check the public URL, DNS and TLS.",
        ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"{url} answered {response.status_code}. Something other than "
            "DICE is serving that path — check the reverse proxy.",
        )
    if (response.json() or {}).get("probe") != "ok":
        raise HTTPException(
            status_code=502,
            detail=f"{url} is reachable but answered unexpectedly — the URL "
            "probably points at a different application.",
        )
    return {"reachable": True, "url": url}


@app.post("/api/settings/realtime/simulate")
async def simulate_signal(body: Annotated[dict, Body()]) -> dict[str, object]:
    """Push a synthetic buy through the real live path, to prove it end to end.

    Uses the watchlist's own wallets and the genuine ingest code — parsing,
    the event store, the threshold and the Telegram push — so a success here
    means everything downstream of Alchemy works. The token is an obviously
    fake address, and the resulting signal can be dismissed like any other.
    """
    watchlist_id = body.get("watchlist_id")
    if not isinstance(watchlist_id, int):
        raise HTTPException(status_code=422, detail="Provide a watchlist_id.")
    watchlist = db.get_watchlist(watchlist_id)
    if watchlist is None:
        raise HTTPException(status_code=404, detail="Watchlist not found.")
    if not watchlist.get("realtime"):
        raise HTTPException(
            status_code=422,
            detail="Switch Live on for this watchlist first — the simulation "
            "runs through the same path a real delivery takes.",
        )

    chain = Chain(watchlist["chain"])
    wallets = db.get_wallets(watchlist_id)
    needed = monitor.effective_min_wallets(
        int(watchlist["min_wallets"]),
        float(watchlist["min_wallets_pct"]),
        len(wallets),
    )
    if len(wallets) < needed:
        raise HTTPException(
            status_code=422,
            detail=f"The watchlist has {len(wallets)} wallets but needs "
            f"{needed} buyers to fire — lower the threshold to test it.",
        )

    # A fresh tx hash per run, so repeat simulations are not deduplicated away.
    run = uuid.uuid4().hex
    payload = {
        "webhookId": "simulated",
        "type": "ADDRESS_ACTIVITY",
        "event": {
            "network": alchemy.network_for(chain) or chain.value,
            # Both legs of a swap, as Alchemy delivers them: the token
            # arriving, and the wallet paying for it in the same transaction.
            # Without the payment leg this would look like an airdrop and be
            # filtered out — which is exactly what a test should not do.
            "activity": [
                leg
                for index, wallet in enumerate(wallets[:needed])
                for leg in (
                    {
                        "fromAddress": "0x" + "1" * 40,
                        "toAddress": wallet,
                        "blockNum": "0x0",
                        "hash": f"0x{run}{index:02x}",
                        "value": 1234.5,
                        "asset": SIMULATED_SYMBOL,
                        "category": "erc20",
                        "rawContract": {
                            "address": SIMULATED_TOKEN,
                            "decimals": 18,
                            "rawValue": "0x1",
                        },
                    },
                    {
                        "fromAddress": wallet,
                        "toAddress": "0x" + "1" * 40,
                        "blockNum": "0x0",
                        "hash": f"0x{run}{index:02x}",
                        "value": 0.5,
                        "asset": "ETH",
                        "category": "external",
                        "rawContract": {"address": None},
                    },
                )
            ],
        },
    }

    summary = await realtime.ingest(payload, chain=chain)
    # A delivery only records now, so the probe has to run the evaluation too
    # — otherwise it would stop proving the thing it exists to prove: that
    # everything downstream of Alchemy works, signal and notification included.
    swept = await realtime.sweep()
    summary = {**summary, "signals": int(swept["signals"])}
    db.record_delivery(
        chain=chain.value,
        status="simulated",
        activity_count=needed,
        stored=int(summary["stored"]),
        signals=int(summary["signals"]),
    )
    telegram_token, telegram_chat = db.telegram_credentials()
    return {
        **summary,
        "wallets_used": needed,
        "token_address": SIMULATED_TOKEN,
        "telegram_configured": bool(telegram_token and telegram_chat),
    }


# ------------------------------------------------------------------ watchlists


def _watchlist_out(row: dict) -> WatchlistOut:
    wallet_count = int(row.get("wallet_count") or 0)
    return WatchlistOut(
        id=row["id"],
        name=row["name"],
        chain=row["chain"],
        source_token_address=row.get("source_token_address"),
        notes=row.get("notes") or "",
        wallet_count=wallet_count,
        min_wallets=row["min_wallets"],
        min_wallets_pct=row["min_wallets_pct"],
        effective_min_wallets=monitor.effective_min_wallets(
            int(row["min_wallets"]),
            float(row["min_wallets_pct"]),
            min(wallet_count, settings.monitor_max_wallets) or wallet_count,
        ),
        buy_window_hours=row["buy_window_hours"],
        monitor_interval_hours=row["monitor_interval_hours"],
        min_buy_usd=row["min_buy_usd"],
        auto_monitor=bool(row["auto_monitor"]),
        ignore_tokens=json.loads(row.get("ignore_tokens") or "[]"),
        buy_detection=row.get("buy_detection") or "both",
        realtime=bool(row.get("realtime")),
        derived=bool(row.get("derived")),
        created_at=row["created_at"],
        last_run_at=row.get("last_run_at"),
        last_run_status=row.get("last_run_status"),
        last_run_error=row.get("last_run_error"),
        next_run_at=row.get("next_run_at") if row["auto_monitor"] else None,
        active_signals=int(row.get("active_signals") or 0),
    )


def _overview_or_404(watchlist_id: int) -> dict:
    row = db.get_watchlist_overview(watchlist_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Watchlist not found.")
    return row


def _enforce_wallet_cap(count: int) -> None:
    cap = settings.monitor_max_wallets
    if count > cap:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{count} wallets exceeds the monitoring cap of {cap}. "
                "Narrow the holder query (dates / min balance), pass top_n to "
                "keep the largest holders, or raise DICE_MONITOR_MAX_WALLETS."
            ),
        )


@app.get("/api/watchlists")
async def list_watchlists() -> list[WatchlistOut]:
    return [_watchlist_out(row) for row in db.list_watchlists()]


@app.post("/api/watchlists", status_code=201)
async def create_watchlist(req: Annotated[WatchlistCreate, Body()]) -> WatchlistOut:
    _enforce_wallet_cap(len(req.wallets))
    ignore_tokens = list(req.ignore_tokens)
    # The source token is ignored by default: these wallets obviously already
    # buy it, so it would fire a trivial signal. Removable via PATCH.
    if req.source_token_address and req.source_token_address not in ignore_tokens:
        ignore_tokens.insert(0, req.source_token_address)
    watchlist_id = db.create_watchlist(
        name=req.name,
        chain=req.chain.value,
        wallets=req.wallets,
        source_token_address=req.source_token_address,
        notes=req.notes,
        min_wallets=req.min_wallets,
        min_wallets_pct=req.min_wallets_pct,
        buy_window_hours=req.buy_window_hours,
        monitor_interval_hours=req.monitor_interval_hours,
        min_buy_usd=req.min_buy_usd,
        auto_monitor=req.auto_monitor,
        ignore_tokens=ignore_tokens,
        buy_detection=req.buy_detection,
        realtime=req.realtime,
    )
    if req.realtime:
        # Register the wallets with Alchemy now; a failure here must not lose
        # the watchlist, so it is reported through last_sync rather than raised.
        await _try_sync(req.chain)
    # A new cohort can promote wallets into the repeat set, so rebuild it.
    _refresh_derived_quietly()
    return _watchlist_out(_overview_or_404(watchlist_id))


async def _try_sync(chain: Chain) -> None:
    try:
        await sync_realtime_chain(chain)
    except HTTPException as exc:
        log.warning("realtime sync for %s failed: %s", chain.value, exc.detail)


@app.post("/api/watchlists/from-job/{job_id}", status_code=201)
async def create_watchlist_from_job(
    job_id: str, req: Annotated[WatchlistFromJob, Body()]
) -> WatchlistOut:
    result = store.get(job_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Result expired or unknown job id — run the query again.",
        )
    holders_req = result.request
    # summary is sorted by max balance descending, so top_n keeps the whales.
    wallets = [s.wallet_address for s in result.summary]
    if req.top_n is not None:
        wallets = wallets[: req.top_n]
    if not wallets:
        raise HTTPException(status_code=422, detail="The job produced no wallets.")
    _enforce_wallet_cap(len(wallets))

    name = req.name or (
        f"{holders_req.chain.value} {holders_req.token_address[:10]}… "
        f"buyers {holders_req.start_date}→{holders_req.end_date}"
    )
    create_req = WatchlistCreate(
        name=name,
        chain=holders_req.chain,
        wallets=wallets,
        source_token_address=holders_req.token_address,
        notes=(
            f"From holders job: {holders_req.chain.value} "
            f"{holders_req.token_address} {holders_req.start_date}"
            f"→{holders_req.end_date} ({holders_req.holder_mode.value})"
        ),
        min_wallets=req.min_wallets,
        min_wallets_pct=req.min_wallets_pct,
        buy_window_hours=req.buy_window_hours,
        monitor_interval_hours=req.monitor_interval_hours,
        min_buy_usd=req.min_buy_usd,
        auto_monitor=req.auto_monitor,
        ignore_tokens=req.ignore_tokens,
        buy_detection=req.buy_detection,
        realtime=req.realtime,
    )
    return await create_watchlist(create_req)


@app.get("/api/watchlists/{watchlist_id}")
async def get_watchlist(watchlist_id: int) -> WatchlistOut:
    return _watchlist_out(_overview_or_404(watchlist_id))


@app.get("/api/watchlists/{watchlist_id}/wallets")
async def get_watchlist_wallets(watchlist_id: int) -> dict[str, object]:
    _overview_or_404(watchlist_id)
    wallets = db.get_wallets(watchlist_id)
    return {"wallet_count": len(wallets), "wallets": wallets}


@app.patch("/api/watchlists/{watchlist_id}")
async def update_watchlist(
    watchlist_id: int, req: Annotated[WatchlistUpdate, Body()]
) -> WatchlistOut:
    row = _overview_or_404(watchlist_id)
    chain_enum = Chain(row["chain"])

    fields: dict[str, object] = {}
    for name in (
        "name",
        "notes",
        "min_wallets",
        "min_wallets_pct",
        "buy_window_hours",
        "monitor_interval_hours",
        "min_buy_usd",
        "buy_detection",
    ):
        value = getattr(req, name)
        if value is not None:
            fields[name] = value
    if req.auto_monitor is not None:
        fields["auto_monitor"] = int(req.auto_monitor)
    if req.realtime is not None:
        fields["realtime"] = int(req.realtime)
    try:
        if req.ignore_tokens is not None:
            fields["ignore_tokens"] = json.dumps(
                normalize_addresses(chain_enum, req.ignore_tokens)
            )
        add = (
            normalize_addresses(chain_enum, req.add_wallets)
            if req.add_wallets
            else []
        )
        remove = (
            normalize_addresses(chain_enum, req.remove_wallets)
            if req.remove_wallets
            else []
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if add:
        current = set(db.get_wallets(watchlist_id))
        resulting = current | set(add)
        _enforce_wallet_cap(len(resulting - set(remove)))
        db.add_wallets(watchlist_id, add)
    if remove:
        db.remove_wallets(watchlist_id, remove)

    # Interval or auto changes reshape the schedule from the last run onwards.
    if req.monitor_interval_hours is not None or req.auto_monitor:
        interval = float(
            req.monitor_interval_hours or row["monitor_interval_hours"]
        )
        base = row.get("last_run_at") or db.utcnow_iso()
        fields["next_run_at"] = db.iso_plus_hours(base, interval)

    db.update_watchlist_fields(watchlist_id, fields)

    # Anything that changes which wallets Alchemy should watch — the toggle
    # itself, or the membership of an already-live list — needs a reconcile.
    membership_changed = bool(add or remove)
    if req.realtime is not None or (membership_changed and row.get("realtime")):
        await _try_sync(chain_enum)

    return _watchlist_out(_overview_or_404(watchlist_id))


@app.delete("/api/watchlists/{watchlist_id}", status_code=204)
async def delete_watchlist(watchlist_id: int) -> Response:
    row = db.get_watchlist(watchlist_id)
    was_live = bool(row and row.get("realtime"))
    chain = Chain(row["chain"]) if row else None
    if not db.delete_watchlist(watchlist_id):
        raise HTTPException(status_code=404, detail="Watchlist not found.")
    if was_live and chain is not None:
        # Stop paying Alchemy attention to wallets nothing watches any more.
        await _try_sync(chain)
    # Losing a cohort can demote wallets out of the repeat set.
    _refresh_derived_quietly()
    # The reusable Dune query slot for this watchlist is meaningless now.
    db.drop_purpose_slots(f"monitor:{watchlist_id}")
    db.drop_purpose_slots(f"positions:{watchlist_id}")
    return Response(status_code=204)


@app.post("/api/watchlists/{watchlist_id}/monitor")
async def run_watchlist_monitor(
    watchlist_id: int, x_dune_api_key: ApiKeyHeader = None
) -> MonitorResult:
    key = resolve_api_key(x_dune_api_key)
    try:
        return await monitor.run_monitor(watchlist_id, api_key=key, trigger="manual")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/watchlists/{watchlist_id}/runs")
async def list_watchlist_runs(
    watchlist_id: int, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[MonitorRunOut]:
    _overview_or_404(watchlist_id)
    return [monitor.run_to_out(row) for row in db.list_runs(watchlist_id, limit)]


# --------------------------------------------------------------------- signals


@app.get("/api/signals")
async def list_signals(
    watchlist_id: Annotated[int | None, Query()] = None,
    include_dismissed: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[SignalOut]:
    rows = db.list_signals(
        watchlist_id=watchlist_id,
        include_dismissed=include_dismissed,
        limit=limit,
    )
    return [monitor.signal_to_out(row) for row in rows]


@app.get("/api/signals/{signal_id}/brief")
def signal_brief(signal_id: int) -> dict[str, object]:
    """The researched brief for one signal, if one was written."""
    brief = db.get_brief(signal_id)
    if brief is None:
        raise HTTPException(
            status_code=404, detail="No brief was written for this signal."
        )
    return brief


@app.post("/api/signals/{signal_id}/brief")
async def research_signal(signal_id: int) -> dict[str, object]:
    """Research one signal now.

    The sweep does this on its own, but a signal that fired before a key was
    saved would otherwise wait for the next one — and a brief is most useful
    while the position is still open.
    """
    row = db.get_signal(signal_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found.")
    if not ai.enabled():
        raise HTTPException(
            status_code=503,
            detail="No model key saved, or research is switched off.",
        )
    chain = Chain(row["chain"])
    markets = await realtime.tradeable(chain, [row["token_address"]])
    brief = await realtime.enrich(
        monitor.signal_to_out(row),
        chain=chain,
        market=markets.get(row["token_address"]),
    )
    if brief is None:
        raise HTTPException(
            status_code=503, detail="The model could not be reached in time."
        )
    return brief


@app.post("/api/signals/{signal_id}/dismiss")
async def dismiss_signal(signal_id: int) -> SignalOut:
    if not db.set_signal_status(signal_id, "dismissed"):
        raise HTTPException(status_code=404, detail="Signal not found.")
    row = db.get_signal(signal_id)
    assert row is not None
    return monitor.signal_to_out(row)


@app.post("/api/signals/{signal_id}/restore")
async def restore_signal(signal_id: int) -> SignalOut:
    if not db.set_signal_status(signal_id, "active"):
        raise HTTPException(status_code=404, detail="Signal not found.")
    row = db.get_signal(signal_id)
    assert row is not None
    return monitor.signal_to_out(row)


# --------------------------------------------------------------------- login
#
# Paths that must answer without a session. Everything else is gated.
#
# The webhooks are the load-bearing entry: Alchemy and Helius cannot present a
# cookie, and they are already authenticated by an HMAC over the raw body,
# which is a stronger check than a password. Gating them would silently stop
# every signal — a failure this deployment has already had once.
OPEN_PATHS = frozenset({
    "/login", "/api/auth/logout",
    "/api/webhooks/alchemy", "/api/webhooks/helius",
    "/api/health", "/favicon.ico",
})
OPEN_PREFIXES = ("/static/",)   # the login page needs its own stylesheet


def _safe_next(target: str | None) -> str:
    """Only ever redirect back into this site."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def _login_page(request: Request, *, error: str = "", status: int = 200) -> Response:
    """Render the sign-in page with any error already in the markup.

    Server-rendered rather than fetched, so the page works with JavaScript
    switched off. A login that needs JavaScript is a login that can lock you
    out of your own server.
    """
    html = (FRONTEND_DIR / "login.html").read_text(encoding="utf-8")
    block = (
        '<div class="alert alert-danger py-2 px-3 small mt-3 mb-0" role="alert">'
        f"{html_escape(error)}</div>"
        if error else ""
    )
    return HTMLResponse(
        html.replace("__ERROR__", block)
            .replace("__NEXT__", html_escape(_safe_next(request.query_params.get("next")), quote=True))
            .replace("__MAX_ATTEMPTS__", str(auth.MAX_ATTEMPTS))
            .replace("__LOCKOUT__", str(auth.LOCKOUT_MINUTES)),
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def _client_address(request: Request) -> str:
    # nginx sets X-Forwarded-For; the first hop is the real client.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    """The gate. Open paths pass; everything else needs a live session.

    With no password set the gate is transparent, so an existing install does
    not lock itself out on upgrade — it opts in by setting one.
    """
    path = request.url.path
    if (
        not auth.password_is_set()
        or path in OPEN_PATHS
        or path.startswith(OPEN_PREFIXES)
        or auth.session_is_valid(request.cookies.get(auth.SESSION_COOKIE))
    ):
        return await call_next(request)

    if path.startswith("/api/"):
        # An API caller gets a status it can act on, not a page it cannot read.
        return JSONResponse({"detail": "Sign in required."}, status_code=401)
    return RedirectResponse(f"/login?next={quote(path, safe='/')}", status_code=303)


@app.get("/login", include_in_schema=False)
async def login_form(request: Request) -> Response:
    if auth.session_is_valid(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return _login_page(request)


@app.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    password: Annotated[str, Form()] = "",
    remember: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    address = _client_address(request)
    waiting = auth.lockout_remaining(address)
    if waiting:
        minutes = max(1, round(waiting / 60))
        return _login_page(
            request,
            error=f"Too many attempts. Try again in {minutes} minute"
                  f"{'s' if minutes != 1 else ''}.",
            status=429,
        )

    if not auth.check_password(password):
        auth.record_failure(address)
        left = auth.MAX_ATTEMPTS - auth.count_recent_failures(address)
        # Warn when the lockout is close. Someone mistyping their own password
        # deserves it, and an attacker learns nothing they could not count.
        hint = f" {left} attempt{'s' if left != 1 else ''} left." if 0 < left <= 3 else ""
        return _login_page(request, error=f"Incorrect password.{hint}", status=401)

    auth.clear_failures(address)
    token, expires = auth.start_session(remember=bool(remember))
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        value=token,
        **auth.cookie_kwargs(expires, secure=request.url.scheme == "https"),
    )
    return response


@app.post("/api/auth/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, object]:
    return {
        "password_set": auth.password_is_set(),
        "signed_in": auth.session_is_valid(request.cookies.get(auth.SESSION_COOKIE)),
    }


@app.put("/api/auth/password")
def change_password(body: Annotated[dict, Body()]) -> dict[str, object]:
    """Set or replace the password. Every existing session is revoked."""
    try:
        auth.set_password(str(body.get("password") or ""))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"password_set": True, "sessions_revoked": True}


if FRONTEND_DIR.is_dir():

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount(
        "/static", StaticFiles(directory=FRONTEND_DIR), name="static"
    )
