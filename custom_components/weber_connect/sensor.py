"""Per-probe temperature sensors (REST cook-history)."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MAX_PROBES
from .coordinator import CONNECTION_OPTIONS, WeberCoordinator


def _device_info(appliance: dict) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, appliance["id"])},
        name=appliance.get("name") or "Weber Connect Hub",
        manufacturer="Weber",
        model=appliance.get("model") or "Connect Smart Grilling Hub",
        serial_number=appliance.get("serial"),
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WeberCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [WeberConnectionSensor(coordinator)]
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
        self._attr_device_info = _device_info(appliance)

    @property
    def _raw(self):
        probes = (self.coordinator.data or {}).get("probes", {})
        return probes.get(self._index)

    @property
    def available(self) -> bool:
        # available only while the hub connection is live/accurate (streaming or
        # polling) AND this channel is reporting a real reading. When monitoring is
        # off, or data is stale/offline, the probe is treated as disconnected.
        if not (self.coordinator.data or {}).get("live"):
            return False
        raw = self._raw
        return bool(super().available) and raw not in (None, 0)

    @property
    def native_value(self):
        raw = self._raw
        if raw in (None, 0):
            return None
        return round(raw / 10.0, 1)  # deci-Celsius -> Celsius (HA converts to user unit)


DONENESS_OPTIONS = ["disconnected", "connected", "idle", "cooking", "done"]


class WeberProbeDonenessSensor(CoordinatorEntity[WeberCoordinator], SensorEntity):
    """Doneness/connection state of one probe channel, as a dropdown (enum).

    States: disconnected (probe unplugged / not reading), connected (reading a
    temperature; doneness unknown because the companion websocket isn't streaming),
    idle (plugged, no target), cooking (below target), done (at/above target).
    idle/cooking/done are only available when the hub maintains a companion
    websocket session; otherwise a reading probe shows "connected".
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = DONENESS_OPTIONS
    _attr_icon = "mdi:grill"

    def __init__(self, coordinator: WeberCoordinator, index: int):
        super().__init__(coordinator)
        self._index = index
        appliance = coordinator.appliance
        self._attr_unique_id = f"{appliance['id']}_probe{index}_state"
        self._attr_name = f"Probe {index + 1} status"
        self._attr_device_info = _device_info(appliance)

    @property
    def available(self) -> bool:
        # mirror the temperature sensor: only meaningful while the hub is live
        return bool(super().available) and bool((self.coordinator.data or {}).get("live"))

    @property
    def native_value(self):
        states = (self.coordinator.data or {}).get("states", {})
        val = states.get(self._index)
        return val if val in DONENESS_OPTIONS else None


class WeberConnectionSensor(CoordinatorEntity[WeberCoordinator], SensorEntity):
    """Hub-level connection/data status (one per hub), as a dropdown (enum).

    streaming = companion websocket delivering live frames; polling = REST
    cook-history returning new snapshots (temps advancing); stale = a cook session
    exists but the hub paused its cloud push (temps frozen); offline = no active
    cook session. Attributes break out the REST and websocket transports separately.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONNECTION_OPTIONS
    _attr_icon = "mdi:grill"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WeberCoordinator):
        super().__init__(coordinator)
        appliance = coordinator.appliance
        self._attr_unique_id = f"{appliance['id']}_connection"
        self._attr_name = "Connection"
        self._attr_device_info = _device_info(appliance)

    @property
    def native_value(self):
        val = (self.coordinator.data or {}).get("connection")
        return val if val in CONNECTION_OPTIONS else None

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        return {
            "rest": d.get("rest"),
            "websocket": d.get("websocket"),
            "session": d.get("session"),
            "new_snapshots_last_poll": d.get("new_snapshots"),
            "last_snapshot_id": d.get("last_snapshot_id"),
            "rest_age_seconds": d.get("rest_age_s"),
            "websocket_age_seconds": d.get("ws_age_s"),
            "last_poll_seconds": d.get("poll_seconds"),
        }
