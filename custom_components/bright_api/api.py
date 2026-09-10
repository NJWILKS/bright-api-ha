"""Minimal async client for the Hildebrand Glowmarkt API."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .const import API_BASE, APP_ID

UK_TZ = ZoneInfo("Europe/London")


class BrightAuthError(Exception):
    """Authentication failed."""


class BrightApiError(Exception):
    """The Bright API returned an unexpected response."""


class BrightApiClient:
    """Small async Glowmarkt client with no Home Assistant state."""

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._username = username
        self._password = password
        self._session = session
        self._token: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        headers = {"applicationId": APP_ID, "Content-Type": "application/json"}
        if self._token:
            headers["token"] = self._token
        return headers

    async def authenticate(self) -> None:
        async with self._session.post(
            f"{API_BASE}/auth",
            headers={"applicationId": APP_ID, "Content-Type": "application/json"},
            json={"username": self._username, "password": self._password},
        ) as response:
            if response.status == 401:
                raise BrightAuthError("Invalid Bright username or password")
            if response.status >= 400:
                raise BrightApiError(f"Authentication failed with HTTP {response.status}")
            payload = await response.json()

        token = payload.get("token") if isinstance(payload, dict) else None
        if not token or not payload.get("valid"):
            raise BrightAuthError("Bright authentication returned an invalid response")
        self._token = str(token)

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> Any | None:
        if self._token is None:
            await self.authenticate()

        async with self._session.get(
            f"{API_BASE}{path}",
            headers=self.headers,
            params=params,
        ) as response:
            if response.status == 401:
                raise BrightAuthError("Bright session is no longer authorised")
            if response.status == 404 and allow_not_found:
                return None
            if response.status >= 400:
                raise BrightApiError(f"Bright request failed with HTTP {response.status}")
            return await response.json()

    async def get_virtual_entities(self) -> list[dict[str, Any]]:
        payload = await self._get_json("/virtualentity")
        return payload if isinstance(payload, list) else []

    async def discover_resources(self, virtual_entity_id: str) -> dict[str, dict[str, Any]]:
        payload = await self._get_json(f"/virtualentity/{virtual_entity_id}/resources")
        if not isinstance(payload, dict):
            raise BrightApiError("Bright resource discovery returned an invalid response")

        resources: dict[str, dict[str, Any]] = {}
        for item in payload.get("resources", []):
            if not isinstance(item, dict):
                continue
            classifier = item.get("classifier")
            resource_id = item.get("resourceId")
            if not classifier or not resource_id:
                continue
            resources[str(classifier)] = {
                "resource_id": str(resource_id),
                "name": str(item.get("name") or classifier),
                "base_unit": str(item.get("baseUnit") or ""),
            }
        return resources

    async def get_readings(
        self,
        resource_id: str,
        start: datetime,
        end: datetime,
        *,
        period: str = "PT30M",
    ) -> list[tuple[datetime, float | None]]:
        params = {
            "from": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "to": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "period": period,
            "offset": 0,
            "function": "sum",
            "nulls": 1,
        }
        payload = await self._get_json(f"/resource/{resource_id}/readings", params=params)
        if not isinstance(payload, dict) or payload.get("status") != "OK":
            raise BrightApiError("Bright readings request returned an invalid response")

        rows: list[tuple[datetime, float | None]] = []
        for item in payload.get("data", []):
            if not isinstance(item, list) or len(item) < 2:
                continue
            timestamp = datetime.fromtimestamp(float(item[0]), tz=UTC)
            value = None if item[1] is None else float(item[1])
            rows.append((timestamp, value))
        return rows

    async def get_first_reading_time(self, resource_id: str) -> datetime | None:
        payload = await self._get_json(
            f"/resource/{resource_id}/first-time",
            allow_not_found=True,
        )
        if payload is None:
            return None
        if not isinstance(payload, dict) or payload.get("status") not in (None, "OK"):
            raise BrightApiError("Bright first-time request returned an invalid response")
        first_ts = (payload.get("data") or {}).get("firstTs")
        if first_ts is None:
            return None
        return datetime.fromtimestamp(float(first_ts), tz=UTC)

    async def get_first_available_reading_time(self, resource_id: str) -> datetime | None:
        """Use first-time only as a locator, then find the first real PT30M row."""
        locator = await self.get_first_reading_time(resource_id)
        if locator is None:
            return None

        local_start = locator.astimezone(UK_TZ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        rows = await self.get_readings(
            resource_id,
            local_start,
            local_start + timedelta(days=3),
        )
        available = [timestamp for timestamp, value in rows if value is not None]
        return min(available) if available else None

    async def get_tariffs(self, resource_id: str) -> list[dict[str, Any]]:
        payload = await self._get_json(f"/resource/{resource_id}/tariff-list")
        if not isinstance(payload, dict) or payload.get("status") not in (None, "OK"):
            raise BrightApiError("Bright tariff request returned an invalid response")
        rows = payload.get("data", [])
        return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
