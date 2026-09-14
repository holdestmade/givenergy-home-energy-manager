"""Async client for the Home Energy Manager authenticated API.

Only the documented surface is implemented:

    GET  /api/control/status
    GET  /api/snapshot
    GET  /api/commands/{command_id}
    POST /api/control/force-charge            {"minutes": n}
    POST /api/control/force-charge/stop
    POST /api/control/force-discharge         {"minutes": n}
    POST /api/control/force-discharge/stop

Every POST carries an Idempotency-Key. Per the HEM docs a retry is only safe
with the *same* key and payload, so the caller owns key generation and the
client never retries a mutation on its own.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import aiohttp
from aiohttp import ClientError, ClientTimeout

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=10)


class HemError(Exception):
    """Base error for the HEM API."""


class HemConnectionError(HemError):
    """The API could not be reached."""


class HemAuthError(HemError):
    """HTTP 401 - the bearer key is missing or wrong."""


class HemPermissionError(HemError):
    """HTTP 403 - battery control through the API is switched off."""


class HemNotFoundError(HemError):
    """HTTP 404 - the endpoint or command id does not exist on this install."""


class HemConflictError(HemError):
    """HTTP 409 - action already running, idempotency clash, or no snapshot yet."""

    def __init__(self, message: str, command_id: str | None = None) -> None:
        """Keep the existing command id when HEM reports one."""
        super().__init__(message)
        self.command_id = command_id


class HemRateLimitError(HemError):
    """HTTP 429 - back off for the advertised interval."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        """Keep the Retry-After hint."""
        super().__init__(message)
        self.retry_after = retry_after


class HemResponseError(HemError):
    """Any other non-2xx response."""


class HomeEnergyManagerApi:
    """Minimal client for one HEM authenticated API server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        api_key: str,
        use_ssl: bool = False,
    ) -> None:
        """Store connection details."""
        self._session = session
        self._api_key = api_key
        scheme = "https" if use_ssl else "http"
        self.base_url = f"{scheme}://{host}:{port}"

    @staticmethod
    def new_idempotency_key() -> str:
        """Return a fresh key for a NEW command (36 chars, within HEM's 16-128)."""
        return str(uuid.uuid4())

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Perform one request and translate HTTP failures into HemError."""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        try:
            response = await self._session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json,
                timeout=REQUEST_TIMEOUT,
            )
            # HEM always answers JSON, including on errors, but be defensive.
            try:
                payload = await response.json(content_type=None)
            except ValueError:
                payload = {"error": (await response.text())[:200]}
        except TimeoutError as err:
            raise HemConnectionError(
                f"Timeout talking to {self.base_url}{path}"
            ) from err
        except ClientError as err:
            raise HemConnectionError(
                f"Cannot reach {self.base_url}{path}: {err}"
            ) from err

        if not isinstance(payload, dict):
            payload = {"data": payload}

        status = response.status
        message = str(
            payload.get("error") or payload.get("message") or f"HTTP {status}"
        )

        if status == 401:
            raise HemAuthError(message)
        if status == 403:
            raise HemPermissionError(message)
        if status == 404:
            raise HemNotFoundError(message)
        if status == 409:
            raise HemConflictError(message, payload.get("command_id"))
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            raise HemRateLimitError(
                message,
                int(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if status >= 400:
            raise HemResponseError(f"HTTP {status}: {message}")

        return payload

    # --- reads ---------------------------------------------------------------

    async def async_get_status(self) -> dict[str, Any]:
        """Return the cached battery operating summary."""
        return await self._request("GET", "/api/control/status")

    async def async_get_snapshot(self) -> dict[str, Any]:
        """Return the limited live measurement projection."""
        return await self._request("GET", "/api/snapshot")

    async def async_get_command(self, command_id: str) -> dict[str, Any]:
        """Return the lifecycle record for a previously submitted command."""
        return await self._request("GET", f"/api/commands/{command_id}")

    # --- mutations -----------------------------------------------------------

    async def async_force_charge(
        self, minutes: int, idempotency_key: str
    ) -> dict[str, Any]:
        """Start Force Charge for the given whole number of minutes."""
        return await self._request(
            "POST",
            "/api/control/force-charge",
            json={"minutes": int(minutes)},
            idempotency_key=idempotency_key,
        )

    async def async_stop_force_charge(self, idempotency_key: str) -> dict[str, Any]:
        """Stop Force Charge. Allowed as recovery even if control was revoked."""
        return await self._request(
            "POST", "/api/control/force-charge/stop", idempotency_key=idempotency_key
        )

    async def async_force_discharge(
        self, minutes: int, idempotency_key: str
    ) -> dict[str, Any]:
        """Start Force Discharge (a forced export) for the given minutes."""
        return await self._request(
            "POST",
            "/api/control/force-discharge",
            json={"minutes": int(minutes)},
            idempotency_key=idempotency_key,
        )

    async def async_stop_force_discharge(self, idempotency_key: str) -> dict[str, Any]:
        """Stop Force Discharge."""
        return await self._request(
            "POST", "/api/control/force-discharge/stop", idempotency_key=idempotency_key
        )
