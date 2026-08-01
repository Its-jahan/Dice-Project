"""DICE backend: historical token holders from Dune, exported as files.

API key policy
--------------
The Dune API key is supplied per request in the ``X-Dune-Api-Key`` header, sent
by the browser from a field in the UI. It is never written to disk, never
logged, and never returned in a response; it lives only for the duration of the
request. ``DUNE_API_KEY`` in the environment is an optional fallback for
single-user/CLI deployments.
"""

from __future__ import annotations

import logging
from pathlib import Path
from dataclasses import asdict
from typing import Annotated

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .cache import cache
from .config import settings
from .dune import DuneClient, DuneError
from .exporters import DATASETS, MEDIA_TYPES, export, filename_for
from .holders import apply_holder_mode, build_summary, parse_rows
from .jobs import store
from .models import Chain, ExportFormat, HoldersRequest, HoldersResponse
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

app = FastAPI(
    title="DICE",
    description="Historical token holder extraction via Dune Analytics.",
    version="0.1.0",
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
    """Header key wins; fall back to the server key only if one is configured."""
    key = (header_key or "").strip() or (settings.dune_api_key or "").strip()
    if not key:
        raise HTTPException(
            status_code=401,
            detail="No Dune API key. Enter your key in the UI (it is kept in your "
            "browser and sent only with your own requests).",
        )
    return key


ApiKeyHeader = Annotated[str | None, Header(alias="X-Dune-Api-Key")]


@app.get("/api/config")
async def get_config() -> dict[str, object]:
    """What the UI needs to render itself — never includes the key itself."""
    return {
        "server_key_configured": bool(settings.dune_api_key),
        "saved_query_id": settings.dune_query_id,
        "execution_mode": "saved_query" if settings.dune_query_id else "ad_hoc",
        "max_rows": settings.max_rows,
        "preview_rows": PREVIEW_ROWS,
    }


@app.post("/api/key/validate")
async def validate_key(x_dune_api_key: ApiKeyHeader = None) -> dict[str, bool]:
    """Check a key before the user spends credits on a real run."""
    key = resolve_api_key(x_dune_api_key)
    async with DuneClient(key) as client:
        return {"valid": await client.validate_key()}


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


async def _run_sql(client: DuneClient, *, name: str, sql: str, max_rows: int):
    """Create, execute and drain a one-off diagnostic query."""
    query_id = await client.create_query(name=name, query_sql=sql)
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
        query_id = await client.create_query(
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


if FRONTEND_DIR.is_dir():

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount(
        "/static", StaticFiles(directory=FRONTEND_DIR), name="static"
    )
