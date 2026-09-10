"""Current-value coordinator for Bright API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BrightApiClient, BrightApiError, BrightAuthError, UK_TZ
from .const import CONF_VIRTUAL_ENTITY_ID, DOMAIN, SUPPORTED_CLASSIFIERS


class BrightDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll today's PT30M values without owning historical state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: BrightApiClient,
    ) -> None:
        super().__init__(
            hass,
            logger=__import__("logging").getLogger(__name__),
            name=DOMAIN,
            update_interval=timedelta(minutes=15),
        )
        self.entry = entry
        self.client = client
        self.resources: dict[str, dict[str, Any]] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if not self.resources:
                self.resources = await self.client.discover_resources(
                    self.entry.data[CONF_VIRTUAL_ENTITY_ID]
                )

            now = datetime.now(timezone.utc)
            local_start = now.astimezone(UK_TZ).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

            values: dict[str, float | None] = {}
            for classifier in SUPPORTED_CLASSIFIERS:
                resource = self.resources.get(classifier)
                if resource is None:
                    continue
                rows = await self.client.get_readings(
                    resource["resource_id"],
                    local_start,
                    now,
                )
                present = [value for _, value in rows if value is not None]
                values[classifier] = sum(present) if present else None

            return {"values": values, "resources": self.resources}
        except (BrightApiError, BrightAuthError) as err:
            raise UpdateFailed(str(err)) from err
