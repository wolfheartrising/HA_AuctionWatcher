"""Auto-booking on/off: one switch per GovAuctions account (usable in automations and by voice)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AuctionWatcherConfigEntry
from .coordinator import AuctionWatcherCoordinator
from .sensor import _device


async def async_setup_entry(
    hass: HomeAssistant, entry: AuctionWatcherConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    if entry.data.get("site", "govauctions") in ("govauctions", "macbid"):
        async_add_entities([AutoBookingSwitch(entry.runtime_data)])


class AutoBookingSwitch(CoordinatorEntity[AuctionWatcherCoordinator], SwitchEntity):
    """Off = no automatic booking anywhere (auto-cancel still works, so nothing keeps spending)."""

    _attr_has_entity_name = True
    @property
    def name(self) -> str:
        return "Auto-bidding" if self.coordinator.config_entry.data.get("site") == "macbid" else "Auto-booking"
    _attr_icon = "mdi:robot"

    def __init__(self, coord: AuctionWatcherCoordinator) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.config_entry.entry_id}_auto_booking"
        self._attr_device_info = _device(coord)

    @property
    def is_on(self) -> bool:
        return not self.coordinator.auto_paused

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"auction_watcher_autoswitch": True, "site": self.coordinator.config_entry.data.get("site", "govauctions")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_auto_paused(False)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_auto_paused(True)
