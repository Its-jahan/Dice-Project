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
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, AsyncIterator

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import alchemy, db, monitor, realtime
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
            "supported_chains": alchemy.supported_chains(),
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


def _webhook_url() -> str:
    base = _public_base_url()
    if not base:
        raise HTTPException(
            status_code=422,
            detail="Set the public HTTPS URL of this server first — Alchemy has "
            "to be able to reach it to deliver events.",
        )
    return base.rstrip("/") + "/api/webhooks/alchemy"


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
        "supported_chains": alchemy.supported_chains(),
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
            results.append(await sync_realtime_chain(Chain(value)))
        except HTTPException as exc:
            results.append({"chain": value, "error": exc.detail})
    return {"synced": results}


#: Marker used by the reachability probe so its own delivery is not mistaken
#: for a misconfigured webhook in the log.
PROBE_WEBHOOK_ID = "dice-reachability-probe"


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


@app.get("/api/live/tokens")
async def live_tokens(
    watchlist_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    include_airdrops: Annotated[bool, Query()] = False,
    include_untradeable: Annotated[bool, Query()] = False,
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
        ),
        "include_airdrops": include_airdrops,
        "include_untradeable": include_untradeable,
    }


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
