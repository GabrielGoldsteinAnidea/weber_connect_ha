"""Per-probe temperature sensors (REST cook-history)."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MAX_PROBES
from .coordinator import WeberCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WeberCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for i in range(MAX_PROBES):
        entities.append(WeberProbeSensor(coordinator, i))
        entities.append(WeberProbeDonenessSensor(coordinator, i))
    async_add_entities(entities)


class WeberProbeSensor(CoordinatorEntity[WeberCoordinator], SensorEntity):
    """Temperature of one probe channel. Raw cloud value is deci-Celsius."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: WeberCoordinator, index: int):
        super().__init__(coordinator)
        self._index = index
        appliance = coordinator.appliance
        self._attr_unique_id = f"{appliance['id']}_probe{index}"
        self._attr_name = f"Probe {index + 1}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, appliance["id"])},
            name=appliance.get("name") or "Weber Connect Hub",
            manufacturer="Weber",
            model=appliance.get("model") or "Connect Smart Grilling Hub",
            serial_number=appliance.get("serial"),
        )

    @property
    def _raw(self):
        probes = (self.coordinator.data or {}).get("probes", {})
        return probes.get(self._index)

    @property
    def available(self) -> bool:
        # available only while connected AND this channel is reporting a real reading
        raw = self._raw
        return bool(super().available) and raw not in (None, 0)

    @property
    def native_value(self):
        raw = self._raw
        if raw in (None, 0):
            return None
        return round(raw / 10.0, 1)  # deci-Celsius -> Celsius (HA converts to user unit)


DONENESS_OPTIONS = ["disconnected", "idle", "cooking", "done", "unknown"]


class WeberProbeDonenessSensor(CoordinatorEntity[WeberCoordinator], SensorEntity):
    """Doneness/connection state of one probe channel, as a dropdown (enum).

    States: disconnected (probe unplugged / no cook), idle (plugged, no target),
    cooking (below target), done (at/above target), unknown (state not yet read).
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = DONENESS_OPTIONS

    def __init__(self, coordinator: WeberCoordinator, index: int):
        super().__init__(coordinator)
        self._index = index
        appliance = coordinator.appliance
        self._attr_unique_id = f"{appliance['id']}_probe{index}_state"
        self._attr_name = f"Probe {index + 1} status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, appliance["id"])},
            name=appliance.get("name") or "Weber Connect Hub",
            manufacturer="Weber",
            model=appliance.get("model") or "Connect Smart Grilling Hub",
            serial_number=appliance.get("serial"),
        )

    @property
    def native_value(self):
        states = (self.coordinator.data or {}).get("states", {})
        val = states.get(self._index)
        if val not in DONENESS_OPTIONS:
            return "unknown"
        return val
