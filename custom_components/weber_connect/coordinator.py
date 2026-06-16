"""DataUpdateCoordinator: polls REST cook-history and tracks latest probe temps.

Robust by design: stateless REST polling (coexists with the phone app), discovers
the active session each cycle (handles new cooks), and pages forward by after_id.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WeberAuthError, WeberCloud, WeberError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, MAX_PROBES

_LOGGER = logging.getLogger(__name__)

DISCONNECTED = "disconnected"


class WeberCoordinator(DataUpdateCoordinator):
    """Coordinates polling for one appliance (hub)."""

    def __init__(self, hass: HomeAssistant, api: WeberCloud, appliance: dict):
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}:{appliance['id']}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api
        self.appliance = appliance
        self._session: str | None = None
        self._after_id = 0
        self._temps: dict[int, int] = {}  # probe index -> raw deci-Celsius
        # probe index -> doneness ("idle"/"cooking"/"done"/"disconnected"); from ws
        self._states: dict[int, str] = {i: DISCONNECTED for i in range(MAX_PROBES)}

    async def _async_update_data(self) -> dict:
        try:
            return await self.hass.async_add_executor_job(self._poll)
        except WeberAuthError as e:
            # creds/pairing problem -> surfaces as a repair-able auth error in HA
            raise UpdateFailed(f"auth/pairing error: {e}") from e
        except WeberError as e:
            raise UpdateFailed(str(e)) from e

    def _poll(self) -> dict:
        appliance_id = self.appliance["id"]
        # (re)discover the active session; reset if a new cook started
        session = self.api.latest_session_id(appliance_id)
        if session != self._session:
            self._session = session
            self._after_id = 0
            self._temps = {}
        if not session:
            # no active cook -> nothing connected
            self._states = {i: DISCONNECTED for i in range(MAX_PROBES)}
            return {"online": False, "session": None, "probes": {}, "states": dict(self._states)}

        snaps, max_id = self.api.get_snapshots(appliance_id, session, self._after_id)
        for s in snaps:
            for p in s.get("data", {}).get("probe_status", []):
                idx = p.get("index")
                if idx is not None:
                    self._temps[int(idx)] = p.get("temperature")
            self._after_id = max(self._after_id, s.get("snapshot_id", self._after_id))

        # Doneness/connection state comes from the companion websocket. It's a
        # single-holder channel shared with the phone app, so it may be contended
        # (returns None) — in that case we keep the last-known states rather than
        # flapping. When we DO get a frame, any probe absent from it is disconnected.
        # Deliberate trade-off: this opens a fresh TLS websocket every poll so the
        # coordinator stays stateless and coexists with the phone app; the per-poll
        # connect cost is acceptable at the DEFAULT_SCAN_INTERVAL cadence.
        try:
            ws = self.api.companion_status(appliance_id, seconds=4.0)
        except WeberError as e:
            _LOGGER.debug("companion websocket unavailable: %s", e)
            ws = None
        if ws is not None:
            self._states = {
                i: (ws[i]["state"] if i in ws else DISCONNECTED)
                for i in range(MAX_PROBES)
            }

        return {
            "online": True,
            "session": session,
            # only channels actually reporting a (non-zero) reading are "present"
            "probes": dict(self._temps),
            "states": dict(self._states),
        }
