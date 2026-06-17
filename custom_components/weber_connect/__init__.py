"""Weber Connect (cloud) integration for Home Assistant."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import WeberAuthError, WeberCloud, WeberError
from .const import CONF_DEVICE_ID, CONF_DEVICE_PASSWORD, DOMAIN
from .coordinator import WeberCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH, Platform.NUMBER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api = WeberCloud(entry.data[CONF_DEVICE_ID], entry.data[CONF_DEVICE_PASSWORD])

    def _connect():
        api.authenticate()
        appliances = api.discover_appliances()
        return appliances

    try:
        appliances = await hass.async_add_executor_job(_connect)
    except WeberAuthError as e:
        raise ConfigEntryAuthFailed(str(e)) from e
    except WeberError as e:
        raise ConfigEntryNotReady(str(e)) from e

    if not appliances:
        raise ConfigEntryNotReady("no appliance paired to this companion device yet")

    appliance = appliances[0]
    coordinator = WeberCoordinator(hass, api, appliance)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
