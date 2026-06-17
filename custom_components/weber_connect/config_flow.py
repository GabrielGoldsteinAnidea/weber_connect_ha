"""Config flow: collect the companion device_id + device_password, validate by
logging in and discovering the paired appliance."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .api import WeberAuthError, WeberCloud, WeberError
from .const import CONF_DEVICE_ID, CONF_DEVICE_PASSWORD, DOMAIN

STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_DEVICE_ID): str,
    vol.Required(CONF_DEVICE_PASSWORD): str,
})


class WeberConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Weber Connect cloud config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID].strip()
            password = user_input[CONF_DEVICE_PASSWORD].strip()

            def _validate():
                api = WeberCloud(device_id, password)
                api.authenticate()                 # raises WeberAuthError on bad creds
                return api.discover_appliances()    # [] if nothing paired

            try:
                appliances = await self.hass.async_add_executor_job(_validate)
            except WeberAuthError:
                errors["base"] = "invalid_auth"
            except WeberError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                if not appliances:
                    errors["base"] = "no_appliances"
                else:
                    await self.async_set_unique_id(device_id)
                    self._abort_if_unique_id_configured()
                    name = appliances[0].get("name") or "Weber Connect"
                    return self.async_create_entry(
                        title=name,
                        data={CONF_DEVICE_ID: device_id, CONF_DEVICE_PASSWORD: password},
                    )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors,
            description_placeholders={
                "how": "Find the App Identifier in the Weber Connect app under "
                       "Settings. The Device password is not shown there — recover "
                       "it from a decrypted app capture (see the project docs).",
            },
        )
