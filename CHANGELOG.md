# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

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

[0.0.1]: https://github.com/GabrielGoldsteinAnidea/weber_connect_ha/releases/tag/v0.0.1
