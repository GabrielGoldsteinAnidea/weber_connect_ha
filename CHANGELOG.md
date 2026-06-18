# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.0.4] - 2026-06-17

### Added
- **Raw protocol capture for offline decoding.** While debug logging is on
  (`custom_components.weber_connect: debug`), every raw companion-websocket frame
  plus one raw cook-history snapshot per poll is appended to
  `config/weber_connect_capture.jsonl` (JSON lines: `{ts, poll, kind, len, hex}` /
  `{…, "data": {snapshot}}`), capped at ~20 MB. In debug mode the websocket reads
  the full window (no early break) to capture as many frames as possible.
- HACS validation GitHub Action (`.github/workflows/validate.yml`), with the
  `brands` check ignored until the integration is added to home-assistant/brands.
- README "Debugging & protocol capture" section documenting the logger string and
  the capture workflow.

## [0.0.3] - 2026-06-17

### Changed
- **Connection state is now debounced over a 60 s grace window.** Both transports
  arrive in bursts, so a single empty poll no longer flips the state; `stale` is
  declared only after 60 s with no data. Stops the per-poll streaming/polling/stale
  flapping (and stops the probe entities flapping unavailable between bursts).
- **Websocket read is gated** — opened only on polls with fresh REST snapshots (a
  burst, when the ws frame actually arrives) plus a periodic safety probe. Idle polls
  dropped from ~5.5 s to ~0.7 s, tightening the effective poll interval.

### Added
- Per-poll DEBUG logging in the coordinator (session, new-snapshot count, after_id,
  ws attempted/frame, per-transport ages, and timing) for diagnosing cloud behavior.
- Connection sensor attributes `rest_age_seconds`, `websocket_age_seconds`, and
  `last_poll_seconds`.

## [0.0.2] - 2026-06-17

### Added
- **Monitoring switch** (`switch.<hub>_monitoring`) — polling is now off by default
  and gated behind this switch, so the cloud isn't polled 24/7.
- **Auto-off duration** (`number.<hub>_auto_off`) — minutes the switch stays on before
  it auto-disables (default 60, restored across restarts). On expiry the switch turns
  itself off, polling stops, and probes disconnect.
- **Connection** sensor (`sensor.<hub>_connection`) — hub-level data status:
  `streaming` / `polling` / `stale` / `offline` / `off`, with `rest`/`websocket`
  transport breakdown, session id, and last snapshot id as attributes.
- Probe `status` enum gained a `connected` state (reading a temperature, doneness
  unknown when the websocket isn't streaming).
- Grill icon (`mdi:grill`) on entities, plus brand-tile assets under `brands/` for a
  future home-assistant/brands PR.

### Changed
- Probe temperature and status entities are now **unavailable** unless the connection
  is `streaming` or `polling` — they no longer show frozen/stale values when the hub
  has paused its cloud push.
- Connectivity is derived from the reliable REST temperature feed; the companion
  websocket only refines a connected probe to `idle`/`cooking`/`done`.
- Config-flow field labelled **"App Identifier"** to match the Weber app's Settings
  screen.

## [0.0.1] - 2026-06-17

First release. Cloud-polling Home Assistant integration for the Weber Connect Smart
Grilling Hub, built clean-room from decrypted app traffic.

### Added
- Config flow: set up by entering your companion `device_id` (the app's
  **App Identifier**) and `device_password`.
- One **temperature** sensor per probe channel (4), sourced from the REST
  cook-history API and polled every 10 s. Reports in °C/°F per your HA unit setting;
  goes unavailable when a probe is unplugged.
- One **status** sensor per probe (enum dropdown): `disconnected` / `idle` /
  `cooking` / `done` / `unknown`, sourced from the companion websocket. Keeps the
  last-known state when the websocket is contended by the phone app rather than
  flapping.
- All entities grouped under one device (the hub), with name, model, and serial
  pulled from the cloud.
- HACS support (`hacs.json`) and full README with in-app credential-location guide.

### Notes
- The app-global OAuth `client_id`/`client_secret` (identical for every Weber app
  install; verified as hardcoded literals in the APK) are embedded in `const.py` as
  `APP_CLIENT_ID`/`APP_CLIENT_SECRET`. They are not personal secrets. Your
  `device_id`/`device_password` are the only values you provide.
- Cloud data flows only while the hub is maintaining a walker-cloud session
  (typically during an active cook); see `docs/` for the reverse-engineering notes.

[0.0.4]: https://github.com/GabrielGoldsteinAnidea/weber_connect_ha/releases/tag/v0.0.4
[0.0.3]: https://github.com/GabrielGoldsteinAnidea/weber_connect_ha/releases/tag/v0.0.3
[0.0.2]: https://github.com/GabrielGoldsteinAnidea/weber_connect_ha/releases/tag/v0.0.2
[0.0.1]: https://github.com/GabrielGoldsteinAnidea/weber_connect_ha/releases/tag/v0.0.1
