"""DataUpdateCoordinator: polls REST cook-history and tracks latest probe temps.

Robust by design: stateless REST polling (coexists with the phone app), discovers
the active session each cycle (handles new cooks), and pages forward by after_id.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WeberAuthError, WeberCloud, WeberError
from .const import (
    DEFAULT_AUTO_OFF_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_PROBES,
    STALE_GRACE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

DISCONNECTED = "disconnected"
CONNECTED = "connected"

# Hub-level connection/data status (most informative -> least):
#   streaming = companion websocket delivered live frames this cycle
#   polling   = REST cook-history returned NEW snapshots this cycle (temps advancing)
#   stale     = a cook session exists but no new data arrived (hub paused its cloud
#               push) — temperatures are frozen at their last value
#   offline   = no active cook session (hub isn't pushing a cook to the cloud at all)
#   off       = monitoring switch is off (we aren't polling at all)
STREAMING = "streaming"
POLLING = "polling"
STALE = "stale"
OFFLINE = "offline"
OFF = "off"
CONNECTION_OPTIONS = [STREAMING, POLLING, STALE, OFFLINE, OFF]
# probes are shown only when the connection is genuinely live/accurate
LIVE_STATES = (STREAMING, POLLING)

# The companion websocket frame only ever arrives alongside a REST burst, and a
# read with no frame blocks ~5 s. So skip the ws read on quiet polls and only do it
# when REST is fresh (a burst is happening) — plus a periodic safety probe so we'd
# still notice ws-only activity. Saves ~5 s on every idle poll.
WS_PROBE_EVERY = 6


class WeberCoordinator(DataUpdateCoordinator):
    """Coordinates polling for one appliance (hub).

    Polling is gated by a monitoring switch with an auto-off timer: it does nothing
    until enabled, runs for `auto_off_minutes`, then auto-disables. While disabled
    the connection reads "off" and all probe entities are unavailable.
    """

    def __init__(self, hass: HomeAssistant, api: WeberCloud, appliance: dict):
        # start with no update_interval -> no polling until the switch enables it
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}:{appliance['id']}", update_interval=None)
        self.api = api
        self.appliance = appliance
        self._session: str | None = None
        self._after_id = 0
        self._temps: dict[int, int] = {}  # probe index -> raw deci-Celsius
        # probe index -> doneness ("idle"/"cooking"/"done"/"disconnected"); from ws
        self._states: dict[int, str] = {i: DISCONNECTED for i in range(MAX_PROBES)}
        # monitoring gate
        self._enabled = False
        self._auto_off_minutes = DEFAULT_AUTO_OFF_MINUTES
        self._expiry: float | None = None  # epoch seconds; auto-off deadline
        # freshness tracking (epoch seconds of last observed data per transport)
        self._last_rest_ts: float = 0.0
        self._last_ws_ts: float = 0.0
        self._poll_count = 0

    # ------------------------------------------------------------ switch/timer
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def auto_off_minutes(self) -> int:
        return self._auto_off_minutes

    @property
    def expires_at(self) -> float | None:
        return self._expiry

    async def async_enable(self) -> None:
        """Turn monitoring on, arm the auto-off timer, and start polling."""
        self._enabled = True
        self._expiry = time.time() + self._auto_off_minutes * 60
        # clear freshness so we don't inherit a "live" state from a previous run
        self._last_rest_ts = 0.0
        self._last_ws_ts = 0.0
        self.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL)
        await self.async_request_refresh()

    def _disable_internal(self) -> None:
        self._enabled = False
        self._expiry = None
        self.update_interval = None  # stop the scheduler

    async def async_disable(self) -> None:
        """Turn monitoring off, stop polling, and mark probes unavailable."""
        self._disable_internal()
        self.async_set_updated_data(self._disabled_data())

    async def async_set_auto_off(self, minutes: int) -> None:
        """Update the auto-off duration; if running, reset the timer from now."""
        self._auto_off_minutes = int(minutes)
        if self._enabled:
            self._expiry = time.time() + self._auto_off_minutes * 60
        # push so the number entity (and remaining-time attribute) refresh
        self.async_set_updated_data(self.data or self._disabled_data())

    def _disabled_data(self) -> dict:
        return {
            "online": False, "session": None, "probes": {}, "states": {},
            "connection": OFF, "live": False, "enabled": False,
            "rest": "off", "websocket": "off",
            "new_snapshots": 0, "last_snapshot_id": self._after_id,
        }

    # ----------------------------------------------------------------- polling
    async def _async_update_data(self) -> dict:
        if not self._enabled:
            return self._disabled_data()
        # auto-off when the timer expires (checked in the loop, safe to mutate here)
        if self._expiry is not None and time.time() >= self._expiry:
            _LOGGER.debug("monitoring auto-off timer expired; disabling")
            self._disable_internal()
            return self._disabled_data()
        try:
            return await self.hass.async_add_executor_job(self._poll)
        except WeberAuthError as e:
            # creds/pairing problem -> surfaces as a repair-able auth error in HA
            raise UpdateFailed(f"auth/pairing error: {e}") from e
        except WeberError as e:
            raise UpdateFailed(str(e)) from e

    def _poll(self) -> dict:
        appliance_id = self.appliance["id"]
        self._poll_count += 1
        t_start = time.time()

        # (re)discover the active session; reset if a new cook started
        session = self.api.latest_session_id(appliance_id)
        if session != self._session:
            _LOGGER.debug("session changed %s -> %s; resetting paging",
                          self._session, session)
            self._session = session
            self._after_id = 0
            self._temps = {}
        if not session:
            # no active cook -> hub isn't pushing anything to the cloud
            self._states = {i: DISCONNECTED for i in range(MAX_PROBES)}
            _LOGGER.debug("poll #%d: no active session -> offline (%.2fs)",
                          self._poll_count, time.time() - t_start)
            return {
                "online": False, "session": None, "probes": {},
                "states": dict(self._states), "connection": OFFLINE, "live": False,
                "enabled": True, "rest": "no session", "websocket": "idle",
                "new_snapshots": 0, "last_snapshot_id": self._after_id,
                "rest_age_s": None, "ws_age_s": None, "poll_seconds": None,
            }

        t_rest = time.time()
        snaps, max_id = self.api.get_snapshots(appliance_id, session, self._after_id)
        rest_seconds = time.time() - t_rest
        fresh = bool(snaps)  # did the hub write any NEW snapshots since last poll?
        for s in snaps:
            for p in s.get("data", {}).get("probe_status", []):
                idx = p.get("index")
                if idx is not None:
                    self._temps[int(idx)] = p.get("temperature")
            self._after_id = max(self._after_id, s.get("snapshot_id", self._after_id))

        # Connectivity is derived from the RELIABLE REST temperature feed: a probe
        # reporting a (non-zero) temperature is connected, full stop. The companion
        # websocket — when it actually streams — only REFINES a connected probe to
        # idle/cooking/done. Many hubs never maintain a companion ws session (it
        # returns None/empty), so we must NOT let an absent ws frame mark a probe
        # that is plainly reading a temperature as "disconnected".
        # only pay the ~5 s websocket read when a burst is happening (fresh REST) or
        # on a periodic safety probe; otherwise skip it so quiet polls are fast.
        do_ws = fresh or (self._poll_count % WS_PROBE_EVERY == 0)
        ws = None
        ws_seconds = 0.0
        if do_ws:
            t_ws = time.time()
            try:
                ws = self.api.companion_status(appliance_id, seconds=4.0)
            except WeberError as e:
                _LOGGER.debug("companion websocket unavailable: %s", e)
                ws = None
            ws_seconds = time.time() - t_ws
        ws_ok = bool(ws)
        states = {}
        for i in range(MAX_PROBES):
            if ws is not None and i in ws:
                states[i] = ws[i]["state"]            # idle / cooking / done
            elif self._temps.get(i) not in (None, 0):
                states[i] = CONNECTED                  # reading temp, doneness unknown
            else:
                states[i] = DISCONNECTED               # no reading -> unplugged
        self._states = states

        # Record per-transport freshness, then derive the connection state from a
        # GRACE WINDOW rather than this single poll. Both transports arrive in
        # bursts, so requiring data every 10 s poll produced streaming/polling/stale
        # flapping (and flapped the probes unavailable). Now: streaming if the ws was
        # seen recently, else polling if REST was seen recently, else stale.
        now = time.time()
        if fresh:
            self._last_rest_ts = now
        if ws_ok:
            self._last_ws_ts = now
        ws_recent = self._last_ws_ts and (now - self._last_ws_ts) <= STALE_GRACE_SECONDS
        rest_recent = self._last_rest_ts and (now - self._last_rest_ts) <= STALE_GRACE_SECONDS
        if ws_recent:
            connection = STREAMING
        elif rest_recent:
            connection = POLLING
        else:
            connection = STALE                         # no data within the grace window

        rest_age = round(now - self._last_rest_ts, 1) if self._last_rest_ts else None
        ws_age = round(now - self._last_ws_ts, 1) if self._last_ws_ts else None
        poll_seconds = round(now - t_start, 2)
        _LOGGER.debug(
            "poll #%d: session=%s new_snaps=%d after_id=%d ws_tried=%s ws_frame=%s "
            "probes_in_ws=%d rest_age=%ss ws_age=%ss rest_t=%.2fs ws_t=%.2fs "
            "total=%.2fs -> %s",
            self._poll_count, (session or "")[:8], len(snaps), self._after_id,
            do_ws, ws_ok, len(ws or {}), rest_age, ws_age, rest_seconds, ws_seconds,
            poll_seconds, connection,
        )

        return {
            "online": True,
            "session": session,
            # only channels actually reporting a (non-zero) reading are "present"
            "probes": dict(self._temps),
            "states": dict(self._states),
            "connection": connection,
            "live": connection in LIVE_STATES,
            "enabled": True,
            "rest": "fresh" if fresh else "stale",
            "websocket": "streaming" if ws_ok else "idle",
            "new_snapshots": len(snaps),
            "last_snapshot_id": self._after_id,
            "rest_age_s": rest_age,
            "ws_age_s": ws_age,
            "poll_seconds": poll_seconds,
        }
