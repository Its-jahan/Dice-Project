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
from typing import Annotated

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import settings
from .dune import DuneClient, DuneError
from .exporters import MEDIA_TYPES, export, filename_for
from .holders import apply_holder_mode, build_summary, parse_rows
from .jobs import store
from .models import Chain, ExportFormat, HoldersRequest, HoldersResponse
from .sql import (
    build_columns_sql,
    build_discovery_sql,
    build_query_parameters,
    build_snapshot_sql,
    table_for,
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
async def preview_sql(req: Annotated[HoldersRequest, Body()]) -> dict[str, object]:
    """Show the DuneSQL DICE would run. No key needed, no credits spent."""
    return {
        "sql": build_snapshot_sql(req),
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
    pattern: str = "balance",
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
            client, name=f"DICE discover {pattern}", sql=sql, max_rows=300
        )

    tables = sorted(
        f"{row.get('table_schema')}.{row.get('table_name')}" for row in rows
    )
    return {
        "pattern": pattern,
        "count": len(tables),
        "tables": tables,
        "expected_by_dice": sorted({table_for(chain) for chain in Chain}),
    }


@app.get("/api/columns")
async def describe_source_table(
    chain: Chain = Chain.ethereum,
    x_dune_api_key: ApiKeyHeader = None,
) -> dict[str, object]:
    """Report the real column names of the balance table DICE reads.

    Dune's balance tables are in Open Beta and have been renamed before. When a
    query starts failing with "table/column does not exist", this says what the
    source actually looks like today instead of leaving you to guess.
    """
    key = resolve_api_key(x_dune_api_key)
    async with DuneClient(key) as client:
        rows = await _run_sql(
            client,
            name=f"DICE schema probe {chain.value}",
            sql=build_columns_sql(chain),
            max_rows=1,
        )
    return {
        "table": table_for(chain),
        "columns": sorted(rows[0].keys()) if rows else [],
        "sample_row": rows[0] if rows else None,
    }


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
        else:
            query_id = await client.create_query(
                name=f"DICE holders {req.chain.value} {req.token_address[:10]} "
                f"{req.start_date}..{req.end_date}",
                query_sql=build_snapshot_sql(req),
            )
            parameters = None

        execution_id = await client.execute_query(query_id, parameters=parameters)
        await client.wait_for_execution(execution_id)
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
        "preview": [s.model_dump(mode="json") for s in snapshots[:PREVIEW_ROWS]],
        "summary_preview": [s.model_dump(mode="json") for s in summary[:PREVIEW_ROWS]],
    }


@app.get("/api/export/{job_id}")
async def download_export(
    job_id: str,
    fmt: Annotated[ExportFormat, Query(alias="format")] = ExportFormat.csv,
) -> Response:
    result = store.get(job_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Result expired or unknown job id — run the query again.",
        )
    payload = export(result, fmt)
    return Response(
        content=payload,
        media_type=MEDIA_TYPES[fmt],
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename_for(result, fmt)}"'
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
