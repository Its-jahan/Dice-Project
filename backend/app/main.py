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
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from dataclasses import asdict
from typing import Annotated, AsyncIterator

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, monitor
from .cache import cache
from .config import settings
from .dune import DuneClient, DuneError, ensure_query
from .exporters import DATASETS, MEDIA_TYPES, export, filename_for
from .holders import apply_holder_mode, build_summary, parse_rows
from .jobs import store
from .models import (
    Chain,
    ExportFormat,
    HoldersRequest,
    HoldersResponse,
    MonitorResult,
    MonitorRunOut,
    SignalOut,
    WatchlistCreate,
    WatchlistFromJob,
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
    build_contracts_catalog_sql,
    build_discovery_sql,
    build_query_parameters,
    build_snapshot_sql,
)

log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
#: How many snapshot rows the JSON preview returns. Full data goes in exports.
PREVIEW_ROWS = 500


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run the watchlist scheduler for the lifetime of the process."""
    scheduler_task: asyncio.Task[None] | None = None
    if settings.monitor_enabled:
        scheduler_task = asyncio.create_task(monitor.scheduler_loop())
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task


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
        chat_id = str(body.get("chat_id") or "").strip()
        if chat_id:
            db.set_setting("telegram_chat_id", chat_id)
        else:
            db.delete_setting("telegram_chat_id")
    return await get_notification_settings()


@app.post("/api/settings/notifications/test")
async def test_notification() -> dict[str, object]:
    error = await monitor.send_telegram_message(
        "DICE test message — signal alerts will arrive in this chat."
    )
    if error:
        raise HTTPException(status_code=502, detail=error)
    return {"sent": True}


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


@app.post("/api/holders")
async def get_holders(
    req: Annotated[HoldersRequest, Body()],
    x_dune_api_key: ApiKeyHeader = None,
) -> dict[str, object]:
    """Run the snapshot query and return a preview plus a job id for export."""
    key = resolve_api_key(x_dune_api_key)

    async with DuneClient(key) as client:
        if settings.dune_query_id:
            query_id = settings.dune_query_id
            parameters = build_query_parameters(req)
            execution_id = await client.execute_query(query_id, parameters=parameters)
            await client.wait_for_execution(execution_id)
        else:
            execution_id = await _run_holders_query(client, req)

        rows, truncated = await client.fetch_results(execution_id)

    snapshots = apply_holder_mode(parse_rows(rows, req), req)
    summary = build_summary(snapshots)
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
    )
    return _watchlist_out(_overview_or_404(watchlist_id))


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
    return _watchlist_out(_overview_or_404(watchlist_id))


@app.delete("/api/watchlists/{watchlist_id}", status_code=204)
async def delete_watchlist(watchlist_id: int) -> Response:
    if not db.delete_watchlist(watchlist_id):
        raise HTTPException(status_code=404, detail="Watchlist not found.")
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


if FRONTEND_DIR.is_dir():

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount(
        "/static", StaticFiles(directory=FRONTEND_DIR), name="static"
    )
