"""Monitoring switch: turns cloud polling on (with an auto-off timer) or off."""
from __future__ import annotations

import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WeberCoordinator
from .sensor import _device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WeberCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WeberMonitorSwitch(coordinator)])


class WeberMonitorSwitch(CoordinatorEntity[WeberCoordinator], SwitchEntity):
    """When on, the integration polls the Weber cloud until the auto-off timer fires."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:grill"

    def __init__(self, coordinator: WeberCoordinator):
        super().__init__(coordinator)
        appliance = coordinator.appliance
        self._attr_unique_id = f"{appliance['id']}_monitoring"
        self._attr_name = "Monitoring"
        self._attr_device_info = _device_info(appliance)

    @property
    def available(self) -> bool:
        return True  # the control itself is always usable

    @property
    def is_on(self) -> bool:
        return self.coordinator.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        exp = self.coordinator.expires_at
        remaining = max(0, int(exp - time.time())) if exp else None
        return {
            "auto_off_minutes": self.coordinator.auto_off_minutes,
            "minutes_remaining": round(remaining / 60, 1) if remaining is not None else None,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_enable()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_disable()
