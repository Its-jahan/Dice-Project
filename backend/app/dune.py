"""Thin async wrapper around the Dune Analytics execution API.

Flow implemented here:

    POST   /query/{id}/execute      ->  execution_id
    GET    /execution/{id}/status   ->  poll until QUERY_STATE_COMPLETED
    GET    /execution/{id}/results  ->  paginated rows

Two execution paths are supported:

``saved``
    ``DUNE_QUERY_ID`` points at a parametrised query the operator already saved
    in Dune. DICE only passes parameter values. Works on every paid plan.

``ad hoc``
    DICE creates a private query from generated SQL, executes it, and deletes
    nothing (it stays in the caller's Dune account for auditing). Query CRUD
    requires a Dune plan that includes the Query API.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)

_TERMINAL_FAILURE_STATES = {
    "QUERY_STATE_FAILED",
    "QUERY_STATE_CANCELLED",
    "QUERY_STATE_EXPIRED",
}
_COMPLETED = "QUERY_STATE_COMPLETED"
_RESULT_PAGE_SIZE = 10_000


class DuneError(RuntimeError):
    """A Dune API call failed in a way the user should hear about."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class DuneAuthError(DuneError):
    def __init__(self, message: str = "Dune rejected the API key") -> None:
        super().__init__(message, status_code=401)


class DuneUnreachable(DuneError):
    """The request never got an answer — network, TLS or timeout.

    Distinct from other failures because it says nothing about the key: a key
    check must not report "valid" just because the network was down.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=503)


class DuneClient:
    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        if not api_key or not api_key.strip():
            raise DuneAuthError("no Dune API key supplied")
        self._api_key = api_key.strip()
        self._base_url = (base_url or settings.dune_api_base).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"X-Dune-Api-Key": self._api_key},
        )

    async def __aenter__(self) -> "DuneClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ HTTP

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # network/TLS/timeout
            raise DuneUnreachable(f"could not reach Dune: {exc}") from exc

        if response.status_code in (401, 403):
            raise DuneAuthError(
                "Dune rejected the API key (401/403). Check the key and its plan."
            )
        if response.status_code == 429:
            raise DuneError("Dune rate limit hit; retry shortly.", status_code=429)
        if response.status_code >= 400:
            raise DuneError(
                f"Dune returned {response.status_code}: {_short(response.text)}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise DuneError("Dune returned a non-JSON response") from exc

    # --------------------------------------------------------------- queries

    async def validate_key(self) -> bool:
        """Cheap liveness check for a key.

        Dune has no ``/me`` endpoint, so we ask for the status of an execution
        that cannot exist: a bad key answers 401/403, a good key answers 404.
        """
        probe = "/execution/00000000-0000-0000-0000-000000000000/status"
        try:
            await self._request("GET", probe)
        except DuneAuthError:
            return False
        except DuneUnreachable:
            # Says nothing about the key — surface it instead of claiming valid.
            raise
        except DuneError:
            # 404/400 for the bogus execution id means the key authenticated.
            return True
        return True

    async def create_query(self, *, name: str, query_sql: str) -> int:
        payload = {
            "name": name,
            "query_sql": query_sql,
            "is_private": True,
        }
        data = await self._request("POST", "/query", json=payload)
        query_id = data.get("query_id")
        if not isinstance(query_id, int):
            raise DuneError("Dune did not return a query_id when creating the query")
        return query_id

    async def execute_query(
        self,
        query_id: int,
        *,
        parameters: dict[str, Any] | None = None,
        performance: str = "medium",
    ) -> str:
        payload: dict[str, Any] = {"performance": performance}
        if parameters:
            # Dune expects every parameter value as a string.
            payload["query_parameters"] = {k: str(v) for k, v in parameters.items()}
        data = await self._request("POST", f"/query/{query_id}/execute", json=payload)
        execution_id = data.get("execution_id")
        if not execution_id:
            raise DuneError("Dune did not return an execution_id")
        return str(execution_id)

    # ------------------------------------------------------------ executions

    async def wait_for_execution(self, execution_id: str) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + settings.execution_timeout
        interval = settings.poll_interval
        while True:
            status = await self._request("GET", f"/execution/{execution_id}/status")
            state = status.get("state", "")
            if state == _COMPLETED:
                return status
            if state in _TERMINAL_FAILURE_STATES:
                raise DuneError(
                    f"Dune execution {execution_id} ended in state {state}: "
                    f"{_short(str(status.get('error', '')))}"
                )
            if asyncio.get_running_loop().time() > deadline:
                raise DuneError(
                    f"Dune execution {execution_id} did not finish within "
                    f"{settings.execution_timeout}s (last state: {state})",
                    status_code=504,
                )
            await asyncio.sleep(interval)
            # Back off gently so long queries don't hammer the status endpoint.
            interval = min(interval * 1.5, 15.0)

    async def fetch_results(
        self, execution_id: str, *, max_rows: int | None = None
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return ``(rows, truncated)`` for a completed execution."""
        limit = max_rows or settings.max_rows
        rows: list[dict[str, Any]] = []
        offset = 0
        while len(rows) < limit:
            page_size = min(_RESULT_PAGE_SIZE, limit - len(rows))
            data = await self._request(
                "GET",
                f"/execution/{execution_id}/results",
                params={"limit": page_size, "offset": offset},
            )
            page = (data.get("result") or {}).get("rows") or []
            rows.extend(page)
            if len(page) < page_size:
                return rows, False
            offset += len(page)
        return rows, True


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."
