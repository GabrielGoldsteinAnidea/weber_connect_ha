"""Constants for the Weber Connect (cloud) integration."""

DOMAIN = "weber_connect"

CONF_DEVICE_ID = "device_id"
CONF_DEVICE_PASSWORD = "device_password"

# App-global OAuth client credentials baked into the Weber Connect app — identical
# for every user (verified as hardcoded string literals in the APK's classes3.dex),
# NOT a personal secret. Required so the companion-device login can authenticate,
# exactly like every app install carries them. The APP_ prefix marks them as the
# application's identity, distinct from the user's device_id/device_password.
APP_CLIENT_ID = "qyw4CGeb/i93BrA0KAUuGtPyKImr+nUKc8lHxFdt"
APP_CLIENT_SECRET = "ekEHLyHw+Ru3H25mH4a9f2OKCMILnMx+YSN2dFIB2zB0PP8NGAnSPTw"

# REST cook-history poll cadence (the hub writes a snapshot ~every 1.5 s).
DEFAULT_SCAN_INTERVAL = 10  # seconds

# Number of probe channels the hub supports.
MAX_PROBES = 4

# Monitoring is gated by a switch + auto-off timer (no reason to poll the cloud
# 24/7). When the switch is turned on, polling runs for this many minutes, then
# auto-disables. User-adjustable via the "Auto-off" number entity.
DEFAULT_AUTO_OFF_MINUTES = 60
MIN_AUTO_OFF_MINUTES = 1
MAX_AUTO_OFF_MINUTES = 1440  # 24 h
