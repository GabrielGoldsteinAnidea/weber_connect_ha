# Weber Connect cloud API — decoded protocol

Reverse-engineered from decrypted PCAPdroid captures of the Weber Connect Android
app (`com.weber.connect`, okhttp/5.3.0) talking to the Walker / "June Cloud"
backend. This is everything needed to build a Home Assistant integration that
polls the cloud for live + historical probe temperatures.

> ⚠️ The tokens, refresh token, and client secret below are **your own live
> credentials** from the capture. Treat them as secrets. The access token
> expires in ~6 hours; the refresh token is long-lived.

## Hosts

| Host | Purpose |
|------|---------|
| `api.walker-cloud.com` | REST API: auth, device registration, **cook history (temperatures)** |
| `messaging.walker-cloud.com` | WebSocket: real-time telemetry (binary protocol) |
| `cdn.walker-cloud.com` | Static assets, recipe content, firmware images |
| `devices-ota.walker-cloud.com` | Device OTA firmware |

Region variants exist: `*.walker-cloud.cn` (China) and `dev-` / `staging-`
prefixes. The app picks `AMER PROD` (seen in `X-June-Cloud-Region` header).

All traffic is HTTPS. **Certificate pinning** is enforced in-app via OkHttp for
`*.walker-cloud.com` (9 pinned SHA-256 SPKI hashes) — irrelevant for a clean-room
HA client that just makes normal HTTPS calls, but it's why packet capture needed
the patched APK.

## Authentication (OAuth2)

Token refresh (this is what the app does on launch — it already holds a refresh
token from the original username/password login):

```
GET /2/auth/oauth/token
      ?grant_type=refresh_token
      &refresh_token=<refresh_token>
      &client_secret=<client_secret>
      &client_id=<client_id>
Host: api.walker-cloud.com
```

Response:

```json
{
  "success": true,
  "token": {
    "access_token": "v2:<access_token>",
    "refresh_token": "v2:<YOUR_REFRESH_TOKEN>",
    "token_type": "Bearer",
    "expires_in": 21599,
    "scope": ""
  }
}
```

All authenticated requests then send:

```
Authorization: Bearer v2:<access_token>
```

Captured credential values (yours):

- `client_id`  = `qyw4CGeb/i93BrA0KAUuGtPyKImr+nUKc8lHxFdt`
- `client_secret` = `<CLIENT_SECRET_redacted>`
- `refresh_token` = `v2:<YOUR_REFRESH_TOKEN>`

(These appear URL-encoded in the request; values above are decoded. `client_id`
and `client_secret` are baked into the app and are the same for every user; the
refresh token is yours.)

The original interactive login (email/password → first refresh token) was not in
the capture because the app reused a stored session. If you need to script that
from scratch, capture one fresh logout→login; it'll be a POST to a
`/2/auth/...` endpoint. For an HA integration, storing the refresh token and
refreshing is the right model.

## Key identifiers (from this account)

| Field | Value | Meaning |
|-------|-------|---------|
| `device_id` | `065d…` | the **app install** instance id |
| `appliance_id` | `6f27…` | the **grill hub** (use this for data) |
| `session_id` | `53fd…` | a single cook session |
| `appliance_serial_number` | `0713… (serial redacted)` | hub serial |
| `device_type` | `saber` | hub model family |
| hub display name | `Weber Connect Hub` | |

## Temperature data — REST (recommended for HA)

This is the clean, JSON path and is fully validated.

```
GET /cook-history/1/appliance/{appliance_id}/session/{session_id}/snapshots
      ?limit=1000&after_id={lastSnapshotId}
Authorization: Bearer <access_token>
```

Response:

```json
{
  "session": {
    "appliance_id": "6f27…",
    "session_id": "53fd…",
    "boot_count": 27,
    "time_since_boot": 6236,
    "timestamp": 1781554030126,
    "server_timestamp": 1781554049887,
    "appliance_timestamp": 1781554029284,
    "created_at": "2026-06-15T20:07:29.887345Z",
    "updated_at": "2026-06-15T20:07:29.887345Z"
  },
  "snapshots": [
    {
      "appliance_id": "6f27…",
      "session_id": "53fd…",
      "snapshot_id": 1,
      "time_since_boot": 7347,
      "data": { "probe_status": [ { "index": 0, "temperature": 227 } ] }
    }
  ]
}
```

