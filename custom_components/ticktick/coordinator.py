"""DataUpdateCoordinator for the TickTick integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class TickTickDataUpdateCoordinator(DataUpdateCoordinator[dict]):
    """A TickTick Data Update Coordinator."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, ticktick_client) -> None:
        """Initialize the TickTick data coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=1),
        )
        self.ticktick_client = ticktick_client

    async def _async_update_data(self) -> dict:
        try:
            await self.hass.async_add_executor_job(self.ticktick_client.sync)
            data = {
                "projects": self.ticktick_client.state["projects"],
                "tasks": self.ticktick_client.task.get_from_project("5dad62dff0fe1fc4fbea252b"),
            }
            return data
        except Exception as e:
            raise UpdateFailed(f"Error updating data from TickTick: {e}") from e
