"""Auto-off duration: how long monitoring stays on before it disables itself."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MAX_AUTO_OFF_MINUTES, MIN_AUTO_OFF_MINUTES
from .coordinator import WeberCoordinator
from .sensor import _device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WeberCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WeberAutoOffNumber(coordinator)])


class WeberAutoOffNumber(CoordinatorEntity[WeberCoordinator], NumberEntity, RestoreEntity):
    """User-set duration (minutes) the monitoring switch stays on before auto-off."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MIN_AUTO_OFF_MINUTES
    _attr_native_max_value = MAX_AUTO_OFF_MINUTES
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: WeberCoordinator):
        super().__init__(coordinator)
        appliance = coordinator.appliance
        self._attr_unique_id = f"{appliance['id']}_auto_off"
        self._attr_name = "Auto-off"
        self._attr_device_info = _device_info(appliance)

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        """Restore the last-set duration across restarts."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            try:
                await self.coordinator.async_set_auto_off(int(float(last.state)))
            except (ValueError, TypeError):
                pass

    @property
    def native_value(self) -> float:
        return self.coordinator.auto_off_minutes

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_auto_off(int(value))
