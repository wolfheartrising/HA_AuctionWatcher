"""Sensors: one per followed auction, plus account totals."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AuctionWatcherConfigEntry
from .const import DOMAIN
from .coordinator import AuctionWatcherCoordinator
from .sites.base import STATUS_LIVE


def _device(coord: AuctionWatcherCoordinator) -> DeviceInfo:
    e = coord.config_entry
    return DeviceInfo(
        identifiers={(DOMAIN, e.entry_id)},
        name=f"Auction Watcher {e.title}",
        manufacturer=e.title,
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: AuctionWatcherConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coord = entry.runtime_data
    async_add_entities([PointsSensor(coord), OpportunitiesSensor(coord), FollowingSensor(coord)])

    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [aid for aid in (coord.data or {}) if aid not in known]
        if new:
            known.update(new)
            async_add_entities(AuctionSensor(coord, aid) for aid in new)

    _add_new()
    entry.async_on_unload(coord.async_add_listener(_add_new))


class _Base(CoordinatorEntity[AuctionWatcherCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord: AuctionWatcherCoordinator, key: str) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.config_entry.entry_id}_{key}"
        self._attr_device_info = _device(coord)


class PointsSensor(_Base):
    _attr_name = "Points balance"
    _attr_icon = "mdi:wallet"
    _attr_native_unit_of_measurement = "points"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: AuctionWatcherCoordinator) -> None:
        super().__init__(coord, "points")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.points

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        interval = self.coordinator.update_interval
        return {
            "polling_mode": self.coordinator.mode,
            "poll_every_seconds": int(interval.total_seconds()) if interval else None,
        }


class OpportunitiesSensor(_Base):
    _attr_name = "Opportunities"
    _attr_icon = "mdi:target"

    def __init__(self, coord: AuctionWatcherCoordinator) -> None:
        super().__init__(coord, "opportunities")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.opportunities)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"auction_ids": self.coordinator.opportunities}


class FollowingSensor(_Base):
    _attr_name = "Live auctions followed"
    _attr_icon = "mdi:binoculars"

    def __init__(self, coord: AuctionWatcherCoordinator) -> None:
        super().__init__(coord, "following")

    @property
    def native_value(self) -> int:
        return sum(1 for s in (self.coordinator.data or {}).values() if s.status == STATUS_LIVE)


class AuctionSensor(_Base):
    """State = current price. Attributes hold everything else."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "USD"
    _attr_icon = "mdi:gavel"

    def __init__(self, coord: AuctionWatcherCoordinator, auction_id: str) -> None:
        super().__init__(coord, f"auction_{auction_id}")
        self.auction_id = auction_id
        s = coord.data[auction_id]
        self._attr_name = s.title
        self._attr_entity_picture = s.image

    @property
    def available(self) -> bool:
        return super().available and self.auction_id in (self.coordinator.data or {})

    @property
    def native_value(self) -> float | None:
        s = (self.coordinator.data or {}).get(self.auction_id)
        return s.price if s else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = (self.coordinator.data or {}).get(self.auction_id)
        if not s:
            return {}
        attrs = s.as_attributes()
        limit = self.coordinator.bid_limit(self.auction_id)
        attrs["bid_limit"] = limit
        attrs["bid_limit_reached"] = bool(limit and s.your_bids >= limit)
        attrs["bids_left_under_limit"] = max(limit - s.your_bids, 0) if limit else None
        attrs["opportunity"] = self.auction_id in self.coordinator.opportunities
        attrs["auction_watcher"] = True  # lets dashboards find these entities
        return attrs