Data model:

- `snapshots[]` is an append-only time series. Page with `after_id` =
  highest `snapshot_id` you've seen (the app polls `limit=1000` repeatedly).
- `data.probe_status[]` — one entry per probe channel. `index` 0..N
  (hub supports up to 4 food probes + an ambient/grill channel). `temperature`
  is an **integer in °F** (227 = 227 °F, a typical smoker temp).
- `time_since_boot` is milliseconds since the hub powered on; combine with the
  session `timestamp`/`boot_count` to get wall-clock time.
- A `temperature` of 0 = no probe / no reading on that channel.

**Polling model for HA:** refresh the OAuth token when near expiry → find the
active `session_id` → poll the snapshots endpoint every ~5–10 s with `after_id`
to get new readings. (Finding the current session id: it comes from the
device/appliance listing and the websocket; see below. Easiest: read the latest
session from the appliance — capture one more session-list call if you want that
exact endpoint, or reuse the websocket which streams the active session_id.)

## Real-time telemetry — WebSocket (decoded)

```
GET /2/messaging/websocket/companion
Upgrade: websocket
Sec-WebSocket-Version: 13
Authorization: Bearer <access_token>
Host: messaging.walker-cloud.com
```

Server replies `101 Switching Protocols` then streams **binary** frames
(opcode 0x2). It is request-driven: the client first sends a small subscribe
sequence; the server then streams telemetry. Frame layout:

```
01 01 <appliance_id:16> 01 <device_id:16> <seq:u32 LE> <len:u16 LE> <payload>
```
(client->server frames use `01 02 <device> 02 <appliance> ...`)

`payload` is a nested **TLV** structure (1-byte tag, 1-byte length, value):

- `08 10 <session_id:16>`  — active session id.
- Per-probe sub-messages (tags `0x04` / `0x06` / `0x40`, each a nested TLV) carry:
  - `02 01 <index>`         — probe channel index.
  - `0a 02 <int16 LE>`      — **probe temperature in °F**  ← the live reading.
  - `03 10 <16 bytes>`      — a fixed 16-byte array (usually zero; NOT the temp).
- Device-info frames (len 177) carry the hub name `Weber Connect Hub`, serial,
  firmware `2.0.3_7398`, MAC, and `0a 04 <int32 LE>` = Wi-Fi RSSI (e.g. -56 dBm).

Validated against a live cook capture: ambient rose 229 → 283 °F and a food
probe climbed 235 → 242 °F, matching the cook-history REST series. Decoder:
`weber_connect.protocol.companion_temps(payload)` ->
`[{"index": n, "temp_f": t}, ...]`.

> Note on standalone use: replaying the captured subscribe frames does not work
> (the server closes the socket, and cook-history then returns 403). The live
> session is gated behind a per-connection handshake element the app derives at
> runtime. The *decoding* is complete; establishing a fresh session from a
> clean-room client still needs that handshake reversed (or run alongside the
> app). For Home Assistant, polling the REST cook-history of the active session
> is the simpler, proven path.

## Other endpoints seen (for completeness)

- `POST /2/devices/register` — registers the app install.
- `GET  /3/devices/{device_id}/tokens` — lists push tokens.
- `PUT  /3/devices/{device_id}/tokens/{fcm_token}?application_id=com.weber.connect&type=fcm`
  — registers an FCM push token.
- `GET  /device-registration/register?appliance_serial_number=...&device_type=saber&device_id=...&locale=en-US`
- `GET  /content-token/token` — token for CDN recipe content.
- `POST /data-pipeline-server/1/events/json` — analytics (ignore).

## Suggested HA integration shape

1. Config flow stores the `refresh_token` (+ embedded `client_id`/`client_secret`).
2. Coordinator refreshes the access token (`expires_in` ~21600 s) before expiry.
3. Discover `appliance_id` and current `session_id`.
4. `DataUpdateCoordinator` polls the cook-history snapshots every 5–10 s, exposing
   one HA `sensor` per probe `index` (°F), plus the grill/ambient channel.
5. (Phase 2) switch to the websocket for push once its payload scaling is confirmed.
